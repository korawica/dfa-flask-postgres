# ------------------------------------------------------------------------------
# Copyright (c) 2022 Korawich Anuttra. All rights reserved.
# Licensed under the MIT License. See LICENSE in the project root for
# license information.
# ------------------------------------------------------------------------------
from __future__ import annotations

import ast
import builtins
from collections.abc import Iterator
from datetime import date, datetime
from itertools import takewhile
from typing import (
    Optional,
    Union,
)

from pydantic import Field, validator
from typing_extensions import Self

from conf.settings import settings

from .__types import DictKeyStr
from .base import (
    PARAMS,
    get_plural,
    get_process_date,
    get_run_date,
    registers,
    sort_by_priority,
)
from .connections import (
    ParamType,
    query_execute,
    query_execute_row,
    query_insert_from_csv,
    query_select,
    query_select_check,
    query_select_one,
    query_select_row,
)
from .convertor import Statement, Value
from .errors import (
    CatalogArgumentError,
    ControlProcessNotExists,
    ControlTableNotExists,
    ControlTableValueError,
    DatabaseProcessError,
    FuncArgumentError,
    FuncNotFound,
    FuncRaiseError,
    ObjectBaseError,
    TableArgumentError,
    TableNotFound,
    TableNotImplement,
)
from .models import (
    UNDEFINED,
    ParameterMode,
    ParameterType,
    Status,
    TaskComponent,
    TaskMode,
    reduce_text,
)
from .schemas import WTM_DEFAULT, ControlWatermark
from .statements import (
    ControlStatement,
    FunctionStatement,
    SchemaStatement,
    TableStatement,
    reduce_in_value,
    reduce_value,
    reduce_value_pairs,
)
from .utils import (
    AI_APP_PATH,
    Environs,
    hash_string,
    logging,
    must_bool,
    must_list,
    ptext,
    split_iterable,
)
from .validators import (
    FrameworkParameter,
    MapParameter,
    ReleaseDate,
)
from .validators import (
    Pipeline as PipelineCatalog,
)
from .validators import (
    Task as BaseTask,
)

env = Environs(env_name=".env")
logger = logging.getLogger(__name__)

__all__ = (
    "Schema",
    "Action",
    "ActionQuery",
    "Node",
    "NodeLocal",
    "NodeIngest",
    "Pipeline",
    "Task",
    "Control",
)


def null_or_str(value: str) -> Optional[str]:
    return None if value == "None" else value


def query_explain(statement: str, parameters: ParamType = True) -> dict:
    """Enhance query function for explain analytic."""
    stm = Statement(statement)
    query: str = stm.generate().strip()
    if query.count(";") > 1:
        raise FuncRaiseError(
            "query should not contain `;` more than 1 letter in 1 query"
        )
    if stm.type == "dql":
        _statement: str = PARAMS.ps_stm.explain.format(query=query.rstrip(";"))
        return {
            "QUERY PLAN": ast.literal_eval(
                query_select_one(_statement, parameters=parameters)[
                    "QUERY PLAN"
                ]
            )
        }
    elif stm.type == "dml":
        _statement: str = PARAMS.ps_stm.explain.format(
            query=Statement.add_row_num(query.rstrip(";")).rstrip(";")
        )
        return {
            "QUERY PLAN": ast.literal_eval(
                query_select_one(_statement, parameters=parameters)[
                    "QUERY PLAN"
                ]
            )
        }
    raise FuncArgumentError(
        "query explain support only statement type be `dql` or `dml` only"
    )


def get_time_checkpoint(
    date_type: Optional[str] = None,
) -> Union[datetime, date]:
    return get_run_date(date_type=(date_type or "date_time"))


