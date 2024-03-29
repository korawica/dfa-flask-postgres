# ------------------------------------------------------------------------------
# Copyright (c) 2022 Korawich Anuttra. All rights reserved.
# Licensed under the MIT License. See LICENSE in the project root for
# license information.
# ------------------------------------------------------------------------------

import ast
import builtins
import datetime as dt
import functools
import inspect
import itertools
import time
from typing import (
    Any,
    Iterator,
    List,
    Optional,
    Tuple,
    Type,
    Union,
)

import pandas as pd
from sqlalchemy.exc import ProgrammingError

from app.core.base import get_catalogs
from app.core.connections.postgresql import (
    ParamType,
    query_execute,
    query_execute_row,
    query_insert_from_csv,
    query_select,
    query_select_df,
    query_select_one,
    query_transaction,
)
from app.core.errors import (
    ControlPipelineNotExists,
    ControlProcessNotExists,
    ControlTableNotExists,
    ControlTableValueError,
    DatabaseProcessError,
    DatabaseSchemaNotExists,
    FuncArgumentError,
    FuncNotFound,
    FuncRaiseError,
    ObjectBaseError,
    ProcessValueError,
    TableArgumentError,
    TableNotFound,
    TableNotImplement,
)
from app.core.legacy.base import (
    AI_APP_PATH,
    FuncCatalog,
    PipeCatalog,
    TblCatalog,
    filter_ps_type,
    get_cal_date,
    get_plural,
    get_process_date,
    get_process_id,
    get_run_date,
    params,
    registers,
    sort_by_priority,
    verbose_log,
)
from app.core.legacy.convertor import (
    Statement,
    Value,
    reduce_in_value,
    reduce_stm,
    reduce_text,
    reduce_value,
    reduce_value_pairs,
)
from app.core.legacy.models import Status, VerboseDummy
from app.core.utils.cache import ignore_unhash
from app.core.utils.config import (
    Environs,
)
from app.core.utils.logging_ import logging
from app.core.utils.reusables import (
    convert_str_bool,
    merge_dicts,
    must_bool,
    must_list,
    only_one,
    path_join,
    split_iterable,
)
from conf.settings import settings

env = Environs(env_name='.env')
logger = logging.getLogger(__name__)


# [x] Migrate to modern style by `connections`
def query_select_row(statement: str, parameters: Optional[dict] = None) -> int:
    """Enhance query function to get `row_number` value from result"""
    if any(
            _ in statement
            for _ in {
                'select count(*) as row_number from ',
                'func_count_if_exists',
            }
    ):
        return int(
            query_select_one(statement, parameters=parameters)['row_number']
        )
    return query_execute_row(statement, parameters=parameters)


# [x] Migrate to modern style by `connections`
def query_select_check(
        statement: str, parameters: Optional[dict] = None
) -> bool:
    """Enhance query function to get `check_exists` value from result"""
    return eval(
        query_select_one(statement, parameters=parameters)['check_exists']
    )


def query_explain(statement: str, parameters: ParamType = True) -> dict:
    """Enhance query function for explain analytic"""
    stm = Statement(statement)
    query: str = stm.generate().strip()
    if query.count(';') > 1:
        raise FuncRaiseError(
            "query should not contain `;` more than 1 letter in 1 query"
        )
    if stm.stm_type == 'dql':
        _statement: str = params.ps_stm.explain.format(query=query.rstrip(';'))
        return {
            "QUERY PLAN": ast.literal_eval(
                query_select_one(
                    _statement,
                    parameters=parameters
                )['QUERY PLAN']
            )
        }
    elif stm.stm_type == 'dml':
        _statement: str = params.ps_stm.explain.format(
            query=Statement.add_row_num(query.rstrip(';')).rstrip(';')
        )
        return {
            "QUERY PLAN": ast.literal_eval(
                query_select_one(
                    _statement,
                    parameters=parameters
                )['QUERY PLAN']
            )
        }
    raise FuncArgumentError(
        "query explain support only statement type be `dql` or `dml` only"
    )


# [x] Migrate to modern style by `Schema` Model
def push_schema_create(schema_name: str):
    """Push create schema"""
    query_execute(
        reduce_stm(params.bs_stm.create.schema),
        parameters={'schema_name': schema_name},
    )


# [x] Migrate to modern style by `Schema` Model
def push_schema_drop(schema_name: str, cascade: bool = False):
    """Push drop schema"""
    query_execute(
        reduce_stm(params.bs_stm.drop.schema),
        parameters={
            'schema_name': schema_name,
            'cascade': ('cascade' if cascade else '')
        }
    )


# [x] Migrate to modern style by `Schema` Model
def check_schema_exists(schema_name: str) -> bool:
    """Check schema exists in database"""
    return query_select_check(
        params.ps_stm.exists.schema, parameters={"schema_name": schema_name}
    )


def check_ai_exists() -> bool:
    return check_schema_exists(schema_name=env.get('AI_SCHEMA', 'ai'))


def get_time_checkpoint(
        date_type: Optional[str] = None
) -> Union[dt.datetime, dt.date]:
    return get_run_date(date_type=(date_type or 'date_time'))


def split_choose(choose: Union[str, list]) -> Tuple[list, list]:
    processes: list = list(set(choose)) if isinstance(choose, list) else [choose]
    _process: dict = {'reject': [], 'filter': []}
    for process in processes:
        if process.startswith('!'):
            _process['reject'].append(process.split('!')[-1])
        else:
            _process['filter'].append(process)
    return _process['filter'], _process['reject']


def _cal_ctr_params(parameters: dict) -> dict:
    proportion_value: int = parameters.get('proportion_value', 3)
    proportion_inc_curr_m: str = parameters.get(
        'proportion_inc_current_month_flag', 'N'
    )
    return {
        'window_start': (
            proportion_value
            if proportion_inc_curr_m == 'N' else (proportion_value - 1)
        ),
        'window_end': (
            1 if proportion_inc_curr_m == 'N' else 0
        ),
        **parameters
    }


class Control:
    """Control Object for get all control framework data from target database.
        The main class methods for this object are,
            - parameters
            - tables
            - catalogs

        The main methods for this object are,
            - pull:
            - push:
            - update:
    """

    __slots__ = (
        'ctr',
        'ctr_cols',
        'ctr_cols_exc_pk',
        'ctr_pk'
    )

    @classmethod
    def parameters(cls, module: Optional[str] = None) -> dict:
        """Get all parameters with `module` argument from `ctr_data_parameter`
        in target database and convert to python dictionary type.
        """
        verbose_log(
            VerboseDummy,
            "[Start] loading parameters in `ctr_data_parameter` that deploy in target database"
        )
        try:
            _results: dict = {
                value['param_name']: ast.literal_eval(value['param_value'])
                if value['param_type'] in {'list', 'dict'}
                else getattr(builtins, value['param_type'])(value['param_value'])
                for value in query_select(
                    params.ps_stm.pull_ctr_params,
                    parameters=reduce_value_pairs(
                        {"module_type": (module or '*')}
                    ),
                )
            }
            verbose_log(
                VerboseDummy,
                "[Success] loading parameters in `ctr_data_parameter` that deploy in target database",
                end='-'
            )

            # Calculate special parameters that logic was provided by vendor.
            proportion_value: int = _results.get('proportion_value', 3)
            proportion_inc_curr_m: str = _results.get(
                'proportion_inc_current_month_flag', 'N'
            )
            window_end: int = 1 if proportion_inc_curr_m == 'N' else 0
            window_start: int = (
                proportion_value
                if proportion_inc_curr_m == 'N'
                else (proportion_value - 1)
            )
            return {
                'window_start': window_start,
                'window_end': window_end,
                **_results
            }
        except DatabaseProcessError:
            return {}

    @classmethod
    def tables(cls, condition: Optional[str] = None) -> List[dict]:
        """Get all tables with `condition` argument from `ctr_data_pipeline` in
        target database and convert to python list of dictionary type.
        """
        verbose_log(
            VerboseDummy,
            (
                "[Start] loading all tables in `ctr_data_pipeline` that"
                "deploy in target database"
            ),
        )
        _sorted = sort_by_priority([
            tbl['table_name'] for tbl in cls('ctr_data_pipeline').pull(
                pm_filter={'table_name': '*'},
                included_cols=['table_name'],
                condition=condition,
                all_flag=True
            )
        ])
        verbose_log(
            VerboseDummy,
            (
                "[Success] loading all tables in `ctr_data_pipeline` that "
                "deploy in target database"
            ),
            end='-'
        )
        return [{'table_name': name} for name in _sorted]

    @classmethod
    def catalogs(cls) -> List[dict]:
        """Get all catalogs from all configuration file at `./conf/catalog` path
        """
        _result: list = list(get_catalogs(
                config_form='catalog',
                priority_sorted=True
        ).keys())
        return [
            {'catalog_name': _} for _ in _result
        ]

    def __init__(
            self,
            ctr: Union[TblCatalog, str]
    ):
        """Main Initialization of Control object"""
        if isinstance(ctr, TblCatalog):
            self.ctr: TblCatalog = ctr
        elif isinstance(ctr, str):
            self.ctr: TblCatalog = TblCatalog(ctr)
        else:
            raise ControlTableValueError(
                f"Control object does not support for {type(ctr)!r} type"
            )

        # Set column attributes.
        self.ctr_cols: list = self.ctr.get_tbl_columns(pk_included=True)
        self.ctr_cols_exc_pk: list = self.ctr.get_tbl_columns(pk_included=False)
        self.ctr_pk: list = self.ctr.tbl_primary_key

    @property
    def name(self) -> str:
        return self.ctr.tbl_name

    @property
    def name_short(self) -> str:
        return self.ctr.tbl_name_sht

    @property
    def col_default(self) -> dict:
        return {
            'update_date': get_run_date(fmt='%Y-%m-%d %H:%M:%S'),
            'process_time': 0,
            'status': 2,
        }

    def __repr__(self):
        return f'{self.__class__.__name__}({self.name})'

    def __str__(self):
        return self.name

    def pull(
            self,
            pm_filter: Union[list, dict],
            condition: Optional[str] = None,
            included_cols: Optional[list] = None,
            active_flag: Optional[str] = None,
            all_flag: Optional[bool] = False
    ) -> dict:
        """Pull data from the Control Framework table in database"""
        if len(self.ctr_pk) > 1 and isinstance(pm_filter, list):
            raise TableNotImplement(
                f"Pull control does not support `pm_filter` with `list` type "
                f"when {self.name} have primary keys more than 1"
            )
        elif isinstance(pm_filter, dict):
            if any(col not in self.ctr_pk for col in pm_filter):
                raise TableArgumentError(
                    f"Pull control does not support value in `pm_filter` "
                    f"with keys, {str(pm_filter.keys())}"
                )
            _pm_filter: dict = reduce_value_pairs(pm_filter)
        else:
            _pm_filter: dict = {
                self.ctr_pk[0]: ', '.join(map(reduce_value, pm_filter))
            }
        _pm_filter_stm: str = ' and '.join([
            f"{pk} in ({_pm_filter[pk]})"
            for pk in self.ctr_pk
        ])
        _query: callable = query_select if all_flag else query_select_one
        return _query(
            params.ps_stm.pull_ctr,
            parameters={
                'select_columns': ", ".join(
                    col
                    for col in self.ctr_cols
                    if col in (included_cols or self.ctr_cols)
                ),
                'table_name': self.name,
                'primary_key_filters': _pm_filter_stm,
                'active_flag': (
                    f"and active_flg in ('{(active_flag or 'Y')}')"
                    if "active_flg" in self.ctr_cols else ""
                ),
                'condition': (
                    f"""and ({condition.replace('"', "'")})"""
                    if condition else ""
                ),
            },
        )

    def push(
            self,
            push_values: dict,
            condition: Optional[str] = None
    ) -> int:
        """Push New data to the Control Framework tables, such as
            - `ctr_data_logging`
            - `ctr_task_process`
        """
        _ctr_columns = filter(
            lambda _col: _col not in {'primary_id'}, self.ctr_cols
        )
        _add_column: dict = merge_dicts(self.col_default, {
            'tracking': 'SUCCESS',
            'active_flg': 'Y',
        })
        _row_record_filter: str = ""
        _status_filter: str = ""
        for col in _ctr_columns:
            value_old: Union[str, int] = (
                _add_column.get(col, 'null')
                if col not in push_values else push_values[col]
            )
            push_values[col]: str = reduce_value(value_old)
        if 'status' in list(_ctr_columns):
            _status_filter: str = "where excluded.status = '2'"
        if 'row_record' in list(_ctr_columns):
            _row_record_filter: str = (
                f"{'' if _status_filter else 'where'} "
                f"{'or' if _status_filter else ''} "
                f"{self.name_short}.row_record <= excluded.row_record"
            )
        _set_value_pairs: str = ", ".join([
            f"{_} = excluded.{_}" for _ in self.ctr_cols_exc_pk
        ])
        return query_select_row(
            reduce_stm(params.ps_stm.push_ctr, add_row_number=False),
            parameters={
                'table_name': self.name,
                'table_name_sht': self.name_short,
                'columns_pair': ', '.join(push_values),
                'values': ', '.join(push_values.values()),
                'primary_key': ', '.join(self.ctr_pk),
                'set_value_pairs': _set_value_pairs,
                'row_record_filter': _row_record_filter,
                'status_filter': _status_filter,
                'condition': f"""and ({condition.replace('"', "'")})"""
                if condition
                else "",
            },
        )

    def update(
            self,
            update_values: dict,
            condition: Optional[str] = None
    ) -> int:
        """Push Updated data to the Control Framework tables, such as
            - `ctr_data_pipeline`
            - `ctr_data_parameter`
            - `ctr_task_schedule`
        """
        _add_column: dict = merge_dicts(
            self.col_default, {'tacking': 'PROCESSING'}
        )
        for col, default in _add_column.items():
            if col in self.ctr_cols and col not in update_values:
                update_values[col] = default
        _update_values: dict = {
            k: reduce_value(str(v))
            for k, v in update_values.items()
            if k not in self.ctr_pk
        }
        _filter: list = [
            f"{self.name_short}.{_} in {reduce_in_value(update_values[_])}"
            for _ in self.ctr_pk
        ]
        return query_select_row(
            reduce_stm(params.ps_stm.push_ctr_update, add_row_number=False),
            parameters={
                'table_name': self.name,
                'table_name_sht': self.name_short,
                'update_values_pairs': ", ".join([
                    f"{k} = {v}" for k, v in _update_values.items()
                ]),
                'filter': " and ".join(_filter),
                'condition': (
                    f"""and ({condition.replace('"', "'")})"""
                    if condition else ""
                ),
            },
        )


