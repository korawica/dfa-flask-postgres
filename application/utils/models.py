# -------------------------------------------------------------------------
# Copyright (c) 2022 Korawich Anuttra. All rights reserved.
# Licensed under the MIT License. See LICENSE in the project root for
# license information.
# --------------------------------------------------------------------------

import os
from typing import Protocol
from dataclasses import dataclass, field
from ..utils.convertor import reduce_text
from ..utils.reusables import must_bool


@dataclass
class BaseStatus:
    id: int
    _message: str

    @property
    def message(self):
        return self._message

    @message.setter
    def message(self, msg: str):
        self._message += (f'\n{msg}' if self._message else f'{msg}')

    def update(self, _id: int, msg: str):
        self.id: int = _id
        self.message: str = reduce_text(msg)


@dataclass
class Status(BaseStatus):
    id: int = 0
    _message: str = ""


@dataclass
class AnalyticStatus(BaseStatus):
    id: int = 0
    _message: str = ""
    percent: float = 0.0


@dataclass
class DependencyStatus(BaseStatus):
    id: int = 0
    _message: str = ""
    mapping: dict = field(default_factory=dict)


class VerboseObject(Protocol):
    """This verbose object protocol should have verbose property
    """
    verbose: bool


class VerboseDummy:
    verbose: bool = must_bool(os.getenv('DEBUG', 'False'))


@dataclass
class DataPipeline:
    """Data class for the control table, `ctr_data_pipeline`."""
    system_type: str
    table_name: str
    table_type: str
    data_date: str
    run_date: str
    run_type: str
    run_count_now: str
    run_count_max: str
    rtt_value: str
    rtt_column: str
