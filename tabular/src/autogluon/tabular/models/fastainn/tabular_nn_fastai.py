import copy
import logging
import pickle
import time
import warnings
from builtins import classmethod
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from autogluon.core.constants import REGRESSION, BINARY
from autogluon.core.features.types import R_OBJECT, R_INT, R_FLOAT, R_DATETIME, R_CATEGORY, R_BOOL
from autogluon.core.models import AbstractModel
from autogluon.core.models.abstract.model_trial import skip_hpo
from autogluon.core.utils import try_import_fastai
from autogluon.core.utils.files import make_temp_directory
from autogluon.core.utils.loaders import load_pkl
from autogluon.core.utils.multiprocessing_utils import is_fork_enabled
from autogluon.core.utils.savers import save_pkl
from .hyperparameters.parameters import get_param_baseline
from .hyperparameters.searchspaces import get_default_searchspace

# FIXME: Has a leak somewhere, training additional models in a single python script will slow down training for each additional model. Gets very slow after 20+ models (10x+ slowdown)
#  Slowdown does not appear to impact Mac OS
# Reproduced with raw torch: https://github.com/pytorch/pytorch/issues/31867
# https://forums.fast.ai/t/runtimeerror-received-0-items-of-ancdata/48935
# https://github.com/pytorch/pytorch/issues/973
# https://pytorch.org/docs/master/multiprocessing.html#file-system-file-system
# Slowdown bug not experienced on Linux if 'torch.multiprocessing.set_sharing_strategy('file_system')' commented out
# NOTE: If below line is commented out, Torch uses many file descriptors. If issues arise, increase ulimit through 'ulimit -n 2048' or larger. Default on Linux is 1024.
# torch.multiprocessing.set_sharing_strategy('file_system')

# MacOS issue: torchvision==0.7.0 + torch==1.6.0 can cause segfaults; use torch==1.2.0 torchvision==0.4.0

LABEL = '__label__'
MISSING = '__!#ag_internal_missing#!__'

logger = logging.getLogger(__name__)


