from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.row_model import FileType, RowStatus


All = Literal["All"]


@dataclass
class FilterModel:
	type_filter: FileType | All
	status_filter: RowStatus | All
