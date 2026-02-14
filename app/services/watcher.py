from __future__ import annotations

import hashlib
import os
import queue
import re
import threading
import time
from dataclasses import dataclass
from dataclasses import dataclass as _dc

from core.app_state import Phase1Result
from core.row_model import FileType


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
	- Excludes <source>/quarantine
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
		quarantine = _norm(os.path.join(source, "quarantine"))

		while not self._stop.is_set():
			try:
				self._scan_once(source, quarantine)
			except Exception:
				# Keep watcher alive; failures will be retried next tick.
				pass
			time.sleep(self._poll_interval_s)

	def _scan_once(self, source: str, quarantine: str) -> None:
		seen_now: set[str] = set()
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
				seen_now.add(norm)
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

		for key in list(self._seen.keys()):
			if key not in seen_now:
				del self._seen[key]


@_dc(frozen=True)
class _Phase1Parsed:
	doc_no: str
	file_type: FileType


_INVALID_WIN_CHARS_RE = re.compile(r'[<>:"/\\|?*]')


def _sanitize_windows_filename_stem(stem: str) -> str:
	stem = (stem or "").strip()
	stem = _INVALID_WIN_CHARS_RE.sub("_", stem)
	stem = stem.rstrip(" .")
	return stem


def _sha256_hex(path: str) -> str:
	h = hashlib.sha256()
	with open(path, "rb") as f:
		for chunk in iter(lambda: f.read(1024 * 1024), b""):
			h.update(chunk)
	return h.hexdigest()


def _classify_file_type_from_text(text: str) -> FileType:
	t = (text or "").upper()
	if "TAX INVOICE" in t:
		return FileType.TaxInvoice
	if "PROFORMA" in t:
		return FileType.Proforma
	if "CREDIT" in t and "NOTE" in t:
		return FileType.Credit
	if "PURCHASE ORDER" in t:
		return FileType.Order
	if "TRANSFER" in t:
		return FileType.Transfer
	return FileType.Unknown


_DOCNO_PATTERNS: list[re.Pattern[str]] = [
	re.compile(
		r"\b(?:INVOICE\s*(?:NO|NUMBER)|INV\s*(?:NO|#)|DOCUMENT\s*(?:NO|NUMBER)|DOC\s*(?:NO|#))\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-_/]{2,})\b",
		re.IGNORECASE,
	),
]


def _extract_doc_no_from_text(text: str) -> str:
	if not text:
		return "!"
	sample = text[:4000]
	cands: set[str] = set()
	for pat in _DOCNO_PATTERNS:
		for m in pat.finditer(sample):
			cand = (m.group(1) or "").strip().rstrip(".:")
			if cand:
				cands.add(cand)
	if len(cands) != 1:
		return "!"
	return next(iter(cands))


def _parse_phase1_from_pdf_page1(path: str) -> _Phase1Parsed:
	try:
		from pypdf import PdfReader  # type: ignore
	except Exception:
		return _Phase1Parsed(doc_no="!", file_type=FileType.Unknown)
	try:
		reader = PdfReader(path)
		if not reader.pages:
			return _Phase1Parsed(doc_no="!", file_type=FileType.Unknown)
		text = reader.pages[0].extract_text() or ""
		doc_no = _extract_doc_no_from_text(text)
		file_type = _classify_file_type_from_text(text)
		return _Phase1Parsed(doc_no=doc_no or "!", file_type=file_type)
	except Exception:
		return _Phase1Parsed(doc_no="!", file_type=FileType.Unknown)


def _choose_collision_free_path(dir_path: str, base_stem: str, *, ext: str = ".pdf") -> str:
	base = _sanitize_windows_filename_stem(base_stem)
	if not base:
		base = "!"
	first = os.path.join(dir_path, base + ext)
	if not os.path.exists(first):
		return first
	i = 2
	while True:
		cand = os.path.join(dir_path, f"{base}__{i}{ext}")
		if not os.path.exists(cand):
			return cand
		i += 1


def _attempt_rename(src_norm: str, target_path: str) -> str | None:
	try:
		if _norm(target_path) == src_norm:
			return src_norm
		os.replace(src_norm, target_path)
		return _norm(target_path)
	except Exception:
		return None


