from __future__ import annotations

from core.app_state import AppState
from core.app_state import enforce_display_name_group_status
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

	def _status_rank(status: RowStatus) -> int:
		return 0 if status == RowStatus.Review else 1

	return sorted(out, key=lambda r: (_status_rank(r.status), -r.origin_seq))


def resolve_review_row_manual(
	state: AppState,
	*,
	row_id: str,
	doc_no: str,
	file_type: FileType,
	new_source_path: str,
) -> bool:
	"""Apply manual inputs to an existing row.

	Updates display_name/file_name/file_type/status/source_path only.
	Must be called on the UI thread.
	"""
	for row in state.rows:
		if row.id != row_id:
			continue

		prev_status = row.status

		old_canon = (row.display_name or "").strip().casefold()

		display_name = (doc_no or "").strip() or "!"
		file_name = display_name
		ft = file_type if isinstance(file_type, FileType) else FileType.Unknown
		status = RowStatus.Ready if (display_name != "!" and ft != FileType.Unknown) else RowStatus.Review

		row.display_name = display_name
		row.file_name = file_name
		row.file_type = ft
		row.status = status
		if prev_status == RowStatus.Review and row.status == RowStatus.Ready:
			row.origin_seq = state.next_row_seq
			state.next_row_seq += 1
		new_canon = (row.display_name or "").strip().casefold()
		if old_canon and old_canon != "!":
			enforce_display_name_group_status(state, old_canon)
		if new_canon and new_canon != "!":
			enforce_display_name_group_status(state, new_canon)
		if (row.status == RowStatus.Ready) and (row.file_type in {FileType.TaxInvoice, FileType.Proforma}):
			row.checkbox_enabled = True
		else:
			row.checkbox_enabled = False
			row.checked = False
		row.source_path = new_source_path or row.source_path
		return True
	return False


def deposit_ready_rows(state: AppState) -> int:
	"""Deposit v1: mark all Ready rows as Processed.

	Pure state transition (no filesystem). Must be called on the UI thread.
	"""
	changed = 0
	for row in state.rows:
		if row.status != RowStatus.Ready:
			continue
		row.status = RowStatus.Processed
		if (row.status == RowStatus.Ready) and (row.file_type in {FileType.TaxInvoice, FileType.Proforma}):
			row.checkbox_enabled = True
		else:
			row.checkbox_enabled = False
			row.checked = False
		changed += 1
	return changed
