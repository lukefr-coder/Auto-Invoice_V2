from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Literal

from core.filters import FilterModel
from core.row_model import RowModel


WorkStatus = Literal["pending", "running", "done"]


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
	return normalize_path(os.path.join(state.source_path, "_quarantine"))


def _is_under_quarantine(state: AppState, norm_path: str) -> bool:
	q = _quarantine_root(state)
	if not q:
		return False
	if norm_path == q:
		return True
	# Ensure we only match as a path prefix boundary.
	return norm_path.startswith(q + os.sep)


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