class MapParameterService(MapParameter):

    @validator("ext_params", always=True)
    def __prepare_ext_params(cls, value: DictKeyStr) -> DictKeyStr:
        return value | Control.params()

    @validator("fwk_params", pre=True, always=True)
    def __prepare_fwk_params(
        cls,
        value: Union[DictKeyStr, FrameworkParameter],
    ) -> FrameworkParameter:
        if isinstance(value, dict):
            return FrameworkParameter.parse(
                {
                    "run_id": hash_string(get_run_date(fmt="%Y%m%d%H%M%S%f")),
                    "run_date": get_run_date(fmt="%Y-%m-%d"),
                    "run_mode": UNDEFINED,
                }
                | value
            )
        return value

    def add_ext_params(self, params: DictKeyStr) -> Self:
        logger.debug("Add more external parameters ...")
        self.__dict__["ext_params"] = self.ext_params | params
        return self

    def ext_params_refresh(self) -> Self:
        """Refresh External parameters."""
        self.__dict__["ext_params"] = self.ext_params | Control.params()
        return self

    def filter_params(
        self,
        params: list[str],
        additional: Optional[DictKeyStr] = None,
    ) -> DictKeyStr:
        _full_params: DictKeyStr = (
            self.fwk_params.dict(by_alias=False)
            | self.ext_params
            | (additional or {})
            | {"update_date": get_run_date(fmt="%Y-%m-%d %H:%M:%S")}
        )
        try:
            return {param: _full_params[param] for param in params}
        except KeyError as k:
            raise CatalogArgumentError(
                f"Catalog does not map config parameter for {k!r}"
            ) from k


class Schema(SchemaStatement):
    """Schema Service Model."""

    name: str = Field(default=env.AI_SCHEMA, description="Schema name")

    def exists(self) -> bool:
        """Push exists statement to target database."""
        return query_select_check(self.statement_check(), parameters=True)

    def create(self) -> Schema:
        """Push create statement to target database."""
        query_execute(self.statement_create())
        return self

    def drop(self, cascade: bool = False) -> Schema:
        """Push drop statement to target database."""
        query_execute(self.statement_drop(cascade=cascade))
        return self


class BaseAction(MapParameterService, FunctionStatement):

    def __init__(self, **data):
        super().__init__(**data)
        _auto_create: bool = must_bool(
            self.fwk_params.task_params.others.get("auto_create", "Y"),
            force_raise=True,
        )
        if self.type == "query":
            try:
                self.explain()
            except DatabaseProcessError as err:
                raise FuncRaiseError(
                    f"{self.name!r} was raised the statement error "
                    f"from query explain checking"
                ) from err
        else:
            if not self.exists() and _auto_create:
                self.create()
            else:
                raise FuncNotFound(
                    f"Table {self.name} not found in the AI Database, "
                    f"Please set `func_auto_create`."
                )

    def exists(self) -> bool:
        """Push exists statement to target database."""
        return query_select_check(self.statement_check())

    def create(self) -> None:
        """Push create statement to target database."""
        if self.type not in ("func", "view", "mview"):
            raise ValueError(
                f"Function type {self.type!r} does not support create"
            )
        query_execute(self.statement(), parameters=True)

    def explain(self):
        if self.type != "query":
            raise FuncRaiseError(
                f"Function type {self.type!r} does not support for explain."
            )
        return query_explain(
            self.statement(),
            parameters=self.filter_params(self.profile.parameter),
        )


class Action(BaseAction): ...


class ActionQuery(BaseAction):

    def execute(self, limit: int = 10):
        """Push down the query to target database."""
        if self.type not in {"query", "view", "mview"}:
            raise FuncRaiseError(
                f"Function type {self.type!r} does not support for execute "
                f"query method."
            )
        stm: Statement = Statement(self.profile.statement)
        if stm.type == "dql":
            return {
                index: result
                for index, result in takewhile(
                    lambda x: x[0] <= limit,
                    enumerate(
                        query_select(
                            stm.generate(),
                            parameters=self.filter_params(
                                self.profile.parameter
                            ),
                        ),
                        start=1,
                    ),
                )
            }
        elif stm.type == "dml":
            return {
                1: {
                    "row_number": query_select_row(
                        stm.generate(),
                        parameters=self.filter_params(self.profile.parameter),
                    )
                }
            }
        raise FuncArgumentError(
            f"Function {self.name!r} does not support `push_query` "
            f"with statement type {stm.type!r}"
        )


