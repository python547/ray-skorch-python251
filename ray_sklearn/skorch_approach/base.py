from typing import Callable, Optional, Type, Union
from contextlib import AbstractContextManager
import io
import numpy as np
import inspect

from ray import train
from ray.train.trainer import Trainer
from ray.train.session import get_session
import ray.data.impl.progress_bar
from ray.data.dataset import Dataset
from ray.data.dataset_pipeline import DatasetPipeline

from skorch import NeuralNet
from skorch.callbacks import Callback
from skorch.callbacks.logging import filter_log_keys
from skorch.dataset import Dataset as SkorchDataset
from skorch.utils import is_dataset

import torch
from torch.nn.parallel.distributed import DistributedDataParallel

from sklearn.base import clone

from ray_sklearn.skorch_approach.dataset import (FixedSplit, PipelineIterator,
                                                 dataset_factory)


def _is_in_train_session() -> bool:
    try:
        get_session()
        return True
    except ValueError:
        return False


def _is_dataset_or_ray_dataset(x) -> bool:
    return is_dataset(x) or isinstance(x, (Dataset, DatasetPipeline))


def _is_using_gpu(device) -> bool:
    return device == "cuda" and torch.cuda.is_available()


class ray_trainer_start_shutdown(AbstractContextManager):
    def __init__(self,
                 trainer: Trainer,
                 initialization_hook: Optional[Callable] = None) -> None:
        self.trainer = trainer
        self.initialization_hook = initialization_hook

    def __enter__(self):
        self.trainer.start(self.initialization_hook)

    def __exit__(self, __exc_type, __exc_value, __traceback) -> None:
        self.trainer.shutdown()


class _TrainReportCallback(Callback):
    def __init__(
            self,
            keys_ignored=None,
    ):
        self.keys_ignored = keys_ignored

    def initialize(self):
        if not _is_in_train_session():
            return
        self.first_iteration_ = True

        keys_ignored = self.keys_ignored
        if isinstance(keys_ignored, str):
            keys_ignored = [keys_ignored]
        self.keys_ignored_ = set(keys_ignored or [])
        self.keys_ignored_.add("batches")
        return self

    def _sorted_keys(self, keys):
        """Sort keys, dropping the ones that should be ignored.

        The keys that are in ``self.ignored_keys`` or that end on
        '_best' are dropped. Among the remaining keys:
          * 'epoch' is put first;
          * 'dur' is put last;
          * keys that start with 'event_' are put just before 'dur';
          * all remaining keys are sorted alphabetically.
        """
        sorted_keys = []

        # make sure "epoch" comes first
        if ("epoch" in keys) and ("epoch" not in self.keys_ignored_):
            sorted_keys.append("epoch")

        # ignore keys like *_best or event_*
        for key in filter_log_keys(
                sorted(keys), keys_ignored=self.keys_ignored_):
            if key != "dur":
                sorted_keys.append(key)

        # add event_* keys
        for key in sorted(keys):
            if key.startswith("event_") and (key not in self.keys_ignored_):
                sorted_keys.append(key)

        # make sure "dur" comes last
        if ("dur" in keys) and ("dur" not in self.keys_ignored_):
            sorted_keys.append("dur")

        return sorted_keys

    def on_epoch_end(self, net, **kwargs):
        if not _is_in_train_session():
            return
        history = net.history
        hist = history[-1]
        train.report(**{
            k: v
            for k, v in hist.items() if k in self._sorted_keys(hist.keys())
        })


