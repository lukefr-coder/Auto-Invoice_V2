from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Literal

from core.filters import FilterModel
from core.row_model import FileType, RowModel, RowStatus


WorkStatus = Literal["pending", "running", "done"]
Phase1Kind = Literal["processed", "duplicate_skipped"]


@dataclass(frozen=True)
class Phase1Result:
	batch_id: int
	original_path: str
	fingerprint_sha256: str
	doc_no: str
	file_type: FileType
	renamed_path: str
	kind: Phase1Kind = "processed"


@dataclass
class ActiveBatch:
	batch_id: int
	snapshot_paths: list[str]
	total: int
	done_count: int = 0


@dataclass
class WorkItem:
	batch_id: int
	path: str
	index: int
	status: WorkStatus = "pending"


@dataclass
class AppState:
	source_path: str
	dest_path: str
	filters: FilterModel
	rows: list[RowModel]

	# Slice 03: watch/batch/work queue state (Core-owned, in-memory only)
	known_paths: set[str] = field(default_factory=set)
	pending_paths: set[str] = field(default_factory=set)
	active_batch: ActiveBatch | None = None
	work_queue: list[WorkItem] = field(default_factory=list)
	next_batch_id: int = 1

	# Slice 04A: Phase-1 stub completion guards (in-memory only)
	phase1_completed_paths: set[str] = field(default_factory=set)
	next_row_seq: int = 1

	# Slice 05: in-memory fingerprint set (UI-thread only mutation)
	known_fingerprints: set[str] = field(default_factory=set)


def normalize_path(path: str) -> str:
	try:
		abs_path = os.path.abspath(path)
	except Exception:
		abs_path = path
	return os.path.normcase(os.path.normpath(abs_path))


def _is_pdf(path: str) -> bool:
	return path.lower().endswith(".pdf")


def _quarantine_root(state: AppState) -> str:
	if not state.source_path:
		return ""
	return normalize_path(os.path.join(state.source_path, "quarantine"))


def _is_under_quarantine(state: AppState, norm_path: str) -> bool:
	q = _quarantine_root(state)
	if not q:
		return False
	if norm_path == q:
		return True
	# Ensure we only match as a path prefix boundary.
	return norm_path.startswith(q + os.sep)


def enforce_display_name_group_status(state: AppState, canon: str) -> None:
	"""Force Review for all rows in an N-way collision group.

	`canon` must already be casefolded.
	"""
	c = (canon or "").strip().casefold()
	if not c or c == "!":
		return
	group = [
		r
		for r in state.rows
		if (r.display_name or "").strip().casefold() == canon
	]
	if len(group) >= 2:
		for r in group:
			r.status = RowStatus.Review
			if (r.status == RowStatus.Ready) and (r.file_type in {FileType.TaxInvoice, FileType.Proforma}):
				r.checkbox_enabled = True
			else:
				r.checkbox_enabled = False
				r.checked = False
	elif len(group) == 1:
		r = group[0]
		base_status = (
			RowStatus.Ready
			if (r.display_name != "!" and r.file_type != FileType.Unknown)
			else RowStatus.Review
		)
		r.status = base_status
		if (r.status == RowStatus.Ready) and (r.file_type in {FileType.TaxInvoice, FileType.Proforma}):
			r.checkbox_enabled = True
		else:
			r.checkbox_enabled = False
			r.checked = False


def reset_watch_state(state: AppState) -> None:
	state.known_paths.clear()
	state.pending_paths.clear()
	state.active_batch = None
	state.work_queue.clear()


def on_fs_event(state: AppState, path: str) -> None:
	"""Record a discovered stable PDF path into pending work.

	This is pure in-memory state mutation; it does not touch Tk.
	The watcher is responsible for stability detection; we still enforce:
	- pdf extension
	- quarantine exclusion
	- dedupe via known_paths
	"""
	if not state.source_path:
		return
	norm = normalize_path(path)
	if not _is_pdf(norm):
		return
	if _is_under_quarantine(state, norm):
		return
	if norm in state.known_paths:
		return
	state.known_paths.add(norm)
	state.pending_paths.add(norm)