class BaseNode(MapParameterService, TableStatement):
    """Base Node Service Model."""

    @classmethod
    def parse_task(
        cls,
        name: str,
        fwk_params: DictKeyStr,
        ext_params: DictKeyStr,
    ) -> Self:
        """Parsing all parameters from Task before Running the Node."""
        return cls.parse_name(
            name,
            additional={"fwk_params": fwk_params, "ext_params": ext_params},
        )

    @property
    def run_type(self) -> str:
        run_types: dict = PARAMS.map_tbl_run_type.copy()
        return next(
            (
                run_types[col]
                for col in self.profile.columns(pk_included=True)
                if col in run_types
            ),
            "daily",
        )

    def exists(self) -> bool:
        """Push exists statement to target database."""
        return query_select_check(self.statement_check(), parameters=True)

    def create(self) -> Self:
        """Execute create statement to target database."""
        query_execute(self.statement_create(), parameters=True)
        return self

    def drop(self): ...

    def make_log(self, values: Optional[dict] = None):
        return Control("ctr_data_logging").create(
            values=(
                {
                    "table_name": self.name,
                    "data_date": self.fwk_params.run_date.strftime("%Y-%m-%d"),
                    "run_date": self.fwk_params.run_date.strftime("%Y-%m-%d"),
                    "action_type": "common",
                    "row_record": 0,
                    "process_time": 0,
                }
                | (values or {})
            )
        )

    def log(self, values: Optional[dict] = None):
        return Control("ctr_data_logging").push(
            values=(
                {
                    "table_name": self.name,
                    "run_date": self.fwk_params.run_date.strftime("%Y-%m-%d"),
                    "action_type": "common",
                }
                | (values or {})
            )
        )

    def make_watermark(self, values: Optional[dict] = None):
        return Control("ctr_data_pipeline").create(
            values=(
                {
                    "system_type": PARAMS.map_tbl_sys.get(
                        self.prefix, UNDEFINED
                    ),
                    "table_name": self.name,
                    "table_type": UNDEFINED,
                    "data_date": self.fwk_params.run_date.strftime("%Y-%m-%d"),
                    "run_date": self.fwk_params.run_date.strftime("%Y-%m-%d"),
                    "run_type": self.run_type,
                    "run_count_now": 0,
                    "run_count_max": 0,
                    "rtt_value": 0,
                    "rtt_column": UNDEFINED,
                }
                | (values or {})
            )
        )

    def push(self, values: Optional[DictKeyStr] = None) -> int:
        """Update logging watermark to Control Pipeline."""
        _values: dict = {"table_name": self.name} | (values or {})
        try:
            return Control("ctr_data_pipeline").push(values=_values)
        except DatabaseProcessError as err:
            logger.warning(
                f"Does not update control data pipeline table because of, \n"
                f"{ptext(str(err))}"
            )
            return 0


class Node(BaseNode):

    watermark: ControlWatermark = Field(
        default_factory=dict,
        description="Node watermark data from Control Pipeline",
    )

    def __init__(self, **data):
        super().__init__(**data)
        _auto_create: bool = must_bool(
            self.fwk_params.task_params.others.get("auto_create", "Y"),
            force_raise=True,
        )
        if not self.exists():
            if not _auto_create:
                raise TableNotFound(
                    "Please set `tbl_auto_create` be True or setup "
                    "this table with API."
                )
            logger.info(
                f"Auto create {self.name!r} in database because "
                f"`auto_create` was set be True"
            )
            self.create()
        if self.watermark.table_name == UNDEFINED:
            if not _auto_create:
                raise ControlTableNotExists(
                    f"Table name: {self.name} does not exists "
                    f"in Control data pipeline"
                )
            logger.info(
                f"Auto insert configuration data to `ctr_data_pipeline` "
                f"for {self.name!r}"
            )
            self.make_watermark()
            self.watermark_refresh()

    @validator("watermark", pre=True, always=True)
    def __prepare_watermark(cls, value: DictKeyStr, values):
        try:
            wtm: DictKeyStr = WTM_DEFAULT | Control("ctr_data_pipeline").pull(
                pm_filter=[values["name"]]
            )
        except DatabaseProcessError:
            wtm = WTM_DEFAULT
        return ControlWatermark.parse_obj(wtm | value)

    def watermark_refresh(self):
        logger.debug("Add more external parameters ...")
        self.__dict__["watermark"] = ControlWatermark.parse_obj(
            obj=Control("ctr_data_pipeline").pull(pm_filter=[self.name])
        )
        return self

    def process_date(self) -> date:
        return get_process_date(
            self.fwk_params.run_date,
            self.watermark.run_type,
            date_type="date",
        )