class _WorkerRayTrainNeuralNet(NeuralNet):
    """Internal use only. Estimator used inside each Train worker."""

    def initialize_callbacks(self):
        super().initialize_callbacks()
        if train.world_rank() != 0:
            self.callbacks_ = [
                callback_tuple for callback_tuple in self.callbacks_
                if getattr(callback_tuple[0], "_on_all_ranks", False)
            ]
        report_callback = _TrainReportCallback()
        report_callback.initialize()
        self.callbacks_ += [("ray_train", report_callback)]
        return self

    def initialize_module(self):
        super().initialize_module()
        self.module_ = DistributedDataParallel(
            self.module_,
            find_unused_parameters=True,
            device_ids=[train.local_rank()]
            if _is_using_gpu(self.device) else None)
        return self

    def initialize(self):
        assert _is_in_train_session()  # TODO improve
        return super().initialize()

    @property
    def iterator_train_(self):
        return getattr(self, "_iterator_train_", None)

    @property
    def iterator_valid_(self):
        return getattr(self, "_iterator_valid_", None)

    @iterator_train_.setter
    def iterator_train_(self, val):
        self._iterator_train_ = val

    @iterator_valid_.setter
    def iterator_valid_(self, val):
        self._iterator_valid_ = val

    def get_iterator(self, dataset, training=False):
        if training:
            initalized_iterator = self.iterator_train_
            if initalized_iterator is None:
                kwargs = self.get_params_for('iterator_train')
                iterator = self.iterator_train
        else:
            initalized_iterator = self.iterator_valid_
            if initalized_iterator is None:
                kwargs = self.get_params_for('iterator_valid')
                iterator = self.iterator_valid

        if initalized_iterator is not None:
            return iter(initalized_iterator)

        if 'batch_size' not in kwargs:
            kwargs['batch_size'] = self.batch_size

        if kwargs['batch_size'] == -1:
            kwargs['batch_size'] = len(dataset)

        initalized_iterator = iterator(dataset, **kwargs)

        if training:
            self.iterator_train_ = initalized_iterator
        else:
            self.iterator_valid_ = initalized_iterator

        return initalized_iterator

    def fit(self, X, y=None, X_val=None, y_val=None, **fit_params):
        if not self.warm_start or not self.initialized_:
            self.initialize()

        self.partial_fit(X, y, X_val=X_val, y_val=y_val, **fit_params)
        return self

    def partial_fit(self,
                    X,
                    y=None,
                    classes=None,
                    X_val=None,
                    y_val=None,
                    **fit_params):
        if not self.initialized_:
            self.initialize()

        self.notify('on_train_begin', X=X, y=y)
        try:
            self.fit_loop(X, y, X_val=X_val, y_val=y_val, **fit_params)
        except KeyboardInterrupt:
            pass
        self.notify('on_train_end', X=X, y=y)
        return self

    def fit_loop(self,
                 X,
                 y=None,
                 epochs=None,
                 X_val=None,
                 y_val=None,
                 **fit_params):
        assert _is_in_train_session()  # TODO improve
        self.check_data(X, y)
        epochs = epochs if epochs is not None else self.max_epochs

        if X_val is None:
            dataset_train, dataset_valid = self.get_split_datasets(
                X, y, **fit_params)
        else:
            self.check_data(X_val, y_val)
            if _is_dataset_or_ray_dataset(X_val) and y_val is None:
                y_val = y
            dataset_train = self.get_dataset(X, y)
            dataset_valid = self.get_dataset(X_val, y_val)

        assert dataset_train.y == dataset_valid.y  # TODO improve

        on_epoch_kwargs = {
            'dataset_train': dataset_train,
            'dataset_valid': dataset_valid,
        }

        for _ in range(epochs):
            self.notify('on_epoch_begin', **on_epoch_kwargs)

            self.run_single_epoch(
                dataset_train,
                training=True,
                prefix="train",
                step_fn=self.train_step,
                **fit_params)

            if dataset_valid is not None:
                self.run_single_epoch(
                    dataset_valid,
                    training=False,
                    prefix="valid",
                    step_fn=self.validation_step,
                    **fit_params)

            self.notify("on_epoch_end", **on_epoch_kwargs)
        return self


