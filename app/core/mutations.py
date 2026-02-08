from __future__ import annotations

from core.app_state import AppState
from core.filters import All
from core.row_model import FileType, RowModel, RowStatus


def set_source_path(state: AppState, new_path: str) -> None:
	state.source_path = new_path


def set_dest_path(state: AppState, new_path: str) -> None:
	state.dest_path = new_path


def set_type_filter(state: AppState, new_filter: FileType | All) -> None:
	state.filters.type_filter = new_filter


def set_status_filter(state: AppState, new_filter: RowStatus | All) -> None:
	state.filters.status_filter = new_filter


def toggle_row_checked(state: AppState, row_id: str, checked: bool) -> None:
	for row in state.rows:
		if row.id == row_id:
			if not row.checkbox_enabled:
				return
			row.checked = checked
			return


def toggle_all_eligible(state: AppState, checked: bool) -> None:
	for row in state.rows:
		if row.checkbox_enabled:
			row.checked = checked


def apply_filters(state: AppState) -> list[RowModel]:
	out: list[RowModel] = []
	for row in state.rows:
		if state.filters.status_filter != "All" and row.status != state.filters.status_filter:
			continue
		if state.filters.type_filter != "All" and row.file_type != state.filters.type_filter:
			continue
		out.append(row)
	return out


def resolve_review_row_manual(
	state: AppState,
	*,
	row_id: str,
	doc_no: str,
	file_type: FileType,
	new_source_path: str,
) -> bool:
	"""Apply manual inputs to an existing row.

	Updates file_name/file_type/status/source_path only.
	Must be called on the UI thread.
	"""
	for row in state.rows:
		if row.id != row_id:
			continue

		file_name = (doc_no or "").strip() or "!"
		ft = file_type if isinstance(file_type, FileType) else FileType.Unknown
		status = RowStatus.Ready if (file_name != "!" and ft != FileType.Unknown) else RowStatus.Review

		row.file_name = file_name
		row.file_type = ft
		row.status = status
		row.source_path = new_source_path or row.source_path
		return True
	return False