# TODO: Takes extremely long time prior to training start if many (10000) continuous features from ngrams, debug - explore TruncateSVD option to reduce input dimensionality
# TODO: currently fastai automatically detect and use CUDA if available - add code to honor autogluon settings
class NNFastAiTabularModel(AbstractModel):
    """ Class for fastai v1 neural network models that operate on tabular data.

        Hyperparameters:
            y_scaler: on a regression problems, the model can give unreasonable predictions on unseen data.
            This attribute allows to pass a scaler for y values to address this problem. Please note that intermediate
            iteration metrics will be affected by this transform and as a result intermediate iteration scores will be
            different from the final ones (these will be correct).
            https://scikit-learn.org/stable/modules/classes.html#module-sklearn.preprocessing

            'layers': list of hidden layers sizes; None - use model's heuristics; default is None

            'emb_drop': embedding layers dropout; defaut is 0.1

            'ps': linear layers dropout - list of values applied to every layer in `layers`; default is [0.1]

            'bs': batch size; default is 256

            'lr': maximum learning rate for one cycle policy; default is 1e-2;
            see also https://fastai1.fast.ai/train.html#fit_one_cycle, One-cycle policy paper: https://arxiv.org/abs/1803.09820

            'epochs': number of epochs; default is 30

            # Early stopping settings. See more details here: https://fastai1.fast.ai/callbacks.tracker.html#EarlyStoppingCallback
            'early.stopping.min_delta': 0.0001,
            'early.stopping.patience': 10,
    """

    model_internals_file_name = 'model-internals.pkl'

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cat_columns = None
        self.cont_columns = None
        self.columns_fills = None
        self.procs = None
        self.y_scaler = None
        self._inner_features = None
        self._load_model = None  # Whether to load inner model when loading.

    def _preprocess_train(self, X, y, X_val, y_val, num_workers):
        from fastai.tabular.core import TabularPandas
        from fastai.data.block import RegressionBlock, CategoryBlock
        from fastai.data.transforms import IndexSplitter
        from fastcore.basics import range_of
        X = self.preprocess(X, fit=True)
        if X_val is not None:
            X_val = self.preprocess(X_val)

        from fastai.tabular.core import FillMissing, Categorify, Normalize
        self.procs = [FillMissing, Categorify, Normalize]

        if self.problem_type == REGRESSION and self.y_scaler is not None:
            y_norm = pd.Series(self.y_scaler.fit_transform(y.values.reshape(-1, 1)).reshape(-1))
            y_val_norm = pd.Series(self.y_scaler.transform(y_val.values.reshape(-1, 1)).reshape(-1)) if y_val is not None else None
            logger.log(0, f'Training with scaled targets: {self.y_scaler} - !!! NN training metric will be different from the final results !!!')
        else:
            y_norm = y
            y_val_norm = y_val

        logger.log(15, f'Using {len(self.cont_columns)} cont features')
        df_train, train_idx, val_idx = self._generate_datasets(X, y_norm, X_val, y_val_norm)

        y_block = RegressionBlock(1) if self.problem_type == REGRESSION else CategoryBlock()

        # Copy cat_columns and cont_columns because TabularList is mutating the list
        data = TabularPandas(
            df_train,
            cat_names=self.cat_columns.copy(),
            cont_names=self.cont_columns.copy(),
            procs=self.procs,
            y_block=y_block,
            y_names=LABEL,
            splits=IndexSplitter(val_idx)(range_of(df_train)),
        )
        return data

    def _preprocess(self, X: pd.DataFrame, fit=False, **kwargs):
        X = super()._preprocess(X=X, **kwargs)
        if fit:
            self.cat_columns = self.feature_metadata.get_features(valid_raw_types=[R_OBJECT, R_CATEGORY, R_BOOL])
            self.cont_columns = self.feature_metadata.get_features(valid_raw_types=[R_INT, R_FLOAT, R_DATETIME])
            try:
                X_stats = X.describe(include='all').T.reset_index()
                cat_cols_to_drop = X_stats[(X_stats['unique'] > self.params.get('max_unique_categorical_values', 10000)) | (X_stats['unique'].isna())]['index'].values
            except:
                cat_cols_to_drop = []
            cat_cols_to_keep = [col for col in X.columns.values if (col not in cat_cols_to_drop)]
            cat_cols_to_use = [col for col in self.cat_columns if col in cat_cols_to_keep]
            logger.log(15, f'Using {len(cat_cols_to_use)}/{len(self.cat_columns)} categorical features')
            self.cat_columns = cat_cols_to_use
            self.cat_columns = [feature for feature in self.cat_columns if feature in list(X.columns)]
            self.cont_columns = [feature for feature in self.cont_columns if feature in list(X.columns)]

            self.columns_fills = {}
            for c in self.cat_columns:
                self.columns_fills[c] = MISSING
            for c in self.cont_columns:
                self.columns_fills[c] = X[c].mean()
            self._inner_features = self.cat_columns + self.cont_columns
        return self._fill_missing(X)

    def _fill_missing(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df[self._inner_features].copy()
        for c in self.cat_columns:
            df[c] = df[c].cat.add_categories(MISSING)
            df[c] = df[c].fillna(self.columns_fills[c])
        for c in self.cont_columns:
            df[c] = df[c].fillna(self.columns_fills[c])
        return df

    def _fit(self,
             X,
             y,
             X_val=None,
             y_val=None,
             time_limit=None,
             num_cpus=None,
             num_gpus=0,
             sample_weight=None,
             **kwargs):
        try_import_fastai()
        from fastai.tabular.model import tabular_config
        from fastai.tabular.learner import tabular_learner
        from fastcore.basics import defaults
        from .callbacks import AgSaveModelCallback, EarlyStoppingCallbackWithTimeLimit
        import torch

        start_time = time.time()
        if sample_weight is not None:  # TODO: support
            logger.log(15, "sample_weight not yet supported for NNFastAiTabularModel, this model will ignore them in training.")

        params = self.params.copy()

        self.y_scaler = params.get('y_scaler', None)
        if self.y_scaler is not None:
            self.y_scaler = copy.deepcopy(self.y_scaler)

        if num_cpus is None:
            num_cpus = defaults.cpus
        # additional workers are helping only when fork is enabled; in other mp modes, communication overhead reduces performance
        num_workers = int(num_cpus / 2)
        if not is_fork_enabled():
            num_workers = 0
        if num_gpus is not None:
            if num_gpus == 0:
                # TODO: Does not obviously impact inference speed
                defaults.device = torch.device('cpu')
            else:
                defaults.device = torch.device('cuda')

        logger.log(15, f'Fitting Neural Network with parameters {params}...')
        data = self._preprocess_train(X, y, X_val, y_val, num_workers=num_workers)

        nn_metric, objective_func_name = self.__get_objective_func_name()
        objective_func_name_to_monitor = self.__get_objective_func_to_monitor(objective_func_name)
        objective_optim_mode = np.less if objective_func_name in [
            'root_mean_squared_error', 'mean_squared_error', 'mean_absolute_error', 'r2'  # Regression objectives
        ] else np.greater

        # TODO: calculate max emb concat layer size and use 1st layer as that value and 2nd in between number of classes and the value
        if params.get('layers', None) is not None:
            layers = params['layers']
        elif self.problem_type in [REGRESSION, BINARY]:
            layers = [200, 100]
        else:
            base_size = max(data.c * 2, 100)
            layers = [base_size * 2, base_size]

        loss_func = None

        if time_limit:
            time_elapsed = time.time() - start_time
            time_left = time_limit - time_elapsed
        else:
            time_left = None

        best_epoch_stop = params.get("best_epoch", None)  # Use best epoch for refit_full.
        dls = data.dataloaders(bs=self.params['bs'] if len(X) > self.params['bs'] else 32)

        self.model = tabular_learner(
            dls, layers=layers, metrics=nn_metric,
            config=tabular_config(ps=params['ps'], embed_p=params['emb_drop']),
            loss_func=loss_func,
        )
        logger.log(15, self.model.model)

        save_callback = AgSaveModelCallback(
            monitor=objective_func_name_to_monitor, comp=objective_optim_mode, fname=self.name,
            best_epoch_stop=best_epoch_stop
        )

        early_stopping = EarlyStoppingCallbackWithTimeLimit(
            monitor=objective_func_name_to_monitor,
            comp=objective_optim_mode,
            min_delta=params['early.stopping.min_delta'],
            patience=params['early.stopping.patience'],
            time_limit=time_left, best_epoch_stop=best_epoch_stop
        )

        callbacks = [save_callback, early_stopping]

        with make_temp_directory() as temp_dir:
            with self.model.no_bar():
                with self.model.no_logging():
                    original_path = self.model.path
                    self.model.path = Path(temp_dir)
                    self.model.fit_one_cycle(params['epochs'], params['lr'], cbs=callbacks)
                    # self.model.save(self.name)

                    # Load the best one and export it
                    self.model = self.model.load(self.name)

                    if objective_func_name == 'log_loss':
                        eval_result = self.model.validate(dl=dls.valid)[0]
                    else:
                        eval_result = self.model.validate(dl=dls.valid)[1]

                    logger.log(15, f'Model validation metrics: {eval_result}')
                    self.model.path = original_path

            self.params_trained['best_epoch'] = save_callback.best_epoch

    def _generate_datasets(self, X, y, X_val, y_val):
        df_train = pd.concat([X, X_val], ignore_index=True)
        df_train[LABEL] = pd.concat([y, y_val], ignore_index=True)
        train_idx = np.arange(len(X))
        if X_val is None:
            val_idx = train_idx + len(train_idx)  # use validation set for refit_full case - it's not going to be used for early stopping
            df_train = pd.concat([df_train, df_train], ignore_index=True)
        else:
            val_idx = np.arange(len(X_val)) + len(X)
        return df_train, train_idx, val_idx

    def __get_objective_func_name(self):
        from fastai.metrics import _rmse, mse, mae, accuracy, FBeta, RocAucBinary, Precision, Recall, R2Score

        metrics_map = {
            # Regression
            'root_mean_squared_error': _rmse,
            'mean_squared_error': mse,
            'mean_absolute_error': mae,
            'r2': R2Score(),
            # Not supported: median_absolute_error

            # Classification
            'accuracy': accuracy,

            'f1': FBeta(beta=1),
            'f1_macro': FBeta(beta=1, average='macro'),
            'f1_micro': FBeta(beta=1, average='micro'),
            'f1_weighted': FBeta(beta=1, average='weighted'),  # this one has some issues

            'roc_auc': RocAucBinary(),

            'precision': Precision(),
            'precision_macro': Precision(average='macro'),
            'precision_micro': Precision(average='micro'),
            'precision_weighted': Precision(average='weighted'),

            'recall': Recall(),
            'recall_macro': Recall(average='macro'),
            'recall_micro': Recall(average='micro'),
            'recall_weighted': Recall(average='weighted'),
            'log_loss': None,
            # Not supported: pac_score
        }

        # Unsupported metrics will be replaced by defaults for a given problem type
        objective_func_name = self.stopping_metric.name
        if objective_func_name not in metrics_map.keys():
            if self.problem_type == REGRESSION:
                objective_func_name = 'mean_squared_error'
            else:
                objective_func_name = 'log_loss'
            logger.warning(f'Metric {self.stopping_metric.name} is not supported by this model - using {objective_func_name} instead')

        if objective_func_name in metrics_map.keys():
            nn_metric = metrics_map[objective_func_name]
        else:
            nn_metric = None
        return nn_metric, objective_func_name

    def __get_objective_func_to_monitor(self, objective_func_name):
        monitor_obj_func = {
            'roc_auc': 'auroc',

            'f1': 'f_beta',
            'f1_macro': 'f_beta',
            'f1_micro': 'f_beta',
            'f1_weighted': 'f_beta',

            'precision_macro': 'precision',
            'precision_micro': 'precision',
            'precision_weighted': 'precision',

            'recall_macro': 'recall',
            'recall_micro': 'recall',
            'recall_weighted': 'recall',
            'log_loss': 'valid_loss',
        }
        objective_func_name_to_monitor = objective_func_name
        if objective_func_name in monitor_obj_func:
            objective_func_name_to_monitor = monitor_obj_func[objective_func_name]
        return objective_func_name_to_monitor

    def _predict_proba(self, X, **kwargs):
        X = self.preprocess(X, **kwargs)

        single_row = len(X) == 1
        # fastai has issues predicting on a single row, duplicating the row as a workaround
        if single_row:
            X = pd.concat([X, X]).reset_index(drop=True)

        # Copy cat_columns and cont_columns because TabularList is mutating the list
        test_dl = self.model.dls.test_dl(X)
        with self.model.no_bar():
            with self.model.no_logging():
                preds, _ = self.model.get_preds(dl=test_dl)
        if single_row:
            preds = preds[:1, :]
        if self.problem_type == REGRESSION:
            if self.y_scaler is not None:
                return self.y_scaler.inverse_transform(preds.numpy()).reshape(-1)
            else:
                return preds.numpy().reshape(-1)
        if self.problem_type == BINARY:
            return preds[:, 1].numpy()
        else:
            return preds.numpy()

    def save(self, path: str = None, verbose=True) -> str:
        self._load_model = self.model is not None
        __model = self.model
        self.model = None
        path = super().save(path=path, verbose=verbose)
        self.model = __model
        # Export model
        if self._load_model:
            save_pkl.save_with_fn(
                f'{path_final}{self.model_internals_file_name}',
                self.model,
                pickle_fn=lambda m, buffer: self.export(m, buffer),
                verbose=verbose
            )
        self._load_model = None
        return path

    @classmethod
    def export(cls, model, filename_or_stream='export.pkl', pickle_module=pickle, pickle_protocol=2):
        from fastai.torch_core import rank_distrib
        import torch
        "Export the content of `self` without the items and the optimizer state for inference"
        if rank_distrib(): return  # don't export if child proc
        model._end_cleanup()
        old_dbunch = model.dls
        model.dls = model.dls.new_empty()
        state = model.opt.state_dict() if model.opt is not None else None
        model.opt = None
        target = open(model.path / filename_or_stream, 'wb') if cls.is_pathlike(filename_or_stream) else filename_or_stream
        with warnings.catch_warnings():
            # To avoid the warning that come from PyTorch about model not being checked
            warnings.simplefilter("ignore")
            torch.save(model, target, pickle_module=pickle_module, pickle_protocol=pickle_protocol)
        model.create_opt()
        if state is not None:
            model.opt.load_state_dict(state)
        model.dls = old_dbunch

    @classmethod
    def is_pathlike(cls, x: Any) -> bool:
        return isinstance(x, (str, Path))

    @classmethod
    def load(cls, path: str, reset_paths=True, verbose=True):
        from fastai.learner import load_learner
        model = super().load(path, reset_paths=reset_paths, verbose=verbose)
        if model._load_model:
            from fastai.basic_train import load_learner
            model.model = load_pkl.load_with_fn(f'{model.path}{model.model_internals_file_name}', lambda p: load_learner(p), verbose=verbose)
        model._load_model = None
        return model

    def _set_default_params(self):
        """ Specifies hyperparameter values to use by default """
        default_params = get_param_baseline(self.problem_type)
        for param, val in default_params.items():
            self._set_default_param_value(param, val)

    def _get_default_searchspace(self):
        return get_default_searchspace(self.problem_type, num_classes=None)

    # TODO: add warning regarding dataloader leak: https://github.com/pytorch/pytorch/issues/31867
    # TODO: Add HPO
    def _hyperparameter_tune(self, **kwargs):
        return skip_hpo(self, **kwargs)

    def _get_default_auxiliary_params(self) -> dict:
        default_auxiliary_params = super()._get_default_auxiliary_params()
        extra_auxiliary_params = dict(
            ignored_type_group_raw=[R_OBJECT],
        )
        default_auxiliary_params.update(extra_auxiliary_params)
        return default_auxiliary_params