class RayTrainNeuralNet(NeuralNet):
    prefixes_ = NeuralNet.prefixes_ + ["worker_dataset", "trainer"]

    def __init__(self,
                 module,
                 criterion,
                 num_workers: int,
                 optimizer=torch.optim.SGD,
                 lr=0.01,
                 max_epochs=10,
                 batch_size=128,
                 iterator_train=PipelineIterator,
                 iterator_valid=PipelineIterator,
                 dataset=dataset_factory,
                 worker_dataset=SkorchDataset,
                 train_split=FixedSplit(0.2),
                 callbacks=None,
                 predict_nonlinearity='auto',
                 warm_start=False,
                 verbose=1,
                 device='cpu',
                 trainer: Union[Type[Trainer], Trainer] = Trainer,
                 **kwargs):
        super().__init__(
            module,
            criterion,
            optimizer=optimizer,
            lr=lr,
            max_epochs=max_epochs,
            batch_size=batch_size,
            iterator_train=iterator_train,
            iterator_valid=iterator_valid,
            dataset=dataset,
            train_split=train_split,
            callbacks=callbacks,
            predict_nonlinearity=predict_nonlinearity,
            warm_start=warm_start,
            verbose=verbose,
            device=device,
            **kwargs)
        self.trainer = trainer
        self.worker_dataset = worker_dataset
        self.num_workers = num_workers

    def initialize(self, initialize_ray=True):
        self.initialize_virtual_params()
        self.initialize_callbacks()

        if initialize_ray:
            self.initialize_trainer()
        else:
            self.initialize_criterion()
            self.initialize_module()
            self.initialize_optimizer()
            self.initialize_history()

        self.initialized_ = True
        return self

    def initialize_trainer(self):
        kwargs = self.get_params_for("trainer")
        trainer = self.trainer
        is_initialized = isinstance(trainer, Trainer)

        if kwargs or not is_initialized:

            kwargs["backend"] = "torch"
            if "num_workers" not in kwargs:
                kwargs["num_workers"] = self.num_workers
            if "use_gpu" not in kwargs:
                kwargs["use_gpu"] = self.device != "cpu"

            if is_initialized:
                trainer = type(trainer)

            if (is_initialized or self.initialized_) and self.verbose:
                msg = self._format_reinit_msg("trainer", kwargs)
                print(msg)

            trainer = trainer(**kwargs)

        if not trainer._backend == "torch":
            raise ValueError("Only torch backend is supported")

        self.trainer_ = trainer
        return self

    def _get_history_io(self, **values):
        return {
            "f_params": io.BytesIO(values.get("f_params", None)),
            "f_optimizer": io.BytesIO(values.get("f_optimizer", None)),
            "f_criterion": io.BytesIO(values.get("f_criterion", None)),
            "f_history": io.StringIO(values.get("f_history", None)),
        }

    def _get_worker_estimator(self) -> _WorkerRayTrainNeuralNet:
        est = clone(self)
        worker_attributes = set(
            inspect.signature(_WorkerRayTrainNeuralNet.__init__).parameters)
        driver_attributes = set(
            inspect.signature(self.__class__.__init__).parameters)
        attributes_to_remove = driver_attributes.difference(worker_attributes)
        for attr in attributes_to_remove:
            del est.__dict__[attr]
        est.__class__ = _WorkerRayTrainNeuralNet
        est.set_params(dataset=self.worker_dataset)
        return est

    def fit(self, X, y=None, X_val=None, y_val=None, **fit_params):
        if not self.warm_start or not self.initialized_:
            self.initialize()

        self.partial_fit(X, y, X_val=X_val, y_val=y_val, **fit_params)
        return self

    def partial_fit(self,
                    X,
                    y=None,
                    classes=None,
                    X_val=None,
                    y_val=None,
                    **fit_params):
        if not self.initialized_:
            self.initialize()

        self.notify('on_train_begin', X=X, y=y)
        try:
            self.fit_loop(X, y, X_val=X_val, y_val=y_val, **fit_params)
        except KeyboardInterrupt:
            pass
        self.notify('on_train_end', X=X, y=y)
        return self

    def fit_loop(self,
                 X,
                 y=None,
                 epochs=None,
                 X_val=None,
                 y_val=None,
                 **fit_params):
        if X_val is None:
            dataset_train, dataset_valid = self.get_split_datasets(
                X, y, **fit_params)
        else:
            self.check_data(X_val, y_val)
            if _is_dataset_or_ray_dataset(X_val) and y_val is None:
                y_val = y
            dataset_train = self.get_dataset(X, y)
            dataset_valid = self.get_dataset(X_val, y_val)

        assert dataset_train.y == dataset_valid.y  # TODO improve

        est = self._get_worker_estimator()
        show_progress_bars = ray.data.impl.progress_bar._enabled

        def train_func(config):
            label = config.pop("label")
            dataset_class = config.pop("dataset_class")
            ray.data.impl.progress_bar.set_progress_bars(show_progress_bars)

            X_train = dataset_class(
                train.get_dataset_shard("dataset_train"), label)
            X_val = dataset_class(
                train.get_dataset_shard("dataset_valid"), label)

            using_cuda = False
            if _is_using_gpu(est.device):
                using_cuda = True
                est.set_params(device=f"cuda:{train.local_rank()}")

            est.fit(X_train, None, epochs=epochs, X_val=X_val, **fit_params)

            if train.world_rank() == 0:
                if using_cuda:
                    est.set_params(device="cuda")
                output = self._get_history_io()
                est.save_params(
                    f_params=output["f_params"],
                    f_optimizer=output["f_optimizer"],
                    f_criterion=output["f_criterion"],
                    f_history=output["f_history"])
                return {k: v.getvalue() for k, v in output.items()}
            return {}

        with ray_trainer_start_shutdown(self.trainer_):
            results = self.trainer_.run(
                train_func,
                config={
                    "dataset_class": self.dataset,
                    "label": dataset_train.y
                },
                dataset={
                    "dataset_train": dataset_train.X,
                    "dataset_valid": dataset_valid.X
                })

        self.initialize(initialize_ray=False)
        params = results[0]
        self.module_ = params.pop("f_params")
        params = self._get_history_io(**params)
        params.pop("f_params")
        self.load_params(**params)
        return self

    def predict_proba(self, X):
        dataset = self.get_dataset(X, None)
        est = clone(self)

        def train_func(config):
            label = config.pop("label")
            config = self._get_history_io(**config)
            est.initialize(initialize_ray=False).load_params(**config)
            X_ray_dataset = train.get_dataset_shard().to_torch(
                label_column=label)
            ret = est.predict_proba(X_ray_dataset)
            return {"ret": ret}

        output = self._get_history_io()
        self.save_params(
            f_params=None,
            f_optimizer=output["f_optimizer"],
            f_criterion=output["f_criterion"],
            f_history=output["f_history"])
        output.pop("f_params")
        output = {k: v.getvalue() for k, v in output.items()}
        output["f_params"] = self.module_
        output["label"] = dataset.y

        with ray_trainer_start_shutdown(self.trainer_):
            results = self.trainer_.run(train_func, output, dataset=dataset.X)
        return np.vstack(
            [result["ret"].ravel().reshape(-1, 1) for result in results])