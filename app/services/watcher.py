from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from dataclasses import dataclass as _dc


def _norm(path: str) -> str:
	try:
		abs_path = os.path.abspath(path)
	except Exception:
		abs_path = path
	return os.path.normcase(os.path.normpath(abs_path))


def _is_pdf(path: str) -> bool:
	return path.lower().endswith(".pdf")


def _is_under(root: str, candidate: str) -> bool:
	root = _norm(root)
	candidate = _norm(candidate)
	if candidate == root:
		return True
	return candidate.startswith(root + os.sep)


@dataclass
class _SeenFile:
	size: int
	mtime_ns: int
	stable_ticks: int = 0
	emitted: bool = False


class FolderWatcher:
	"""Polling folder watcher for stable PDFs.

	- Detects *.pdf (case-insensitive)
	- Excludes <source>/_quarantine
	- Emits a path only once it is stable across 2 polls

	Emits normalized absolute paths into out_queue.
	"""

	def __init__(
		self,
		source_path: str,
		out_queue: queue.Queue[str],
		*,
		poll_interval_s: float = 0.25,
		required_stable_ticks: int = 2,
	):
		self._source_path = source_path
		self._out_queue = out_queue
		self._poll_interval_s = poll_interval_s
		self._required_stable_ticks = required_stable_ticks
		self._stop = threading.Event()
		self._thread = threading.Thread(target=self._run, name="FolderWatcher", daemon=True)
		self._seen: dict[str, _SeenFile] = {}

	def start(self) -> None:
		# Only start once; callers should create a new watcher instance to restart.
		if self._thread.is_alive():
			return
		self._thread.start()

	def is_alive(self) -> bool:
		return self._thread.is_alive()

	def stop(self, timeout_s: float = 1.0) -> None:
		self._stop.set()
		try:
			self._thread.join(timeout=timeout_s)
		except Exception:
			pass

	def _run(self) -> None:
		source = _norm(self._source_path)
		if not source or not os.path.isdir(source):
			return
		quarantine = _norm(os.path.join(source, "_quarantine"))

		while not self._stop.is_set():
			try:
				self._scan_once(source, quarantine)
			except Exception:
				# Keep watcher alive; failures will be retried next tick.
				pass
			time.sleep(self._poll_interval_s)

	def _scan_once(self, source: str, quarantine: str) -> None:
		# Walk the source tree, skipping quarantine.
		for root, dirs, files in os.walk(source):
			root_norm = _norm(root)
			# Prevent descending into quarantine.
			if root_norm == quarantine or root_norm.startswith(quarantine + os.sep):
				dirs[:] = []
				continue

			# Also remove quarantine from immediate dir list for efficiency.
			dirs[:] = [d for d in dirs if _norm(os.path.join(root_norm, d)) != quarantine]

			for name in files:
				candidate = os.path.join(root_norm, name)
				if not _is_pdf(candidate):
					continue
				if _is_under(quarantine, candidate):
					continue

				try:
					st = os.stat(candidate)
				except Exception:
					continue

				norm = _norm(candidate)
				prev = self._seen.get(norm)
				cur = (int(st.st_size), int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))))

				if prev is None:
					self._seen[norm] = _SeenFile(size=cur[0], mtime_ns=cur[1], stable_ticks=0, emitted=False)
					continue

				if prev.size == cur[0] and prev.mtime_ns == cur[1]:
					prev.stable_ticks += 1
				else:
					prev.size = cur[0]
					prev.mtime_ns = cur[1]
					prev.stable_ticks = 0
					prev.emitted = False

				if (not prev.emitted) and prev.stable_ticks >= self._required_stable_ticks:
					prev.emitted = True
					try:
						self._out_queue.put_nowait(norm)
					except queue.Full:
						# Drop if UI is overwhelmed; it will be rediscovered later.
						prev.emitted = False


@_dc(frozen=True)
class Phase1StubResult:
	batch_id: int
	original_path: str
	kind: str = "stub_done"


class Phase1StubProcessor:
	"""Slice 04A Phase-1 stub worker.

	- Background thread only; never calls Tk
	- Emits ("running"|"done", batch_id, path)
	- Stores a tiny result retrievable via take_result(...)
	- Performs zero filesystem mutation (optional existence check only)
	"""

	def __init__(
		self,
		out_events: queue.Queue[tuple[str, int, str]],
		*,
		delay_s: float = 0.9,
		check_exists: bool = True,
	):
		self._out_events = out_events
		self._delay_s = delay_s
		self._check_exists = check_exists
		self._in: queue.Queue[tuple[int, str]] = queue.Queue()
		self._stop = threading.Event()
		self._thread = threading.Thread(target=self._run, name="Phase1StubProcessor", daemon=True)
		self._results_lock = threading.Lock()
		self._results_by_key: dict[tuple[int, str], Phase1StubResult] = {}

	def start(self) -> None:
		self._thread.start()

	def stop(self, timeout_s: float = 1.0) -> None:
		self._stop.set()
		try:
			self._in.put_nowait((-1, ""))
		except Exception:
			pass
		try:
			self._thread.join(timeout=timeout_s)
		except Exception:
			pass

	def enqueue(self, batch_id: int, path: str) -> None:
		self._in.put((batch_id, path))

	def take_result(self, batch_id: int, path: str) -> Phase1StubResult | None:
		key = (batch_id, _norm(path))
		with self._results_lock:
			return self._results_by_key.pop(key, None)

	def _emit(self, kind: str, batch_id: int, path: str) -> None:
		try:
			self._out_events.put_nowait((kind, batch_id, path))
		except queue.Full:
			pass

	def _store_result(self, res: Phase1StubResult) -> None:
		with self._results_lock:
			self._results_by_key[(res.batch_id, _norm(res.original_path))] = res

	def _run(self) -> None:
		while not self._stop.is_set():
			batch_id, path = self._in.get()
			if batch_id < 0:
				return
			self._emit("running", batch_id, path)
			norm = _norm(path)
			if (not self._check_exists) or os.path.exists(norm):
				self._store_result(Phase1StubResult(batch_id=batch_id, original_path=norm))
			if self._delay_s > 0:
				time.sleep(self._delay_s)
			self._emit("done", batch_id, path)