class ManageNode(Node):
    def backup(self): ...

    def retention(self): ...

    def init(self): ...


class NodeLocal(Node):
    """Node for Local File loading."""

    def load(
        self,
        filename: str,
        chuck: int = 10_000,
        truncate: bool = False,
        compress: Optional[str] = None,
    ) -> int:
        file_props: DictKeyStr = {
            "filepath": AI_APP_PATH / f"{registers.path.data}/{filename}",
            "table": self.name,
            "props": {
                "delimiter": "|",
                "encoding": "utf-8",
                "engine": "python",
            },
        }
        rows: int = 0
        for chunk, row in query_insert_from_csv(
            file_props,
            chunk_size=chuck,
            truncate=truncate,
            compress=compress,
        ):
            logger.info(
                f"Success with first chuck size {chunk} "
                f"with {row} row{get_plural(row)}"
            )
            rows += row
        return rows


class NodeIngest(Node):
    """Node that implement Ingestion method that use on the ingestion
    component."""

    def __ingest(
        self,
        payloads: list,
        mode: str,
        action: str,
        update_date: datetime,
    ) -> tuple[int, int]:
        ps_row_success: int = 0
        ps_row_failed: int = 0
        _start_time: datetime = get_time_checkpoint()
        self.make_log(
            values={
                "data_date": update_date.strftime("%Y-%m-%d"),
                "update_date": update_date.strftime("%Y-%m-%d %H:%M:%S"),
                "action_type": "ingestion",
            }
        )

        # Note: Chuck size for merge mode will be split from the first level
        # of payloads.
        for index, data in enumerate(
            split_iterable(payloads, settings.APP_INGEST_CHUCK),
            start=1,
        ):
            try:
                cols, values = Value(
                    values=data,
                    update_date=update_date.strftime("%Y-%m-%d %H:%M:%S"),
                    mode=mode,
                    action=action,
                    expected_cols=self.profile.to_mapping(pk=True),
                    expected_pk=self.profile.primary_key,
                ).generate()
                row_ingest: int = (
                    query_execute_row(
                        self.statement_update(suffix="UD"),
                        parameters={
                            "string_columns": ", ".join(cols),
                            "string_columns_pairs": ", ".join(
                                [
                                    (
                                        f"{col.name} = {self.shortname}_UD."
                                        f"{col.name}::{col.datatype}"
                                    )
                                    for col in self.profile.features
                                    if col.name not in self.profile.primary_key
                                ]
                            ),
                            "string_values": values,
                        },
                    )
                    if action == "update"
                    else query_execute_row(
                        self.statement_insert(),
                        parameters={
                            "string_columns": ", ".join(cols),
                            "string_values": values,
                        },
                    )
                )
                _failed: int = 0
                if row_ingest != (_row_generate := (values.count("), (") + 1)):
                    _failed: int = _row_generate - row_ingest
                    ps_row_failed += _failed
                ps_row_success += row_ingest
                logger.info(
                    f"Ingest chunk {index:02d}: "
                    f"(success: {row_ingest} row{get_plural(row_ingest)},"
                    f"false: {_failed} row{get_plural(_failed)})"
                )
                self.log(
                    values={
                        "action_type": "ingestion",
                        "row_record": {1: ps_row_success, 2: ps_row_failed},
                        "process_time": round(
                            (
                                get_time_checkpoint() - _start_time
                            ).total_seconds()
                        ),
                        "status": 0,
                    }
                )
            except ObjectBaseError as err:
                self.log(
                    values={
                        "action_type": "ingestion",
                        "row_record": {1: ps_row_success, 2: ps_row_failed},
                        "process_time": round(
                            (
                                get_time_checkpoint() - _start_time
                            ).total_seconds()
                        ),
                        "status": 1,
                    }
                )
                logger.error(f"Error: {err.__class__.__name__}: {str(err)}")
                raise err
        return ps_row_success, ps_row_failed

    def execute(self) -> tuple[int, int]:
        """Ingest Data from the input payload."""
        _action: str = self.ext_params.get("ingest_action", "insert")
        if (_mode := self.ext_params.get("ingest_mode", "common")) not in (
            "common",
            "merge",
        ) or _action not in ("insert", "update"):
            raise TableArgumentError(
                f"Pair of ingest mode {_mode!r} and action mode {_action!r} "
                f"does not support yet."
            )
        _update_date: datetime = (
            datetime.fromisoformat(_update)
            if (_update := self.ext_params.get("update_date"))
            else get_run_date("datetime")
        )
        if self.watermark.data_date > _update_date.date():
            raise ControlTableValueError(
                f"Please check value of `update_date`, which less than "
                f"the current control data date: "
                f"'{self.watermark.data_date:'%Y-%m-%d'}'"
            )
        _payloads: Union[dict, list] = self.ext_params.get("payloads", [])
        _row_record: tuple[int, int] = self.__ingest(
            payloads=[_payloads] if isinstance(_payloads, dict) else _payloads,
            mode=_mode,
            action=_action,
            update_date=_update_date,
        )
        self.push(values={"data_date": _update_date.strftime("%Y-%m-%d")})
        return _row_record