class Phase1Processor:
	"""Slice 05 Phase-1 worker (no Tk, no Core/AppState access).

	Event contract: emits ("running"|"done", batch_id, ORIGINAL_path)
	"""

	def __init__(
		self,
		out_events: queue.Queue[tuple[str, int, str]],
		*,
		delay_s: float = 0.0,
	):
		self._out_events = out_events
		self._delay_s = delay_s
		self._in: queue.Queue[tuple[int, str]] = queue.Queue()
		self._stop = threading.Event()
		self._thread = threading.Thread(target=self._run, name="Phase1Processor", daemon=True)
		self._results_lock = threading.Lock()
		self._results_by_key: dict[tuple[int, str], Phase1Result] = {}
		self._seen_fingerprints: set[str] = set()
		self._canonical_path_by_fp: dict[str, str] = {}

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

	def forget_fingerprint(self, fp: str) -> None:
		fp = (fp or "").strip().lower()
		if not fp:
			return
		self._seen_fingerprints.discard(fp)
		self._canonical_path_by_fp.pop(fp, None)

	def take_result(self, batch_id: int, path: str) -> Phase1Result | None:
		key = (batch_id, _norm(path))
		with self._results_lock:
			return self._results_by_key.pop(key, None)

	def _emit(self, kind: str, batch_id: int, path: str) -> None:
		try:
			self._out_events.put_nowait((kind, batch_id, path))
		except queue.Full:
			pass

	def _store_result(self, res: Phase1Result) -> None:
		with self._results_lock:
			self._results_by_key[(res.batch_id, _norm(res.original_path))] = res

	def _run(self) -> None:
		while not self._stop.is_set():
			batch_id, original_path = self._in.get()
			if batch_id < 0:
				return
			self._emit("running", batch_id, original_path)
			orig_norm = _norm(original_path)
			try:
				if not orig_norm or not os.path.exists(orig_norm):
					continue

				fp = ""
				fp = _sha256_hex(orig_norm)
				if fp in self._seen_fingerprints:
					can_quarantine = True
					# GUARD 1 — Hash reliability: if fp is missing/invalid, do not quarantine.
					# (_sha256_hex normally returns 64 hex chars; invalid values are treated as unreliable.)
					fp_l = (fp or "").lower()
					if len(fp_l) != 64 or any(c not in "0123456789abcdef" for c in fp_l):
						can_quarantine = False

					canonical = self._canonical_path_by_fp.get(fp)
					canonical_norm = _norm(canonical) if canonical else ""
					if canonical_norm and orig_norm == canonical_norm:
						# Canonical file re-seen; never quarantine it.
						can_quarantine = False

					if not canonical_norm:
						# No canonical mapping for a seen fp (e.g. prior processing failed mid-way);
						# treat current path as canonical and never quarantine it.
						can_quarantine = False
						self._canonical_path_by_fp[fp] = orig_norm

					if canonical_norm and not os.path.exists(canonical_norm):
						# Canonical path is stale (file was renamed/moved outside worker);
						# treat current path as canonical and never quarantine it.
						can_quarantine = False
						self._canonical_path_by_fp[fp] = orig_norm

					# GUARD 2 — Canonical fingerprint verification (MANDATORY)
					# Quarantine is allowed ONLY if:
					# 1) canonical exists, 2) canonical exists on disk, 3) SHA256(canonical)==fp,
					# 4) SHA256(current)==fp, 5) current path != canonical.
					try:
						current_verify = _sha256_hex(orig_norm)
					except Exception:
						# Fingerprint computation failed/unreliable; do not quarantine.
						can_quarantine = False
						self._store_result(
							Phase1Result(
								batch_id=batch_id,
								original_path=orig_norm,
								fingerprint_sha256=fp,
								doc_no="!",
								file_type=FileType.Unknown,
								renamed_path="",
								kind="duplicate_skipped",
							)
						)
						continue

					if current_verify != fp:
						# The fp used for duplicate classification is not reliable for this file;
						# do not quarantine.
						can_quarantine = False

					if (not canonical_norm) or (not os.path.exists(canonical_norm)) or (orig_norm == canonical_norm):
						# Canonical missing/stale or re-seen; never quarantine.
						can_quarantine = False
						self._canonical_path_by_fp[fp] = orig_norm

					try:
						canonical_verify = _sha256_hex(canonical_norm)
					except Exception:
						# Canonical fingerprint verification failed; treat mapping as stale.
						can_quarantine = False
						self._canonical_path_by_fp[fp] = orig_norm
						self._store_result(
							Phase1Result(
								batch_id=batch_id,
								original_path=orig_norm,
								fingerprint_sha256=fp,
								doc_no="!",
								file_type=FileType.Unknown,
								renamed_path="",
								kind="duplicate_skipped",
							)
						)
						continue

					if canonical_verify != fp:
						# Canonical content does not match fp; treat mapping as stale.
						can_quarantine = False
						self._canonical_path_by_fp[fp] = orig_norm

					if not can_quarantine:
						self._store_result(
							Phase1Result(
								batch_id=batch_id,
								original_path=orig_norm,
								fingerprint_sha256=fp,
								doc_no="!",
								file_type=FileType.Unknown,
								renamed_path="",
								kind="duplicate_skipped",
							)
						)
						continue

					# IMPORTANT (Slice 05): do not delete duplicates here.
					# Renames can trigger a second fs event and re-queue the renamed file;
					# deleting would remove the only copy.
					try:
						source = os.path.dirname(orig_norm)
						q_dir = os.path.join(source, "quarantine")
						os.makedirs(q_dir, exist_ok=True)

						base_name = os.path.basename(orig_norm)
						stem, ext = os.path.splitext(base_name)
						target = os.path.join(q_dir, base_name)
						if os.path.exists(target):
							i = 2
							while True:
								cand = os.path.join(q_dir, f"{stem}__{i}{ext}")
								if not os.path.exists(cand):
									target = cand
									break
								i += 1

						os.replace(orig_norm, target)
					except Exception:
						# Failure handling: leave file in place and still emit duplicate_skipped.
						pass

					self._store_result(
						Phase1Result(
							batch_id=batch_id,
							original_path=orig_norm,
							fingerprint_sha256=fp,
							doc_no="!",
							file_type=FileType.Unknown,
							renamed_path="",
							kind="duplicate_skipped",
						)
					)
					continue

				self._seen_fingerprints.add(fp)

				parsed = _parse_phase1_from_pdf_page1(orig_norm)
				doc_no = parsed.doc_no if parsed.doc_no else "!"
				file_type = parsed.file_type if isinstance(parsed.file_type, FileType) else FileType.Unknown

				# Determine preferred target
				if doc_no.lower().endswith(".pdf"):
					doc_no = doc_no[:-4]
				safe_doc_stem = _sanitize_windows_filename_stem(doc_no) if doc_no != "!" else ""
				if doc_no != "!" and not safe_doc_stem:
					doc_no = "!"
					file_type = FileType.Unknown

				dir_path = os.path.dirname(orig_norm)
				fp12 = fp[:12]

				preferred_path = (
					_choose_collision_free_path(dir_path, safe_doc_stem)
					if doc_no != "!"
					else _choose_collision_free_path(dir_path, f"!__{fp12}")
				)
				fallback_path = _choose_collision_free_path(dir_path, f"!__{fp12}")

				renamed_norm: str | None = None
				if doc_no != "!":
					renamed_norm = _attempt_rename(orig_norm, preferred_path)
					if renamed_norm is None:
						# CRITICAL: if doc_no-based rename fails, must attempt failure-name fallback.
						renamed_norm = _attempt_rename(orig_norm, fallback_path)
						doc_no = "!"
						file_type = FileType.Unknown
				else:
					renamed_norm = _attempt_rename(orig_norm, fallback_path)

				if renamed_norm is None:
					# Only if both renames fail may we leave in place; force failure markers.
					renamed_norm = orig_norm
					doc_no = "!"
					file_type = FileType.Unknown

				self._canonical_path_by_fp[fp] = _norm(renamed_norm or orig_norm)

				self._store_result(
					Phase1Result(
						batch_id=batch_id,
						original_path=orig_norm,
						fingerprint_sha256=fp,
						doc_no=doc_no,
						file_type=file_type,
						renamed_path=renamed_norm,
						kind="processed",
					)
				)
			except Exception:
				# Keep worker alive, but never drop a file silently.
				if fp:
					try:
						self._seen_fingerprints.discard(fp)
						self._canonical_path_by_fp.pop(fp, None)
					except Exception:
						pass
				self._store_result(
					Phase1Result(
						batch_id=batch_id,
						original_path=orig_norm,
						fingerprint_sha256=fp,
						doc_no="!",
						file_type=FileType.Unknown,
						renamed_path="",
						kind="processed",
					)
				)
			finally:
				if self._delay_s > 0:
					try:
						time.sleep(self._delay_s)
					except Exception:
						pass
				self._emit("done", batch_id, original_path)
