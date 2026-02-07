from __future__ import annotations

from dataclasses import dataclass

from core.filters import FilterModel
from core.row_model import RowModel


@dataclass
class AppState:
	source_path: str
	dest_path: str
	filters: FilterModel
	rows: list[RowModel]