def _batch_sort_key(norm_path: str) -> tuple[str, str]:
	base = os.path.basename(norm_path).lower()
	return (base, norm_path)


def start_next_batch_if_idle(state: AppState) -> list[WorkItem]:
	"""Freeze a deterministic snapshot of pending paths into an active batch.

	Returns the created WorkItems (to be enqueued to a background worker).
	"""
	if state.active_batch is not None:
		return []
	if not state.pending_paths:
		return []

	snapshot = sorted(state.pending_paths, key=_batch_sort_key)
	for p in snapshot:
		state.pending_paths.discard(p)

	batch_id = state.next_batch_id
	state.next_batch_id += 1
	state.active_batch = ActiveBatch(batch_id=batch_id, snapshot_paths=snapshot, total=len(snapshot))
	state.work_queue = [WorkItem(batch_id=batch_id, path=p, index=i) for i, p in enumerate(snapshot)]
	return list(state.work_queue)


def mark_item_running(state: AppState, batch_id: int, path: str) -> None:
	if state.active_batch is None or state.active_batch.batch_id != batch_id:
		return
	norm = normalize_path(path)
	for item in state.work_queue:
		if item.batch_id == batch_id and item.path == norm:
			item.status = "running"
			return


def mark_item_done(state: AppState, batch_id: int, path: str) -> None:
	if state.active_batch is None or state.active_batch.batch_id != batch_id:
		return
	norm = normalize_path(path)
	for item in state.work_queue:
		if item.batch_id == batch_id and item.path == norm:
			if item.status != "done":
				item.status = "done"
				state.active_batch.done_count += 1
			break

	if state.active_batch.done_count >= state.active_batch.total:
		state.active_batch = None
		state.work_queue.clear()


def add_row_from_phase1_result(state: AppState, *, res: Phase1Result) -> bool:
	"""Create a row from a completed Phase-1 result.

	Must be called on the UI thread.
	"""
	orig_norm = normalize_path(res.original_path)
	if not orig_norm:
		return False

	# Slice 05 idempotency: if the same fingerprint arrives again (even via a
	# different path string due to rename-triggered fs events), treat it as
	# already handled and avoid creating a second row.
	if res.fingerprint_sha256 and res.fingerprint_sha256 in state.known_fingerprints:
		state.phase1_completed_paths.add(orig_norm)
		return False

	if orig_norm in state.phase1_completed_paths:
		return False
	state.phase1_completed_paths.add(orig_norm)

	# Update known fingerprints ONLY here (UI thread).
	if res.fingerprint_sha256:
		state.known_fingerprints.add(res.fingerprint_sha256)

	if res.kind == "duplicate_skipped":
		return False

	origin_seq = state.next_row_seq
	row_id = f"p1_{origin_seq:04d}"
	state.next_row_seq += 1

	display_name = res.doc_no.strip() if res.doc_no and res.doc_no != "!" else "!"
	file_name = res.doc_no if res.doc_no and res.doc_no != "!" else "!"
	file_type = res.file_type if isinstance(res.file_type, FileType) else FileType.Unknown
	status = RowStatus.Ready if (display_name != "!" and file_type != FileType.Unknown) else RowStatus.Review

	final_path = normalize_path(res.renamed_path) if res.renamed_path else orig_norm

	state.rows.append(
		RowModel(
			id=row_id,
			file_name=file_name,
			file_type=file_type,
			date_str="",
			account_str="",
			total_str="",
			status=status,
			checked=False,
			checkbox_enabled=False,
			source_path=final_path,
			display_name=display_name,
			fingerprint_sha256=res.fingerprint_sha256 or "",
			origin_seq=origin_seq,
		)
	)

	if display_name != "!":
		enforce_display_name_group_status(state, display_name.casefold())
	return True