class Pipeline(PipelineCatalog):
    """Pipeline Service Model."""

    def nodes(self): ...

    def log_push(self): ...

    def log_fetch(self): ...

    def check_triggered(self): ...

    def check_scheduled(self): ...

    def push(self): ...


class Task(BaseTask):
    """Task Service Model.

    Notes:
        -   For starting any api tasks, I will create this Task model with the 4
            keys (module, parameters, mode, and component).
        -   Task will generate the task ID from a module value, ``param.type``,
            and ``param.name``.
        -   Task has the runner method that return generator of datetime from
            the ``param.dates``.
        -   ``Task.start`` and ``Task.finish`` is the main of create and update
            the task status log to logging table at target database.

    Examples:

        >>> task: Task = Task.make(module="demo_docstring")
        ... task.start()
        ... "Do Something"
        ... task.finish()
    """

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.finish()

    @classmethod
    def pull(cls, task_id: str):
        """Pull Task from target database with Task ID."""
        if ctr_process := (
            Control("ctr_task_process").pull(pm_filter=[task_id])
        ):
            mode, _type = ctr_process.get(
                "process_type", "foreground|undefined"
            ).split("|", maxsplit=1)
            component, module = ctr_process.get(
                "process_module", "undefined|undefined"
            ).split("|", maxsplit=1)
            run_mode: str = ctr_process.get("run_mode", "common")
            _date: Optional[str] = null_or_str(ctr_process.get("run_date_get"))
            return cls(
                module=module,
                parameters={
                    "name": module,
                    "type": ParameterType(_type),
                    "mode": ParameterMode(run_mode),
                    "dates": must_list(ctr_process["run_date_put"]),
                    **ctr_process,
                },
                mode=TaskMode(mode),
                component=TaskComponent(component),
                status=Status(int(ctr_process["status"])),
                id=task_id,
                message=ctr_process["process_message"],
                release=ReleaseDate(date=_date),
            )
        raise ControlProcessNotExists(
            f"Process ID: {task_id} does not exists in Control Task Process "
            f"table."
        )

    def start(self, task_total: Optional[int] = 1) -> int:
        """Start Task.

        :rtype: int
        :return: Return 0 if the release was pushed (runner index !=
            start index). But push the log to target database when
            release was not pushed.
        """
        return (
            0
            if self.release.pushed
            else self.create(values={"process_number_put": task_total})
        )

    def finish(self) -> int:
        """Update task log to target database."""
        return self.push(
            values=(
                {
                    "process_name_get": "null",
                    "run_date_get": "null",
                }
                if self.status == Status.SUCCESS
                else {}
            )
        )

    def create(self, values: Optional[dict] = None) -> int:
        """Create information to the Control Data Logging."""
        return Control("ctr_task_process").create(
            values=(
                {
                    "process_id": self.id,
                    "process_name_put": self.parameters.name,
                    "process_name_get": "null",
                    "run_date_put": reduce_text(str(self.parameters.dates)),
                    "run_date_get": self.release.date,
                    "process_message": (
                        f"Start {self.mode} process {self.parameters.type} "
                        f"`{self.parameters.name}` ..."
                    ),
                    "process_number_put": 1,
                    "process_number_get": 1,
                    "process_module": f"{self.component}|{self.module}",
                    "process_type": f"{self.mode}|{self.parameters.type}",
                }
                | (values or {})
            )
        )

    def push(self, values: Optional[dict] = None) -> int:
        """Update information to the Control Data Logging."""
        return Control("ctr_task_process").push(
            values=(
                {
                    "process_id": self.id,
                    "process_message": reduce_text(self.message),
                    "process_time": self.duration(),
                    "status": self.status,
                }
                | (values or {})
            )
        )