class TblProcess(TblCatalog):
    """Table process object for sync configuration from base config to target
    database and log to Control data logging.
    """

    __slots__ = (
        'tbl_run_date',
        'fwk_parameters',
        'tbl_auto_create',
        'tbl_auto_drop',
        'tbl_auto_init',
        'tbl_ctr_data',
        'tbl_just_create',
        'tbl_just_init',
        'tbl_ps_date',
        'tbl_col_diff',
        'tbl_col_not_equal',
        'tbl_col_update',
        'tbl_col_delete',
        'is_ctr'
    )

    def __init__(
            self,
            tbl_name: str,
            tbl_type: str,
            tbl_run_date: Optional[str] = None,
            fwk_parameters: Optional[dict] = None,
            tbl_auto_create: Union[bool, str] = True,
            tbl_auto_drop: Union[bool, str] = False,
            tbl_auto_init: Union[bool, str] = True,
            verbose: bool = False,
    ):
        """Main Initialization of Table Process object."""
        self.tbl_run_date: dt.date = (
            dt.date.fromisoformat(tbl_run_date)
            if tbl_run_date else get_run_date(date_type='date')
        )
        super().__init__(
            tbl_name=tbl_name,
            tbl_type=tbl_type,
            verbose=verbose
        )
        self.is_ctr: bool = self.tbl_name.startswith("ctr_")
        self.fwk_parameters: dict = fwk_parameters or {}

        # Set table auto parameters
        self.tbl_auto_create: bool = self.validate_tbl_with_flag(tbl_auto_create)
        self.tbl_auto_drop: bool = self.validate_tbl_with_flag(tbl_auto_drop)
        self.tbl_auto_init: bool = self.validate_tbl_with_flag(tbl_auto_init)

        # Set table tracking and cache parameters
        self.tbl_just_create: bool = False
        self.tbl_just_init: int = 0

        # Set table column parameters
        self.tbl_col_diff: bool = False
        self.tbl_col_not_equal: bool = False
        self.tbl_col_update: bool = False
        self.tbl_col_delete: bool = False

        if not self.check_tbl_exists:
            logger.warning(
                f"Table {self.tbl_name} not found in AI Database ..."
            )
            if self.tbl_auto_create:
                self.tbl_just_init: int = self.push_tbl_create(
                    force_drop=self.tbl_auto_drop
                )
                self.tbl_just_create = True
                logger.info(
                    f"Auto create {self.tbl_name!r} in database because "
                    f"`tbl_auto_create` was set be True"
                )
            else:
                raise TableNotFound(
                    "Please set `tbl_auto_create` be True or setup "
                    "this table with API."
                )

        # Pull data from control table.
        # FIXME: if ctr_data_pipeline does not exists it will raise error
        self.tbl_ctr_data: dict = self.pull_tbl_from_ctr_pipeline()

        if not self.tbl_ctr_data:
            if not self.tbl_auto_create:
                raise ControlTableNotExists(
                    f"Table name: {self.tbl_name} does not exists "
                    f"in Control data pipeline"
                )
            logger.info(
                f"Auto insert configuration data to `ctr_data_pipeline` "
                f"for {self.tbl_name!r}"
            )
            self.push_tbl_to_ctr_pipeline()
            self.tbl_ctr_data: dict = self.pull_tbl_from_ctr_pipeline()

        # Generate process date from control data.
        self.tbl_ps_date: dt.date = get_process_date(
            self.tbl_run_date, self.tbl_ctr_run_type, date_type='date'
        )

    @property
    def tbl_parameters(self) -> dict:
        return merge_dicts({
            'run_date': self.tbl_run_date.strftime('%Y-%m-%d'),
            'update_date': get_run_date(fmt='%Y-%m-%d %H:%M:%S'),
        }, self.fwk_parameters)

    @property
    def tbl_ctr_run_count_now(self) -> int:
        return int(float(self.tbl_ctr_data['run_count_now']))

    @property
    def tbl_ctr_run_count_max(self) -> int:
        return int(float(self.tbl_ctr_data['run_count_max']))

    @property
    def tbl_ctr_tbl_type(self) -> str:
        return self.tbl_ctr_data['table_type']

    @property
    def tbl_ctr_run_type(self) -> str:
        return self.tbl_ctr_data['run_type']

    @property
    def tbl_ctr_data_date(self) -> dt.date:
        return dt.date.fromisoformat(self.tbl_ctr_data['data_date'])

    @property
    def tbl_ctr_run_date(self) -> dt.date:
        return dt.date.fromisoformat(self.tbl_ctr_data['run_date'])

    @property
    def tbl_ctr_rtt_value(self) -> int:
        return int(float(self.tbl_ctr_data['rtt_value']))

    @property
    def tbl_ctr_rtt_column(self) -> Union[str, list]:
        try:
            _col: str = self.tbl_ctr_data['rtt_column']
            return (
                ast.literal_eval(_col)
                if (_col.startswith('[') and _col.endswith(']'))
                else _col
            )
        except AttributeError:
            # This error will raise when tbl_ctr_data does not define
            # before call this property.
            return 'undefined'

    @property
    def check_tbl_exists(self) -> bool:
        """Check table exists"""
        return query_select_check(
            params.ps_stm.exists.tbl,
            parameters={"table_name": self.tbl_name},
        )

    def pull_tbl_count(self, condition: Optional[Union[str, list]] = None):
        if condition:
            _condition: list = [condition] if isinstance(condition, str) else condition
            return query_select_row(
                params.ps_stm.pull_count_condition,
                parameters={
                    "table_name": self.tbl_name,
                    "condition": ' and '.join(_condition),
                },
            )
        return query_select_row(
            params.ps_stm.pull_count,
            parameters={"table_name": self.tbl_name}
        )

    def pull_tbl_max_data_date(self, default: bool = True) -> Optional[dt.date]:
        """Pull max data date that use the retention column for sorting"""
        _default_value: Optional[dt.date] = (
            dt.datetime.strptime('1990-01-01', '%Y-%m-%d').date()
            if default else None
        )
        if self.tbl_ctr_rtt_column == 'undefined':
            return _default_value

        return dt.date.fromisoformat(
            query_select_one(
                params.ps_stm.pull_max_data_date,
                parameters={
                    "table_name": self.tbl_name,
                    'ctr_rtt_col': self.tbl_ctr_rtt_column
                }
            ).get('max_date', _default_value)
        )

    def pull_tbl_retention_date(
            self, rtt_mode: str,
            rtt_date_type: Optional[str] = None,
    ) -> dt.date:
        """Pull min retention date with mode, like `data_date` or `run_date`"""
        _input_date: dt.date = (
            self.pull_tbl_max_data_date()
            if rtt_mode == 'data_date' else self.tbl_run_date
        )
        _run_type: str = (rtt_date_type or 'monthly')
        return get_process_date(run_date=get_cal_date(
            data_date=_input_date,
            mode='sub',
            run_type=_run_type,
            cal_value=self.tbl_ctr_rtt_value,
            date_type='date'
        ), run_type=_run_type, date_type='date')

    def pull_tbl_columns_datatype(self) -> dict:
        """Pull name and data type of all columns of table in database
        """
        return {
            rows['column_name']: {
                'order': int(rows['ordinal_position']),
                'datatype': rows['data_type'],
                'nullable': eval(rows['nullable'])
            }
            for rows in query_select(
                params.ps_stm.pull_columns_datatype,
                parameters={"table_name": self.tbl_name}
            )
        }

    def pull_tbl_from_ctr_pipeline(self) -> dict:
        """Pull configuration data from the Control Data Pipeline"""
        try:
            return Control('ctr_data_pipeline').pull(pm_filter=[self.tbl_name])
        except ProgrammingError:
            logger.warning("control table `ctr_data_pipeline` does not exits")
            return {}

    def pull_tbl_from_ctr_logging(self, action_type: str, all_flag: bool = False):
        """Pull logging data from the Control Data Logging"""
        return (
            Control('ctr_data_logging')
                .pull(
                    pm_filter={
                        'table_name': self.tbl_name,
                        'run_date': (
                            self.tbl_run_date.strftime('%Y-%m-%d')
                            if not all_flag else '*'
                        ),
                        'action_type': action_type
                    },
                    all_flag=all_flag
                )
        )

    def push_tbl_drop(
        self,
        cascade: bool = False,
        execute: bool = True,
    ) -> Optional[str]:
        row_num: int = self.pull_tbl_count()
        _del_log: str = (
            f"with {row_num} row{get_plural(row_num)} " if row_num > 0 else ''
        )
        logger.info(f"Drop table {self.tbl_name!r} {_del_log}successful")
        if not execute:
            return self.get_tbl_stm_drop(cascade=cascade)
        query_execute(self.get_tbl_stm_drop(cascade=cascade), parameters=True)

    def push_tbl_drop_partition(self):
        ...

    def push_tbl_create(
            self,
            force_drop: bool = False,
            cascade: bool = False
    ) -> int:
        """Push create table"""
        if self.tbl_just_create:
            self.tbl_just_create: bool = False
            return self.tbl_just_init

        _stm_all: list = [self.get_tbl_stm_create()]
        row_num: int = 0
        if force_drop:
            row_num: int = self.pull_tbl_count()
            _del_log: str = (
                f"with {row_num} row{get_plural(row_num)} "
                if row_num > 0 else ''
            )
            _stm_all.insert(0, self.get_tbl_stm_drop(cascade=cascade))
            logger.info(
                f"Drop table {self.tbl_name!r} {_del_log}"
                f"before create successful"
            )
            self.update_tbl_to_ctr_pipeline(
                update_values={
                    'data_date': (
                        Control
                            .parameters('framework')
                            .get('data_setup_initial_date', '2018-10-31')
                    ),
                }
            )

        query_transaction(_stm_all, parameters=True)

        if (
                self.tbl_auto_init
                and force_drop
                and (init := self.tbl_initial)
        ):
            _start_time: dt.datetime = get_time_checkpoint()
            row_num: int = self.push_tbl_process(init, force_sql=True)
            self.push_tbl_to_ctr_logging(push_values={
                'action_type': 'initial',
                'row_record': row_num,
                'process_time': round((get_time_checkpoint() - _start_time).total_seconds()),
                'status': 0
            })
            self.update_tbl_to_ctr_pipeline(
                update_values={
                    'data_date': self.pull_tbl_max_data_date().strftime('%Y-%m-%d')
                }
            )
            logger.info(
                f"Success initial value after create with {row_num} row"
                f"{get_plural(row_num)}"
            )
        return row_num

    def push_tbl_create_bk(self):
        ...

    def push_tbl_create_partition(self):
        ...

    def push_tbl_insert(self, columns: list, value: str) -> int:
        """Push Insert values to target table
        :warning: columns must have primary key
        """
        return query_execute_row(self.get_tbl_stm_ingest(), parameters={
            'string_columns': ', '.join(columns),
            'string_values': value
        })

    def push_tbl_update(self, columns: list, value: str) -> int:
        """Push Update values to target table
        :warning: columns must have primary key
        """
        _map_columns_type: dict = {
            col: attrs['datatype']
            for col, attrs in self.get_tbl_columns(
                pk_included=True,
                datatype_included=True,
            ).items()
        }
        string_columns_pairs: str = ", ".join(
            [
                f"{col} = {self.tbl_name_sht}_ud.{col}"
                f"::{_map_columns_type[col]}"
                for col in columns
                if col not in self.tbl_primary_key
            ]
        )
        return query_execute_row(
            self.get_tbl_stm_update(),
            parameters={
                'string_columns': ', '.join(columns),
                'string_columns_pairs': string_columns_pairs,
                'string_values': value
            }
        )

    def push_tbl_ingestion(
            self,
            payloads: list,
            mode: str,
            action: str,
            update_date: dt.datetime,
    ) -> Tuple[int, int]:
        ps_row_success: int = 0
        ps_row_failed: int = 0
        _start_time: dt.datetime = get_time_checkpoint()
        self.push_tbl_to_ctr_logging(push_values={
            'data_date': update_date.strftime('%Y-%m-%d'),
            'update_date': update_date.strftime('%Y-%m-%d %H:%M:%S'),
            'action_type': 'ingestion',
        })

        # Note: Chuck size for merge mode will be split from the first level
        # of payloads.
        for index, data in enumerate(
                split_iterable(payloads, settings.APP_INGEST_CHUCK),
                start=1,
        ):
            try:
                cols, values = Value(
                    values=data,
                    update_date=update_date.strftime('%Y-%m-%d %H:%M:%S'),
                    mode=mode,
                    action=action,
                    expected_cols=self.get_tbl_columns(
                        pk_included=True,
                        datatype_included=True
                    ),
                    expected_pk=self.tbl_primary_key
                ).generate()
                row_ingest: int = (
                    self.push_tbl_update(cols, values)
                    if action == 'update'
                    else self.push_tbl_insert(cols, values)
                )
                _failed: int = 0
                if row_ingest != (_row_generate := (values.count('), (') + 1)):
                    _failed: int = (_row_generate - row_ingest)
                    ps_row_failed += _failed
                ps_row_success += row_ingest
                logger.info(
                    f"Ingest chunk {index:02d}: "
                    f"(success: {row_ingest} row{get_plural(row_ingest)},"
                    f"false: {_failed} row{get_plural(_failed)})"
                )
                self.update_tbl_to_ctr_logging(update_values={
                    'action_type': 'ingestion',
                    'row_record': {1: ps_row_success, 2: ps_row_failed},
                    'process_time': round(
                        (get_time_checkpoint() - _start_time).total_seconds()
                    ),
                    'status': 0
                })
            except ObjectBaseError as err:
                self.update_tbl_to_ctr_logging(update_values={
                    'action_type': 'ingestion',
                    'row_record': {1: ps_row_success, 2: ps_row_failed},
                    'process_time': round(
                        (get_time_checkpoint() - _start_time).total_seconds()
                    ),
                    'status': 1
                })
                logger.error(f"Error: {err.__class__.__name__}: {str(err)}")
                raise err
        return ps_row_success, ps_row_failed

    def push_tbl_diff(self):
        # Get mapping of different columns between config table
        # and target table.
        get_cols, pull_cols, compare = self._generate_tbl_columns_diff()
        merge_cols: dict = self._generate_tbl_column_diff_map(
            get_cols, pull_cols
        )
        try:
            if self.tbl_col_update:
                self.push_tbl_diff_update(get_cols, compare['left'])
            elif self.tbl_col_delete:
                self.push_tbl_diff_delete(compare['right'])
            elif self.tbl_col_diff:
                self.push_tbl_diff_merge(merge_cols)
            else:
                logger.info(
                    f"Dose not any change from configuration data "
                    f"in table {self.tbl_name!r}"
                )
        except DatabaseProcessError as err:
            logger.error(
                f"Push different columns process of {self.tbl_name!r} "
                f"failed with {err}"
            )

    def push_tbl_diff_update(self, get_cols: dict, not_exists_cols: set):
        """Update column properties of target table in database
        TODO: ALTER [ COLUMN ] column { SET | DROP } NOT NULL
        """
        logger.info(
            f"Update column properties or add new column{get_plural(len(not_exists_cols))}, "
            f"{', '.join([repr(_) for _ in not_exists_cols])} of table {self.tbl_name!r} in database"
        )
        add_col_list = [
            (name, props['datatype'])
            for name, props in get_cols.items()
            if name in not_exists_cols
        ]
        print(', '.join(f'add column {col[0]} {col[1]}' for col in add_col_list))
        # query_execute(statement=params.ps_stm.alter, parameters={
        #     'table_name': self.tbl_name,
        #     'action': ', '.join(f'add column {col[0]} {col[1]}' for col in add_col_list)
        # })

    def push_tbl_diff_delete(self, exists_cols: set):
        """Delete column in target table with not exists from configuration data
        """
        logger.info(
            f"Drop column{get_plural(len(exists_cols))}, "
            f"{', '.join([repr(_) for _ in exists_cols])} of table {self.tbl_name!r} in database"
        )
        print(', '.join(f'drop column {col}' for col in exists_cols))
        # query_execute(statement=params.ps_stm.alter, parameters={
        #     'table_name': self.tbl_name,
        #     'action': ', '.join(f'drop column {col}' for col in exists_cols)
        # })

    def push_tbl_diff_merge(self, merge_cols: dict):
        """Transfer full data from old table to new created table from merge columns
        """
        mapping_col_insert: list = []
        mapping_col_select: list = []
        for col_name, col_attrs in merge_cols.items():
            mapping_col_insert.append(col_name)
            if _not_match := col_attrs['not_match']:
                stm_select: str = col_name

                # Check nullable not match
                if _nullable := _not_match.get('nullable'):
                    # Generate default value with data-type
                    if not _nullable[0] and _nullable[1]:
                        # stm_select: str = f"coalesce({col_name}, {default})"
                        raise TableNotImplement(
                            f"Table column different merge process does not "
                            f"support for change `null` to `not null` "
                            f"with column: {col_name!r} in {self.tbl_name!r}"
                        )

                # Check data-type not match
                if _datatype := _not_match.get('datatype'):
                    stm_select: str = f"{stm_select}::{_datatype[0]} as {col_name}"

                mapping_col_select.append(stm_select)
                continue
            mapping_col_select.append(col_name)

        logger.info(
            f"insert into {{database_name}}.{{ai_schema_name}}.{self.tbl_name} \n"
            f"\t\t ( {', '.join(mapping_col_insert)} ) \n"
            f"\t\t select {', '.join(mapping_col_select)} \n"
            f"\t\t from {{database_name}}.{{ai_schema_name}}.{self.tbl_name}_old;"
        )
        # query_execute(statement=params.ps_stm.push_merge, parameters={
        #     'table_name': self.tbl_name,
        #     'mapping_insert': ', '.join(mapping_col_insert),
        #     'mapping_select': ', '.join(mapping_col_select)
        # })

    def push_tbl_vacuum(self, option: Optional[list] = None) -> None:
        _option: str = ', '.join(option) if option else 'full'
        return query_execute(
            reduce_stm(params.ps_stm.push_vacuum),
            parameters={'table_name': self.tbl_name, 'option': _option}
        )

    def push_tbl_del_with_condition(self, condition: str) -> int:
        return query_select_row(
            reduce_stm(params.ps_stm.push_del_with_condition, add_row_number=False),
            parameters={'table_name': self.tbl_name, 'condition': condition}
        )

    def push_tbl_del_with_date(self, delete_date: str, delete_mode: Optional[str] = None) -> int:
        _primary_key: list = self.tbl_primary_key
        _primary_key_group: str = ','.join([str(_) for _ in range(1, len(_primary_key) + 1)])
        _primary_key_mark_a: str = ','.join([f'a.{col}' for col in _primary_key])
        _primary_key_join_a_and_b: str = 'on ' + ' and '.join([f'a.{col} = b.{col}' for col in _primary_key])
        if self.tbl_ctr_tbl_type == 'master' and delete_mode == 'rtt':
            _stm: str = reduce_stm(params.ps_stm.push_del_with_date.master_rtt)
        elif self.tbl_ctr_tbl_type != 'master':
            _stm: str = reduce_stm(params.ps_stm.push_del_with_date.not_master)
        else:
            return 0
        return query_select_row(_stm, parameters={
            'ctr_rtt_col': self.tbl_ctr_rtt_column,
            'ctr_rtt_value': self.tbl_ctr_rtt_value,
            'del_operation': ('<' if delete_mode == 'rtt' else '>='),
            'del_date': delete_date,
            'table_name': self.tbl_name,
            'primary_key': ", ".join(_primary_key),
            'primary_key_group': _primary_key_group,
            'primary_key_mark_a': _primary_key_mark_a,
            'primary_key_join_a_and_b': _primary_key_join_a_and_b,
        })

    def push_tbl_process(
            self,
            process: dict,
            force_sql: bool = False,
            additional: Optional[dict] = None
    ) -> int:
        _additional: dict = additional or {}
        parameters: dict = self._generate_params(process['parameter'], additional=_additional)

        if (self.tbl_type == 'sql') or force_sql:
            # Push table process with `sql` type
            return query_select_row(statement=process['statement'], parameters=parameters)

        # Push table process with `func` type
        _func: callable = process.get('function')
        _func_parameters: list = list(inspect.signature(_func).parameters.keys())

        if not (_input_key := only_one(_func_parameters, params.map_func.input, default=False)):
            raise TableNotImplement(
                f"Process function: {_func.__name__} of {self.tbl_name!r} "
                f"does not have input argument like `input_df`"
            )
        _input_df: pd.DataFrame = query_select_df(
            statement=process['load']['statement'],
            parameters=self._generate_params(process['load']['parameter'], additional=_additional)
        )
        if _input_df.empty:
            logger.warning("Input DataFrame of function was empty")
            return 0
        _func_value: str = self.validate_func_output_type(_func(**{_input_key: _input_df}, **parameters))
        if not _func_value:
            logger.warning("Output string of `function_value` was empty")
            return 0
        return query_select_row(
            statement=process['save']['statement'],
            parameters=self._generate_params(
                process['save']['parameter'],
                additional=merge_dicts(_additional, {'function_value': _func_value})
            )
        )

    def validate_func_output_type(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        elif isinstance(value, pd.DataFrame):
            raise TableNotImplement(
                f"Output of process function in {self.tbl_name!r} "
                f"does not support for DataFrame type yet."
            )
        raise TableArgumentError(
            f"Output of process function in {self.tbl_name!r} "
            f"does not support for {type(value)!r} type."
        )

    def push_tbl_processes(
            self,
            ps_included: list,
            ps_excluded: list,
            ps_act_type: str,
            ps_params: Optional[dict] = None,
            raise_if_error: bool = True
    ) -> dict:
        """Push all table processes to target database
        """
        _row_record: dict = {1: 0}
        _start_time: dt.datetime = get_time_checkpoint()
        self.push_tbl_to_ctr_logging(push_values={
            'run_date': ps_params.get('run_date', self.tbl_run_date.strftime('%Y-%m-%d')),
            'data_date': ps_params.get('data_date', self.tbl_run_date.strftime('%Y-%m-%d')),
            'action_type': ps_act_type
        })
        for index, (ps_name, ps_props) in enumerate(self.tbl_process.items(), start=1):
            if (
                    (
                            ps_name.lower().startswith('mockup_data') and
                            self.tbl_parameters.get('data_normal_common_filter_mockup', 'N') == 'Y'
                    ) or
                    ps_name in ps_excluded or
                    ps_name not in ps_included
            ):
                logger.info(f"Filter {self.tbl_type.upper()} process name: {ps_name!r}")
                _row_record[index] = 0
                continue
            logger.info(f"Priority {index:02d}: {self.tbl_type.upper()} process name: {ps_name!r}")
            try:
                _row_record[index] = self.push_tbl_process(ps_props, additional=ps_params)
                logger.info(
                    f'Success with running process with {_row_record[index]} row{get_plural(_row_record[index])}'
                )
                self.update_tbl_to_ctr_logging(update_values={
                    'run_date': ps_params.get('run_date', self.tbl_run_date.strftime('%Y-%m-%d')),
                    'action_type': ps_act_type,
                    'row_record': reduce_text(str(_row_record)) if len(_row_record) > 1 else _row_record[1],
                    'process_time': round((get_time_checkpoint() - _start_time).total_seconds()),
                    'status': 0
                })
            except DatabaseProcessError as err:
                _row_record[index] = 0
                self.update_tbl_to_ctr_logging(update_values={
                    'run_date': ps_params.get('run_date', self.tbl_run_date.strftime('%Y-%m-%d')),
                    'action_type': ps_act_type,
                    'row_record': reduce_text(str(_row_record)) if len(_row_record) > 1 else _row_record[1],
                    'process_time': round((get_time_checkpoint() - _start_time).total_seconds()),
                    'status': 1
                })
                if raise_if_error:
                    raise err
                logger.error(f"Error: {err.__class__.__name__}: {str(err)}")
                break
        return _row_record

    def push_tbl_backup(
            self,
            backup_name: str,
            backup_schema: Optional[str] = None,
            raise_if_error: bool = True
    ) -> int:
        _start_time: dt.datetime = get_time_checkpoint()
        self.push_tbl_to_ctr_logging(push_values={'action_type': 'backup'})
        _stm_all: list = [
            self.get_tbl_stm_create_bk(tbl_name_bk=backup_name),
            reduce_stm(params.ps_stm.push_backup),
        ]
        _schema_name_bk: str = env.get('AI_SCHEMA', 'ai')
        if backup_schema and ((backup_schema or '') != _schema_name_bk):
            if check_schema_exists(schema_name=_schema_name_bk):
                logger.info(
                    f"Create new backup schema: {_schema_name_bk!r} to database"
                )
                _stm_all.insert(0, reduce_stm(params.bs_stm.create.schema))
            _schema_name_bk: str = backup_schema
        logger.info(f"Create backup table: '{_schema_name_bk}.{backup_name}'")
        try:
            _backup_row: int = query_transaction(_stm_all, parameters={
                'table_name': self.tbl_name,
                'schema_name': _schema_name_bk,
                'ai_schema_name_backup': _schema_name_bk,
                'table_name_backup': backup_name
            })
            logger.info(
                f"Backup {self.tbl_name} to '{_schema_name_bk}.{backup_name}' "
                f"successful with {_backup_row} row{get_plural(_backup_row)}"
            )
            self.update_tbl_to_ctr_logging(update_values={
                'action_type': 'backup',
                'row_record': _backup_row,
                'process_time': round((get_time_checkpoint() - _start_time).total_seconds()),
                'status': 0
            })
        except DatabaseProcessError as err:
            _backup_row: int = 0
            self.update_tbl_to_ctr_logging(update_values={
                'action_type': 'backup',
                'process_time': round((get_time_checkpoint() - _start_time).total_seconds()),
                'status': 1
            })
            if raise_if_error:
                raise err
            logger.error(f"Error: {err.__class__.__name__}: {str(err)}")
        return _backup_row

    def push_tbl_retention(
            self,
            rtt_date: dt.date
    ) -> int:
        _start_time: dt.datetime = get_time_checkpoint()
        self.push_tbl_to_ctr_logging(push_values={
            'data_date': rtt_date.strftime('%Y-%m-%d'),
            'action_type': 'retention'
        })
        try:
            _rtt_row: int = self.push_tbl_del_with_date(
                delete_date=rtt_date.strftime('%Y-%m-%d'),
                delete_mode='rtt'
            )
            logger.info(
                f"Success Delete data_date that less than {rtt_date:%Y-%m-%d} "
                f"with {_rtt_row} row{get_plural(_rtt_row)}"
            )
            self.update_tbl_to_ctr_logging(update_values={
                'action_type': 'retention',
                'row_record': _rtt_row,
                'process_time': round((get_time_checkpoint() - _start_time).total_seconds()),
                'status': 0
            })
        except DatabaseProcessError as err:
            _rtt_row: int = 0
            self.update_tbl_to_ctr_logging(update_values={
                'action_type': 'retention',
                'process_time': round((get_time_checkpoint() - _start_time).total_seconds()),
                'status': 1
            })
            raise err
        return _rtt_row

    def push_tbl_to_ctr_logging(self, push_values: Optional[dict] = None):
        """Push data information to the Control Data Logging"""
        _push_values: dict = merge_dicts({
            'table_name': self.tbl_name,
            'data_date': self.tbl_run_date.strftime('%Y-%m-%d'),
            'run_date': self.tbl_run_date.strftime('%Y-%m-%d'),
            'action_type': 'common',
            'row_record': 0,
            'process_time': 0,
        }, (push_values or {}))
        return Control('ctr_data_logging').push(push_values=_push_values)

    def update_tbl_to_ctr_logging(self, update_values: Optional[dict] = None):
        """Update data information to the Control Data Logging"""
        _update_values: dict = merge_dicts({
            'table_name': self.tbl_name,
            'run_date': self.tbl_run_date.strftime('%Y-%m-%d'),
            'action_type': 'common',
        }, (update_values or {}))
        return Control('ctr_data_logging').update(update_values=_update_values)

    def push_tbl_to_ctr_pipeline(self, push_values: Optional[dict] = None):
        """Push data information to the Control Data Logging"""
        _push_values: dict = merge_dicts({
            'system_type': params.map_tbl_sys.get(self.tbl_prefix, 'undefined'),
            'table_name': self.tbl_name,
            'table_type': 'undefined',
            'data_date': self.tbl_run_date.strftime('%Y-%m-%d'),
            'run_date': self.tbl_run_date.strftime('%Y-%m-%d'),
            'run_type': self._generate_run_type(),
            'run_count_now': 0,
            'run_count_max': 0,
            'rtt_value': 0,
            'rtt_column': 'undefined'
        }, (push_values or {}))
        return Control('ctr_data_pipeline').push(push_values=_push_values)

    def update_tbl_to_ctr_pipeline(
            self,
            update_values: Optional[dict] = None) -> int:
        """Update data information to the Control Data Pipeline"""
        _update_values: dict = merge_dicts(
            # Default value if does not parsing updated values.
            {
                'table_name': self.tbl_name,
            },
            # Merge with parsing updated values.
            (update_values or {})
        )
        try:
            return Control('ctr_data_pipeline').update(
                update_values=_update_values
            )
        except DatabaseProcessError as err:
            logger.warning(
                f'Does not update control data pipeline table because of, \n'
                f'{err}'
            )
            return 0

    def _generate_params(
            self, parameters: list, additional: Optional[dict] = None
    ) -> dict:
        """Generate parameters which filter `tbl_parameters` with list of
        config parameters
        """
        _full_parameters: dict = merge_dicts(self.tbl_parameters, (additional or {}))
        return {param: _full_parameters[param] for param in parameters}

    def _generate_run_type(self) -> str:
        run_types: dict = params.map_tbl_run_type.copy()
        return next(
            (
                run_types[col]
                for col in self.get_tbl_columns(pk_included=True)
                if col in run_types
            ),
            'daily'
        )

    def _generate_tbl_columns_diff(self) -> Tuple[dict, ...]:
        _pull_cols: dict = self.pull_tbl_columns_datatype()
        _get_cols: dict = self.get_tbl_columns(
            pk_included=True, datatype_included=True
        )

        compare_diff: dict = {'left': set(), 'right': set(), 'all': set()}
        compare_diff['left'].update(set(_get_cols.keys()).difference(set(_pull_cols.keys())))
        compare_diff['right'].update(set(_pull_cols.keys()).difference(set(_get_cols.keys())))
        compare_diff['all'].update(set(_get_cols.keys()).intersection(set(_pull_cols.keys())))

        if len(_get_cols) != len(_pull_cols) or sorted(_get_cols.keys()) != sorted(_pull_cols.keys()):
            self.tbl_col_not_equal: bool = True
            if not compare_diff['right']:
                self.tbl_col_update: bool = True
            elif not compare_diff['left']:
                self.tbl_col_delete: bool = True

        return _get_cols, _pull_cols, compare_diff

    def _generate_tbl_column_diff_map(self, _get_cols: dict, _pull_cols: dict) -> dict:
        """Generate mapping of different matrix of column properties
        """
        results: dict = {}

        if self.tbl_col_not_equal:
            _get_cols: dict = {
                k: _get_cols[k]
                for k in _get_cols
                if k in _pull_cols
            }

        for col_name, col_attrs in _get_cols.items():
            result: dict = {'match': {}, 'not_match': {}}
            for _check in {'order', 'datatype', 'nullable'}:
                if _pull_cols[col_name][_check] != col_attrs[_check]:
                    self.tbl_col_diff: bool = True
                    if _check == 'order':
                        # Cannot use update or delete because order of column
                        # does not align with config data
                        self.tbl_col_update: bool = False
                        self.tbl_col_delete: bool = False
                    result['not_match'][_check] = (
                        col_attrs[_check], _pull_cols[col_name][_check]
                    )
                else:
                    result['match'][_check] = (
                        col_attrs[_check], _pull_cols[col_name][_check]
                    )
                results[col_name]: dict = result
        return results


class FuncProcess(FuncCatalog):
    """Function process object for sync configuration from base config to target
    database
    """

    __slots__ = (
        'func_run_date',
        'func_auto_create',
        'func_just_create',
        'func_auto_drop',
        'fwk_parameters',
    )

    def __init__(
            self,
            func_name: str,
            func_type: str,
            func_run_date: Optional[str] = None,
            fwk_parameters: Optional[dict] = None,
            func_auto_create: Union[bool, str] = True,
            func_auto_drop: Union[bool, str] = False
    ):
        self.func_run_date: dt.date = dt.date.fromisoformat(func_run_date) if func_run_date \
            else get_run_date(date_type='date')
        super().__init__(
            func_name=func_name,
            func_type=func_type
        )
        self.func_auto_create: bool = must_bool(func_auto_create)
        self.func_auto_drop: bool = must_bool(func_auto_drop)
        self.fwk_parameters: dict = fwk_parameters or {}
        self.func_just_create: bool = False
        if self.func_type in {'query', }:
            try:
                _ = self.check_func_exists
            except DatabaseProcessError as err:
                raise FuncRaiseError(
                    f"{self.func_name!r} was raised the statement error "
                    f"from query explain checking"
                ) from err
        elif (
                not self.check_func_exists
                and self.func_type in {'func', 'view', 'mview', }
        ):
            if self.func_auto_create:
                self.push_func_create(force_drop=self.func_auto_drop)
                self.func_just_create: bool = True
            else:
                raise FuncNotFound(
                    f"Table {self.func_name} not found in the AI Database, "
                    f"Please set `func_auto_create`."
                )

    @property
    def func_parameters(self) -> dict:
        return merge_dicts({
            'run_date': self.func_run_date.strftime('%Y-%m-%d'),
            'update_date': get_run_date(fmt='%Y-%m-%d %H:%M:%S'),
        }, self.fwk_parameters)

    @property
    def check_func_exists(self) -> bool:
        """Check function exists in database"""
        if self.func_type in {'func', 'view', 'mview', }:
            return query_select_check(params.ps_stm.exists.func, parameters={"func_name": self.func_name})
        elif self.func_type == 'query':
            query_explain(self.get_func_stm_create(), parameters=self._generate_params(self.func_profile['parameter']))
        return False

    def push_func_create(self, force_drop: bool = False, cascade: bool = False) -> Optional[dict]:
        if self.func_just_create:
            self.func_just_create: bool = False
            return

        _stm_all: list = [self.get_func_stm_create()]
        if force_drop:
            logger.info(f"Drop function {self.func_name!r} before create successful")
            _stm_all.insert(0, self.get_func_stm_drop(cascade=cascade))
        query_transaction(_stm_all, parameters=self._generate_params(self.func_profile['parameter']))

    def push_query(self, limit: int = 10):
        if self.func_type not in {'query', 'view', 'mview'}:
            raise FuncRaiseError(f"Function type {self.func_type!r} does not support for query method")

        stm = Statement(self.get_func_stm_create())
        if stm.stm_type == 'dql':
            return {
                data[0]: data[1]
                for data in itertools.takewhile(
                    lambda x: x[0] <= limit,
                    enumerate(query_select(
                        stm.generate(),
                        parameters=self._generate_params(self.func_profile['parameter']),
                    ), start=1, )
                )
            }
        elif stm.stm_type == 'dml':
            return {1: {
                'row_number': query_select_row(
                    stm.generate(),
                    parameters=self._generate_params(self.func_profile['parameter'])
                )
            }}
        else:
            raise FuncArgumentError(
                f"Function {self.func_name!r} does not support `push_query` with statement type {stm.stm_type!r}"
            )

    def _generate_params(self, parameters: list, additional: Optional[dict] = None) -> dict:
        _full_parameters: dict = merge_dicts(self.func_parameters, (additional or {}))
        try:
            return {param: _full_parameters[param] for param in parameters}
        except KeyError as k:
            raise FuncArgumentError(
                f"Function {self.func_name!r} does not set parameter for {k}"
            ) from k


def check_run_mode(obj: str, run_mode: Optional[str] = None):
    """Checking function of `run_mode` argument that correct with it mapping
    """
    if not run_mode:
        return params.list_run_mode[obj][0]
    if run_mode not in params.list_run_mode[obj]:
        raise ProcessValueError(
            f"Process `run_mode` is {run_mode!r} does not support"
        )


class Action(FuncProcess):
    """Action object for control process of function object
    """

    __slots__ = (
        'act_func_type', 'act_func_name', 'act_func_params', 'act_func_run_mode',
        'act_start_datetime', 'act_func_ps_id'
    )

    def __init__(
            self,
            name: str,
            process_id: Optional[str] = None,
            run_mode: Optional[str] = None,
            run_date: Optional[str] = None,
            auto_create: Union[bool, str] = True,
            auto_drop: Union[bool, str] = False,
            external_parameters: Optional[dict] = None,
            fwk_parameters: Optional[dict] = None,
    ):
        self.act_func_type, self.act_func_name = filter_ps_type(name)
        self.act_func_ps_id: str = process_id or get_process_id("undefined")
        self.act_func_run_mode: str = check_run_mode('action', run_mode)
        self.act_func_params: dict = fwk_parameters or merge_dicts(
            Control.parameters(), (external_parameters or {})
        )
        super().__init__(
            func_name=self.act_func_name,
            func_type=self.act_func_type,
            func_run_date=run_date,
            fwk_parameters=self.act_func_params,
            func_auto_create=auto_create,
            func_auto_drop=auto_drop,
        )
        self.act_start_datetime: dt.datetime = get_time_checkpoint()

    @property
    def name(self) -> str:
        return self.func_name

    @property
    def process_time(self) -> int:
        return round((get_time_checkpoint() - self.act_start_datetime).total_seconds())


class Node(TblProcess):
    """Node object for control process of table object and log to Control task process
    """

    @classmethod
    def convert_short(cls, name_short: str) -> str:
        return TblCatalog.short(name_short).tbl_name

    __slots__ = (
        'node_tbl_type',
        'node_tbl_run_mode',
        'node_tbl_ps_included',
        'node_tbl_params',
        'node_start_datetime',
        'node_tbl_run_check',
        'node_tbl_name',
        'node_tbl_ps_excluded',
        'node_tbl_ps_id',
        'verbose'
    )

    def __init__(
            self,
            name: str,
            process_id: Optional[str] = None,
            run_mode: Optional[str] = None,
            run_date: Optional[str] = None,
            choose: Optional[list] = None,
            auto_create: Union[bool, str] = True,
            auto_drop: Union[bool, str] = False,
            auto_init: Union[bool, str] = True,
            external_parameters: Optional[dict] = None,
            fwk_parameters: Optional[dict] = None,
            verbose: bool = False,
    ):
        self.node_tbl_type, self.node_tbl_name = filter_ps_type(name)
        self.node_tbl_ps_id: str = process_id or get_process_id("undefined")
        _node_tbl_ps_included, self.node_tbl_ps_excluded = split_choose(
            choose or []
        )
        self.node_tbl_run_mode: str = check_run_mode('node', run_mode)
        self.verbose = verbose
        verbose_log(
            self,
            f"[Start] initialize the node object name {self.node_tbl_name} ..."
        )
        verbose_log(
            self,
            (
                "Mapping parameter from the control table and external "
                "parameter argument together ..."
            ),
        )
        self.node_tbl_params: dict = (fwk_parameters or merge_dicts(
            Control.parameters(), (external_parameters or {})
        ))
        super().__init__(
            tbl_name=self.node_tbl_name,
            tbl_type=self.node_tbl_type,
            tbl_run_date=run_date,
            fwk_parameters=self.node_tbl_params,
            tbl_auto_create=auto_create,
            tbl_auto_drop=auto_drop,
            tbl_auto_init=auto_init,
            verbose=verbose,
        )
        self.node_tbl_ps_included: list = [
            _ for _ in self.tbl_process.keys() if _ in _node_tbl_ps_included
        ] if _node_tbl_ps_included else list(self.tbl_process.keys())
        self.node_start_datetime: dt.datetime = get_time_checkpoint()
        self.node_tbl_run_check: bool = (
            self.tbl_ctr_run_count_now < self.tbl_ctr_run_count_max
            or self.tbl_ctr_run_count_max == 0
        )
        if (
                self.tbl_run_date < self.tbl_ctr_run_date
                and self.node_tbl_run_mode == 'common'
        ):
            raise ControlTableValueError(
                f"Please check value of `run_date`, "
                f"which less than the current running date: "
                f"{self.tbl_ctr_run_date:'%Y-%m-%d'}"
            )
        verbose_log(
            self,
            f"[Success] initialize the node object name {self.node_tbl_name}",
            end='=',
        )

    @property
    def name(self) -> str:
        return self.tbl_name

    @property
    def run_date(self):
        return self.tbl_run_date

    @property
    def auto_drop(self) -> bool:
        return self.tbl_auto_drop

    @property
    def auto_init(self) -> bool:
        return self.tbl_auto_init

    @auto_init.setter
    def auto_init(self, value: bool):
        self.tbl_auto_init: bool = value

    @property
    def cascade(self) -> bool:
        return (
                (self.node_tbl_params.get('cascade', 'N') == 'Y') and
                (self.node_tbl_params.get("permission_force_delete", 'N') == 'Y')
        )

    @property
    def quota(self) -> bool:
        return (
                not self.node_tbl_run_check
                and (self.tbl_run_date == self.tbl_ctr_run_date)
        )

    @property
    def process_time(self) -> int:
        return round(
            (get_time_checkpoint() - self.node_start_datetime).total_seconds()
        )

    @property
    def process_count(self) -> int:
        _excluded: list = self.node_tbl_ps_excluded
        if _included := self.node_tbl_ps_included:
            return (
                    len(_included)
                    - len(set(_included).intersection(set(_excluded)))
            )
        return self.tbl_process_count - len(_excluded)

    def _prepare_before_rerun(self, sla: int) -> Tuple[dt.date, dt.date]:
        _run_date: dt.date = (
            self.tbl_run_date
            if self.node_tbl_run_mode == 'common'
            else self.tbl_ps_date
        )
        _data_date: dt.date = self.tbl_ctr_data_date
        if self.node_tbl_run_mode == 'common':
            return _run_date, _data_date

        if self.tbl_run_date < self.tbl_ctr_data_date:
            _run_date: dt.date = self.tbl_ps_date
            _data_date: dt.date = get_cal_date(
                data_date=self.tbl_ps_date,
                mode='sub',
                run_type=self.tbl_ctr_run_type,
                cal_value=(1 + sla),
                date_type='date'
            )
        if (
                self.node_tbl_params.get(
                    'data_normal_rerun_reset_data', 'N'
                ) == 'Y'
                and self.tbl_ctr_tbl_type == 'transaction'
        ):
            del_record: int = self.push_tbl_del_with_date(
                delete_date=_run_date.strftime('%Y-%m-%d'),
                delete_mode=self.node_tbl_run_mode
            )
            logger.info(f'Delete current data transaction: {del_record} rows')
        logger.info(
            f'Reset (data_date, run_date) from '
            f'({self.tbl_ctr_data_date:%Y-%m-%d}, '
            f'{self.tbl_run_date:%Y-%m-%d}) to '
            f'({_data_date:%Y-%m-%d}, {_run_date:%Y-%m-%d})'
        )
        return _run_date, _data_date

    def process_run_count(self, row_record: dict):
        return int(float(self.tbl_ctr_run_count_now)) + 1 if (
                self.tbl_run_date == self.tbl_ctr_run_date and
                max(row_record.values(), default=0) != 0
        ) else 0

    def process_start(self) -> dict:
        _additional: dict = self.node_tbl_params.copy()
        if self.node_tbl_params.get('data_normal_rerun_reset_sla', 'N') == 'Y':
            _ps_sla: int = 0
            for key, value in _additional.items():
                if (
                        key in params.map_tbl_ps_sla.values()
                        and isinstance(value, int)
                ):
                    _additional[key] = 0
                logger.warning(f'Reset `{key}` parameter values to 0')
        else:
            _ps_sla: int = _additional.get(
                params.map_tbl_ps_sla[self.tbl_ctr_run_type], 1
            )
        _run_date, _data_date = self._prepare_before_rerun(sla=_ps_sla)
        _row_record: dict = self.push_tbl_processes(
            ps_included=self.node_tbl_ps_included,
            ps_excluded=self.node_tbl_ps_excluded,
            ps_act_type=self.node_tbl_run_mode,
            ps_params=merge_dicts(
                _additional, {
                    'run_date': _run_date.strftime('%Y-%m-%d'),
                    'data_date': _data_date.strftime('%Y-%m-%d')
                }
            ),
        )
        if self.node_tbl_run_mode == 'common':
            self.update_tbl_to_ctr_pipeline(update_values={
                'data_date': self.pull_tbl_max_data_date().strftime('%Y-%m-%d'),
                'run_date': self.tbl_run_date.strftime('%Y-%m-%d'),
                'run_count_now': self.process_run_count(row_record=_row_record)
            })
        else:
            self.update_tbl_to_ctr_pipeline(update_values={
                'data_date': self.pull_tbl_max_data_date().strftime('%Y-%m-%d'),
            })
        return _row_record

    @property
    def retention_mode(self) -> str:
        return self.node_tbl_params.get('data_retention_mode', 'data_date')

    @property
    def retention_date(self) -> dt.date:
        return self.pull_tbl_retention_date(rtt_mode=self.retention_mode)

    def retention_start(self) -> int:
        _rtt_row: int = 0
        if self.tbl_ctr_rtt_value == 0:
            logger.info(
                "Skip retention process because `ctr_rtt_value` equal 0 or "
                "does not set in Control Framework table"
            )
            return _rtt_row
        logger.info(
            f'Process Retention value: {self.tbl_ctr_rtt_value} month '
            f'with mode: {self.retention_mode}'
        )
        _rtt_row: int = self.push_tbl_retention(rtt_date=self.retention_date)
        return _rtt_row

    @property
    def backup_name(self) -> Optional[str]:
        _bk_name: Optional[str] = self.node_tbl_params.get('backup_table')
        try:
            return (
                f"{self.tbl_name}_bk"
                if must_bool(_bk_name, force_raise=True) else None
            )
        except ValueError:
            return _bk_name

    @property
    def backup_schema(self) -> Optional[str]:
        _bk_name: Optional[str] = self.node_tbl_params.get('backup_schema')
        try:
            return (
                f"{env.AI_SCHEMA}_bk"
                if must_bool(_bk_name, force_raise=True) else None
            )
        except ValueError:
            return _bk_name

    def backup_start(self) -> int:
        if any(self.backup_name == x['table_name'] for x in Control.tables()):
            raise TableNotImplement(
                f"default backup table name {self.backup_name!r} was "
                f"duplicated with any table in catalog"
            )
        return self.push_tbl_backup(
            backup_name=self.backup_name,
            backup_schema=self.backup_schema
        )

    @property
    def ingest_payloads(self) -> list:
        _payloads_raw = self.node_tbl_params.get('payloads', [])
        return [_payloads_raw] if isinstance(_payloads_raw, dict) else _payloads_raw

    @property
    def ingest_mode(self) -> str:
        return self.node_tbl_params.get('ingest_mode', 'common')

    @property
    def ingest_action(self) -> str:
        return self.node_tbl_params.get('ingest_action', 'insert')

    def ingest_start(self) -> Tuple[int, int]:
        if (
                self.ingest_mode not in {'common', 'merge'}
                or self.ingest_action not in {'insert', 'update'}
        ):
            raise TableArgumentError(
                f"Pair of ingest mode {self.ingest_mode!r} "
                f"and action mode {self.ingest_action!r} "
                f"does not support yet."
            )

        _update_date: dt.datetime = (
            dt.datetime.fromisoformat(_update)
            if (_update := self.node_tbl_params.get('update_date'))
            else get_run_date('datetime')
        )
        if self.tbl_ctr_data_date > _update_date.date():
            raise ControlTableValueError(
                f"Please check value of `update_date`, which less than "
                f"the current control data date: "
                f"'{self.tbl_ctr_data_date:'%Y-%m-%d'}'"
            )
        _row_record: Tuple[int, int] = self.push_tbl_ingestion(
            payloads=self.ingest_payloads,
            mode=self.ingest_mode,
            action=self.ingest_action,
            update_date=_update_date
        )
        self.update_tbl_to_ctr_pipeline(update_values={
            'data_date': _update_date.strftime('%Y-%m-%d'),
            # 'run_date': self.tbl_run_date.strftime('%Y-%m-%d'),
        })
        return _row_record

    def load_file(
            self,
            filename: str,
            chunk_size: int = 10000,
            truncate: bool = False,
            compress: Optional[str] = None
    ):
        filepath: str = path_join(
            AI_APP_PATH, f'{registers.path.data}/{filename}'
        )
        file_props: dict = {
            'filepath': filepath,
            'table': self.name,
            'props': {
                'delimiter': '|',
                'encoding': 'utf-8',
                'engine': 'python'
            }
        }
        for chunk, rows in query_insert_from_csv(
                file_props,
                chunk_size=chunk_size,
                truncate=truncate,
                compress=compress
        ):
            logger.info(
                f'Success with first chuck size {chunk} '
                f'with {rows} row{get_plural(rows)}'
            )


class PipeProcess(PipeCatalog):
    """Pipeline process object for sync configuration from base config to target
    database and log to Control task schedule
    """

    __slots__ = (
        'pipe_run_date',
        'pipe_node_auto_create',
        'pipe_params',
        'pipe_ctr_schedule',
        'pipe_node_auto_drop',
        'pipe_node_auto_init',
        'pipe_node_generator'
    )

    def __init__(
            self,
            pipe_name: str,
            pipe_run_date: Optional[str] = None,
            external_parameters: Optional[dict] = None,
            schema_auto_create: bool = False,
            node_auto_drop: Union[bool, str] = False,
            node_auto_init: Union[bool, str] = True,
            verbose: bool = False
    ):
        """Main Pipeline process object initialization. If argument of pipeline
        name match the name like `retention_search` or `control_search`, the
        engine will auto generate all table name that active in
        `ctr_data_pipeline`.
        """
        self.pipe_run_date: dt.date = (
            dt.date.fromisoformat(pipe_run_date)
            if pipe_run_date
            else get_run_date(date_type='date')
        )
        self.pipe_node_auto_create: bool = schema_auto_create
        self.pipe_node_auto_drop: bool = node_auto_drop
        self.pipe_node_auto_init: bool = node_auto_init
        self.pipe_node_generator: bool = True
        if pipe_name == 'control_search':
            super().__init__(
                pipe_name=pipe_name,
                pipe_catalog={
                    'config_name': pipe_name,
                    'id': 'control',
                    'nodes': [{'name': f"sql:{x['table_name']}"} for x in Control.tables()]
                },
                verbose=verbose
            )
        elif pipe_name == 'retention_search':
            super().__init__(
                pipe_name=pipe_name,
                pipe_catalog={
                    'config_name': pipe_name,
                    'id': 'retention',
                    'nodes': [{'name': f"sql:{x['table_name']}"} for x in Control.tables(condition='rtt_value > 0')]
                },
                verbose=verbose
            )
        else:
            self.pipe_node_generator: bool = False
            super().__init__(
                pipe_name=pipe_name,
                verbose=verbose
            )
        if not check_ai_exists() and not self.pipe_node_auto_create:
            raise DatabaseSchemaNotExists(
                "Schema AI does not exists in database, "
                f"Please set `pipe_schema_auto_create` in {self.pipe_name}"
            )
        elif self.pipe_node_auto_create:
            verbose_log(
                self,
                f"Push auto create schema name: {env.AI_SCHEMA} in target database ..."
            )
            push_schema_create(schema_name=env.AI_SCHEMA)
        verbose_log(
            self,
            "Mapping parameter from the control table and external parameter argument together ...",
        )

        self.pipe_params: dict = merge_dicts(
            Control.parameters(), (external_parameters or {})
        )
        self.pipe_ctr_schedule: dict = self.pull_pipe_from_ctr_schedule()
        if (
                not self.pipe_ctr_schedule
                and self.pipe_name not in {'retention_search', 'control_search'}
        ):
            if not self.pipe_node_auto_create:
                raise ControlPipelineNotExists(
                    f"Pipeline ID: {self.pipe_id} does not exists in Control "
                    f"task schedule"
                )
            self.push_pipe_to_ctr_schedule()
            self.pipe_ctr_schedule: dict = self.pull_pipe_from_ctr_schedule()
            logger.info(
                f"Auto create {self.pipe_name!r} in control schedule table "
                f"because `pipe_node_auto_create` was set be True"
            )
        verbose_log(
            self,
            f"[Success] initialize the pipeline process object name {self.pipe_name!r}",
            end='=',
        )

    @property
    def pipe_ctr_type(self) -> str:
        return self.pipe_ctr_schedule['pipeline_type']

    @property
    def pipe_ctr_track(self) -> str:
        return self.pipe_ctr_schedule['tracking']

    @property
    def pipe_ctr_update_date(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.pipe_ctr_schedule['update_date'])

    def pull_pipe_nodes(
            self,
            node_props: Optional[dict] = None,
            auto_update: bool = True
    ) -> Iterator[Tuple[int, Node]]:
        """Pull all nodes that the pipeline contains in configuration file and
        passing node's properties.
        """
        for order, nodes in self.pipe_nodes.items():
            yield order, Node(**(node_props or {}), **nodes)
        if auto_update and not self.pipe_node_generator:
            self.update_pipe_to_ctr_schedule(
                update_values={'pipeline_id': self.pipe_alert_inc}
            )

    @ignore_unhash
    @functools.lru_cache
    def __check_trigger_function(
            self,
            trigger: Union[str, list, set],
            ctr_schedules: dict,
    ):
        if isinstance(trigger, str):
            if not (pln_trigger := ctr_schedules.get(trigger, {})):
                logger.warning(
                    f"Pipeline ID: {trigger!r} does not exists in "
                    f"`ctr_task_schedule` or active_flg equal 'N' "
                    f"from `trigger` in '{self.pipe_name}'"
                )
                raise ControlPipelineNotExists(
                    f"Pipeline ID: {trigger!r} does not exists in "
                    f"`ctr_task_schedule` or active_flg equal 'N' "
                    f"from `trigger` in '{self.pipe_name}'"
                )
            return (
                           (
                                   pln_trigger["update_date"]
                                   > self.pipe_ctr_update_date
                           )
                           and (
                                   pln_trigger["tracking"]
                                   == self.pipe_ctr_track
                                   == 'SUCCESS'
                           )
                   ) or (
                           (
                                   pln_trigger["update_date"]
                                   <= self.pipe_ctr_update_date
                           )
                           and (
                                   pln_trigger["tracking"] == 'SUCCESS'
                                   and self.pipe_ctr_track == 'FAILED'
                           )
                   )
        if run_flags := [
            self.__check_trigger_function(_trigger, ctr_schedules)
            for _trigger in trigger
        ]:
            return (
                any(run_flags)
                if isinstance(trigger, set)
                else all(run_flags)
            )
        return False

    def check_pipe_trigger(self) -> bool:
        """Return True if pipeline ..."""
        _triggers: Union[set, list] = self.pipe_trigger.copy()
        _all_pipe_schedules: dict = {
            _ctr_value['pipeline_id']: {
                'tracking': _ctr_value['tracking'],
                'update_date': dt.datetime.fromisoformat(
                    _ctr_value['update_date']
                )
            }
            for _ctr_value in self.pull_pipe_from_ctr_schedule(
                included_cols=['pipeline_id', 'tracking', 'update_date'],
                all_flag=True
            )
        }
        return self.__check_trigger_function(
            _triggers, _all_pipe_schedules
        )

    def check_pipe_schedule(
            self, group: str, waiting_process: int = 300
    ) -> bool:
        if not self.pipe_schedule:
            return False

        if group not in self.pipe_schedule:
            return False

        if self.pipe_ctr_track == 'FAILED':
            logger.warning(
                f'Pipeline ID: {self.pipe_id!r} was `FAILED` status, '
                f'please check `ctr_task_process` with pipeline_name = '
                f'{self.pipe_name!r}.'
            )
            return False
        while self.pipe_ctr_track == 'PROCESSING':
            logger.info(f"Waiting Pipeline ID: {self.pipe_id!r} processing ...")
            time.sleep(waiting_process)
            self.pipe_ctr_schedule: dict = self.pull_pipe_from_ctr_schedule()
        return True

    def pull_pipe_from_ctr_schedule(
            self,
            pipe_id: Optional[str] = None,
            included_cols: Optional[list] = None,
            all_flag: bool = False
    ):
        """Pull tacking data from the Control Task Schedule"""
        _pipe_id: str = pipe_id or self.pipe_id
        return Control('ctr_task_schedule').pull(
            pm_filter={'pipeline_id': ('*' if all_flag else _pipe_id)},
            included_cols=included_cols,
            all_flag=all_flag
        )

    def push_pipe_to_ctr_schedule(self, push_values: Optional[dict] = None):
        """Push data information to the Control Data Logging"""
        _push_values: dict = merge_dicts({
            'pipeline_id': self.pipe_id,
            'pipeline_name': self.pipe_name,
            'pipeline_type': self.pipe_schedule_type,
            'tracking': 'SUCCESS',
            'active_flg': 'true'
        }, (push_values or {}))
        return Control('ctr_task_schedule').push(push_values=_push_values)

    def update_pipe_to_ctr_schedule(self, update_values: Optional[dict] = None):
        """Update tacking information to the Control Task Schedule"""
        _update_values: dict = merge_dicts({
            'pipeline_id': self.pipe_id,
            'tracking': 'SUCCESS'
        }, (update_values or {}))
        return Control('ctr_task_schedule').update(update_values=_update_values)


class Pipeline(PipeProcess):
    """Pipeline object for orchestrate nodes of table process and log to Control
    task process
    """

    __slots__ = (
        'pipe_start_datetime', 'pipe_run_mode', 'pipe_ps_id'
    )

    def __init__(
            self,
            name: str,
            process_id: Optional[str] = None,
            run_mode: Optional[str] = None,
            run_date: Optional[str] = None,
            auto_create: Union[bool, str] = True,
            auto_drop: Union[bool, str] = False,
            auto_init: Union[bool, str] = True,
            external_parameters: Optional[dict] = None,
            verbose: bool = False
    ):
        self.pipe_run_mode: str = check_run_mode('pipe', run_mode)
        self.pipe_ps_id: str = process_id or get_process_id("undefined")
        super().__init__(
            pipe_name=name,
            pipe_run_date=run_date,
            external_parameters=external_parameters,
            schema_auto_create=auto_create,
            node_auto_drop=auto_drop,
            node_auto_init=auto_init,
            verbose=verbose
        )
        self.pipe_start_datetime: dt.datetime = get_time_checkpoint()
        verbose_log(
            self,
            f"[Success] initialize the pipeline object name {self.pipe_name}",
            end='='
        )

    @property
    def name(self):
        """The pipeline name which change attribute name from pipe_name to name
        """
        return self.pipe_name

    @property
    def run_date(self):
        return self.pipe_run_date

    @property
    def process_count(self):
        return self.pipe_nodes_count

    @property
    def process_time(self) -> int:
        return round(
            (get_time_checkpoint() - self.pipe_start_datetime).total_seconds()
        )

    def nodes(self, auto_update: bool = True):
        """Node method for passing pipeline parameters to all node in the
        pipeline
        """
        return self.pull_pipe_nodes(node_props={
            'process_id': self.pipe_ps_id,
            'run_date': self.pipe_run_date.strftime('%Y-%m-%d'),
            'run_mode': self.pipe_run_mode,
            'auto_create': self.pipe_node_auto_create,
            'auto_drop': self.pipe_node_auto_drop,
            'auto_init': self.pipe_node_auto_init,
            'fwk_parameters': self.pipe_params,
        }, auto_update=auto_update)

    def update_to_ctr_schedule(self, update_values: Optional[dict] = None):
        _update_values: dict = merge_dicts({
            'pipeline_id': self.pipe_alert_inc,
            'tracking': 'SUCCESS'
        }, (update_values or {}))
        return self.update_pipe_to_ctr_schedule(update_values=_update_values)


# [x] Migrate to modern style by `Task` Service Model
class Process:
    """Process Object for control task functions.
    This object will connect data from the control task process.
    """

    # [x] Migrate to modern style by `Task` Service Model
    @classmethod
    def load(cls, process_id: str):
        if _ctr_process := (
                Control('ctr_task_process')
                    .pull(pm_filter=[process_id])
        ):
            return cls(
                module='undefined',
                parameters=_ctr_process,
                process_id=process_id,
                load=True
            )
        raise ControlProcessNotExists(
            f"Process ID: {process_id} does not exists in Control Task Process "
            f"table"
        )

    # [x] Migrate to modern style by `Task` Model
    @classmethod
    def make(cls, module: str):
        return cls(module=module, bypass=True)

    def __init__(
            self,
            module: str,
            parameters: Optional[dict] = None,
            task: Optional[str] = None,
            component: Optional[str] = None,
            process_id: Optional[str] = None,
            bypass: bool = False,
            load: bool = False,
    ):
        self.ps_module: str = module
        self.ps_obj: Union[Node, Pipeline]
        self.ps_params: dict = parameters.copy() if parameters else {}
        self.ps_task: str = task or 'foreground'
        self.ps_component: str = component or 'undefined'
        _tbl_name: Optional[str] = self.ps_params.get('table_name')
        _pipe_name: Optional[str] = self.ps_params.get('pipeline_name')
        if _tbl_name:
            self.ps_name: str = _tbl_name
            self.ps_type: str = 'table'
            self.ps_obj = Node
        elif _pipe_name:
            self.ps_name: str = _pipe_name
            self.ps_type: str = 'pipeline'
            self.ps_obj = Pipeline
        elif load:
            self.ps_task, self.ps_type = (
                self.parameters
                    .get('process_type', 'foreground|undefined')
                    .split('|', maxsplit=1)
            )
            self.ps_component, self.ps_module = (
                self.parameters
                    .get('process_module', 'undefined|undefined')
                    .split('|', maxsplit=1)
            )
            self.ps_name: str = self.ps_module
        elif bypass:
            self.ps_name: str = module
            self.ps_type: str = 'undefined'
        else:
            raise ProcessValueError(
                "Process does not support when `table_name` or `pipeline_name` "
                "did not set from request form"
            )

        # Main attributes of process object
        self.ps_id: str = (
                process_id or
                get_process_id(self.ps_module + self.ps_type + self.ps_name)
        )
        self.ps_dates: list = (
            must_list(self.parameters.get('run_date_put', [get_run_date()]))
            if load else self.ps_params.get('run_dates', [get_run_date()])
        )
        self.ps_date: str = (
            self.parameters.get('run_date_get') if load else self.ps_dates[0]
        )
        self.ps_date_num: int = 1
        self.ps_messages: str = (
            self.parameters.get('process_message', "") if load else ""
        )
        self.ps_status: int = (
            int(self.parameters.get('status', '0')) if load else 2
        )
        self.ps_start_time: dt.datetime = get_run_date(date_type='date_time')

        # Optional attributes of framework object
        self.ps_drop_tbl: bool = convert_str_bool(
            self.ps_params.get("drop_table", 'N')
        )
        self.ps_drop_schema: bool = convert_str_bool(
            self.ps_params.get("drop_schema", 'N')
        )
        self.ps_cascade: bool = convert_str_bool(
            self.ps_params.get('cascade', 'N')
        )
        self.ps_run_mode: str = self.ps_params.get('run_mode', 'common')

    @property
    def mode(self) -> str:
        if self.ps_module == 'data':
            return self.ps_run_mode

    @property
    def is_tbl(self) -> bool:
        return self.ps_type == 'table'

    # [x] Migrate to modern style by `Parameter` Model
    @property
    def drop_schema(self) -> bool:
        return self.ps_drop_schema

    # [x] Migrate to modern style by `Parameter` Model
    @property
    def drop_table(self) -> bool:
        return self.ps_drop_tbl

    # [x] Migrate to modern style by `Parameter` Model
    @property
    def cascade(self) -> bool:
        return self.ps_cascade

    # [x] Migrate to modern style by `Parameter` Model
    @property
    def name(self) -> str:
        return self.ps_name

    # [x] Migrate to modern style by `Task` Model
    @property
    def module(self):
        return self.ps_module

    # [x] Migrate to modern style by `Task` Model
    @property
    def type(self) -> str:
        return self.ps_type

    # [x] Migrate to modern style by `Task` Model
    @property
    def component(self) -> str:
        return self.ps_component

    @property
    def obj(self) -> Type[Union[Node, Pipeline]]:
        if self.ps_type == 'undefined':
            raise ProcessValueError(
                f"Process does not support attribute `obj` "
                f"for process type is {self.ps_type!r}"
            )
        return self.ps_obj

    # [x] Migrate to modern style by `Task` Model
    @property
    def id(self) -> str:
        return self.ps_id

    # [x] Migrate to modern style by `Task` Model
    @property
    def run_date(self) -> str:
        return self.ps_date

    # [x] Migrate to modern style by `Task` Model
    @property
    def run_dates(self):
        for idx, date in enumerate(self.ps_dates):
            self.ps_messages += (
                f"\n[ run_date: {date} ]"
                if self.ps_messages else f"[ run_date: {date} ]"
            )
            self.ps_date_num += idx
            yield date

    # [x] Migrate to modern style by `Task` Model
    @property
    def parameters(self):
        return self.ps_params

    # [x] Migrate to modern style by `Task` Model
    @property
    def start_time(self) -> dt.datetime:
        return self.ps_start_time

    # [x] Migrate to modern style by `Task` Model
    @property
    def duration(self) -> int:
        return round(
            (
                    get_run_date(date_type='date_time') - self.start_time
            ).total_seconds()
        )

    # [x] Migrate to modern style by `Task` Model
    @property
    def messages(self) -> str:
        return self.ps_messages

    # [x] Migrate to modern style by `Task` Model
    @messages.setter
    def messages(self, msg: str):
        self.ps_messages += (f'\n{msg}' if self.ps_messages else msg)

    # [x] Migrate to modern style by `Task` Model
    @property
    def status(self) -> int:
        return self.ps_status

    # [x] Migrate to modern style by `Task` Model
    @status.setter
    def status(self, sts: int):
        self.ps_status = sts

    # [x] Migrate to modern style by `Task` Model
    def receive(self, results: Status):
        self.status = results.id
        self.messages = results.message

    # [x] Migrate to modern style by `Task` Service Model
    def push_task(self, push_values: Optional[dict] = None):
        """Push data information to the Control Data Logging"""
        _push_values: dict = merge_dicts({
            'process_id': self.ps_id,
            'process_name_put': self.ps_name,
            'process_name_get': 'null',
            'run_date_put': reduce_text(str(self.ps_dates)),
            'run_date_get': self.ps_dates[self.ps_date_num - 1],
            'process_message': (
                f'Start {self.ps_task} process {self.ps_type} '
                f'`{self.ps_name}` ...'
            ),
            'process_number_put': 1,
            'process_number_get': 1,
            'process_module': f'{self.ps_component}|{self.ps_module}',
            'process_type': f'{self.ps_task}|{self.ps_type}',
        }, (push_values or {}))
        return Control('ctr_task_process').push(push_values=_push_values)

    # [x] Migrate to modern style by `Task` Service Model
    def update_task(self, update_values: Optional[dict] = None):
        """Update data information to the Control Data Pipeline"""
        _update_values: dict = merge_dicts({
            'process_id': self.ps_id,
            'process_message': reduce_text(self.ps_messages),
            'process_time': self.duration,
            'status': self.ps_status
        }, (update_values or {}))
        return Control('ctr_task_process').update(update_values=_update_values)

    # [x] Migrate to modern style by `Task` Service Model
    def start_task(self, index: int, count: Optional[int] = None):
        if index == 1:
            count: int = count or 1
            return self.push_task(push_values={'process_number_put': count})
        return 0

    # [x] Migrate to modern style by `Task` Service Model
    def end_task(self):
        _update: dict = {}
        if self.ps_status == 0:
            _update: dict = {
                'process_name_get': 'null',
                'run_date_get': 'null',
            }
        return self.update_task(update_values=_update)


ObjectType: Type = Optional[Union[Node, Pipeline, Action]]


# [x] Migrate to modern style by `Schema` Service Model
class Schema:
    """Schema Object for action with schema in target database.
    """
    __slots__ = (
        'name',
        'cascade',
    )

    def __init__(
            self,
            schema_name: Optional[str] = None,
            cascade: bool = False
    ):
        self.name: str = schema_name or env.AI_SCHEMA
        self.cascade: bool = cascade

    # [x] Migrate to modern style by `Schema` Model in services
    @property
    def exists(self) -> bool:
        return check_schema_exists(schema_name=self.name)

    # [x] Migrate to modern style by `Schema` Model in services
    def create(self) -> None:
        push_schema_create(schema_name=self.name)

    # [x] Migrate to modern style by `Schema` Model in services
    def drop(self) -> None:
        push_schema_drop(schema_name=self.name, cascade=self.cascade)