class Control(ControlStatement):

    def __init__(self, name: str, *, params: Optional[dict] = None) -> None:
        super().__init__(name=name)
        self.defaults: dict[str, Union[str, int]] = {
            "update_date": get_run_date(fmt="%Y-%m-%d %H:%M:%S"),
            "process_time": 0,
            "status": Status.WAITING.value,
        } | (params or {})

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.name})"

    def __str__(self) -> str:
        return self.name

    @classmethod
    def params(cls, module: Optional[str] = None) -> DictKeyStr:
        logger.debug("Loading params from `ctr_data_parameter` by Control ...")
        _results: dict = {
            value["param_name"]: (
                ast.literal_eval(value["param_value"])
                if value["param_type"] in {"list", "dict"}
                else getattr(builtins, value["param_type"])(
                    value["param_value"]
                )
            )
            for value in query_select(
                cls.statement_params(),
                parameters=reduce_value_pairs({"module_type": (module or "*")}),
            )
        }
        # Calculate special parameters that logic was provided by vendor.
        proportion_value: int = _results.get("proportion_value", 3)
        proportion_inc_curr_m: str = _results.get(
            "proportion_inc_current_month_flag", "N"
        )
        return {
            "window_start": (
                proportion_value
                if proportion_inc_curr_m == "N"
                else (proportion_value - 1)
            ),
            "window_end": (1 if proportion_inc_curr_m == "N" else 0),
            **_results,
        }

    @classmethod
    def tables(
        cls,
        condition: Optional[str] = None,
    ) -> Iterator[dict[str, str]]:
        """Get all tables with `condition` argument from `ctr_data_pipeline` in
        target database and convert to python list of dictionary type."""
        logger.debug("Loading tables from `ctr_data_pipeline` by Control ...")
        for name in sort_by_priority(
            [
                tbl["table_name"]
                for tbl in cls("ctr_data_pipeline").pull(
                    pm_filter={"table_name": "*"},
                    included=["table_name"],
                    condition=condition,
                    all_flag=True,
                )
            ]
        ):
            yield {"table_name": name}

    def create(
        self,
        values: dict,
        condition: Optional[str] = None,
    ) -> int:
        _ctr_columns = filter(
            lambda _col: _col not in {"primary_id"}, self.cols
        )
        _add_column: DictKeyStr = self.defaults | {
            "tracking": "SUCCESS",
            "active_flg": "Y",
        }
        _row_record_filter: str = ""
        _status_filter: str = ""
        for col in _ctr_columns:
            value_old: Union[str, int] = (
                _add_column.get(col, "null")
                if col not in values
                else values[col]
            )
            values[col]: str = reduce_value(value_old)
        if "status" in list(_ctr_columns):
            _status_filter: str = "where excluded.status = '2'"
        if "row_record" in list(_ctr_columns):
            _row_record_filter: str = (
                f"{'or' if _status_filter else 'where'} "
                f"{self.tbl.shortname}.row_record <= excluded.row_record"
            )
        _set_value_pairs: str = ", ".join(
            [f"{_} = excluded.{_}" for _ in self.cols_no_pk]
        )
        return query_select_row(
            self.statement_create(),
            parameters={
                "columns_pair": ", ".join(values),
                "values": ", ".join(values.values()),
                "primary_key": ", ".join(self.pk),
                "set_value_pairs": _set_value_pairs,
                "row_record_filter": _row_record_filter,
                "status_filter": _status_filter,
                "condition": (
                    f"""AND ({condition.replace('"', "'")})"""
                    if condition
                    else ""
                ),
            },
        )

    def push(
        self,
        values: DictKeyStr,
        condition: Optional[str] = None,
    ) -> int:
        _add_column: dict = self.defaults | {"tacking": "PROCESSING"}
        for col, default in _add_column.items():
            if col in self.cols and col not in values:
                values[col] = default
        _update_values: dict[str, str] = {
            k: reduce_value(str(v))
            for k, v in values.items()
            if k not in self.pk
        }
        _filter: list[str] = [
            f"{self.tbl.shortname}.{_} in {reduce_in_value(values[_])}"
            for _ in self.pk
        ]
        return query_select_row(
            self.statement_push(),
            parameters={
                "update_values_pairs": ", ".join(
                    [f"{k} = {v}" for k, v in _update_values.items()]
                ),
                "filter": " AND ".join(_filter),
                "condition": (
                    f"""AND ({condition.replace('"', "'")})"""
                    if condition
                    else ""
                ),
            },
        )

    def pull(
        self,
        pm_filter: Union[list, dict],
        condition: Optional[str] = None,
        included: Optional[list] = None,
        *,
        active_flag: Optional[str] = None,
        all_flag: Optional[bool] = False,
    ) -> DictKeyStr:
        """Pull data from the control table."""
        if len(self.pk) > 1 and isinstance(pm_filter, list):
            raise TableNotImplement(
                f"Pull control does not support `pm_filter` with `list` type "
                f"when {self.name} have primary keys more than 1"
            )
        elif isinstance(pm_filter, dict):
            if any(col not in self.pk for col in pm_filter):
                raise TableArgumentError(
                    f"Pull control does not support value in `pm_filter` "
                    f"with keys, {str(pm_filter.keys())}"
                )
            _pm_filter: dict = reduce_value_pairs(pm_filter)
        else:
            _pm_filter: dict = {
                self.pk[0]: ", ".join(map(reduce_value, pm_filter))
            }
        _pm_filter_stm: str = " and ".join(
            [f"{pk} in ({_pm_filter[pk]})" for pk in self.pk]
        )
        return (query_select if all_flag else query_select_one)(
            self.statement_pull(),
            parameters={
                "select_columns": ", ".join(
                    col for col in self.cols if col in (included or self.cols)
                ),
                "primary_key_filters": _pm_filter_stm,
                "active_flag": (
                    f"and active_flg in ('{(active_flag or 'Y')}')"
                    if "active_flg" in self.cols
                    else ""
                ),
                "condition": (
                    f"""AND ({condition.replace('"', "'")})"""
                    if condition
                    else ""
                ),
            },
        )
