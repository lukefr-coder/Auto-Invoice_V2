from __future__ import annotations

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import queue
import os
import time
import re

from core.app_state import AppState
from core.filters import FilterModel
from core.mutations import deposit_ready_rows, resolve_review_row_manual, set_dest_path, set_source_path
from core.app_state import (
	add_row_from_phase1_result,
	enforce_display_name_group_status,
	mark_item_done,
	mark_item_running,
	on_fs_event,
	reset_watch_state,
	start_next_batch_if_idle,
)
from core.row_model import FileType, RowStatus
from services.watcher import (
	FolderWatcher,
	Phase1Processor,
	_attempt_rename,
	_choose_collision_free_path,
	_norm,
	_sanitize_windows_filename_stem,
)
from state import persistence
from state.persistence import load_settings, save_settings
from ui.grid import FilesGrid
from ui.pdf_preview import PdfPage1Preview
from ui.status_bar import StatusBar


def _white_address_entry(master: tk.Misc, textvariable: tk.StringVar) -> tk.Entry:
	entry = tk.Entry(
		master,
		textvariable=textvariable,
		state="readonly",
		relief="solid",
		bd=1,
		background="white",
		readonlybackground="white",
	)
	entry.configure(highlightthickness=0)
	return entry


def _upper_var(var: tk.StringVar) -> None:
	try:
		value = var.get() or ""
	except Exception:
		return
	upper = value.upper()
	if upper != value:
		try:
			var.set(upper)
		except Exception:
			return


class AppWindow(ttk.Frame):
	def __init__(self, master: tk.Tk):
		super().__init__(master, padding=10)

		self.master = master
		self.state = AppState(
			source_path="",
			dest_path="",
			filters=FilterModel(type_filter="All", status_filter="All"),
			rows=[],
		)
		self._settings = load_settings()
		self._apply_persisted_settings()

		self.source_var = tk.StringVar(value=self.state.source_path)
		self.dest_var = tk.StringVar(value=self.state.dest_path)
		self.export_var = tk.StringVar(value=self._settings.get(self._export_key(), ""))

		self.viewing_text_var = tk.StringVar(value="")
		self.file_count_var = tk.StringVar(value="Files: 0")
		self._sync_viewing_text()
		self.source_var.trace_add("write", lambda *_: self._sync_viewing_text())

		self._build_layout()
		self._sync_file_count()
		self._center_window_once()

		# Slice 03/04A: background watching + Phase-1 stub processing (no Tk calls off-thread).
		self._fs_event_queue: queue.Queue[str] = queue.Queue()
		self._worker_event_queue: queue.Queue[tuple[str, int, str]] = queue.Queue()
		self._watcher: FolderWatcher | None = None
		self._worker = Phase1Processor(self._worker_event_queue)
		self._worker.start()
		self._batch_done_linger_until: float = 0.0
		self._batch_done_linger_text: str = ""
		self._restart_watcher_if_possible()
		self.after(100, self._poll_background)

		try:
			self.master.protocol("WM_DELETE_WINDOW", self._on_close)
		except Exception:
			pass

	def _source_key(self) -> str:
		return persistence._KEYS[0]

	def _dest_key(self) -> str:
		return persistence._KEYS[1]

	def _export_key(self) -> str:
		return persistence._KEYS[2]

	def _apply_persisted_settings(self) -> None:
		source = self._settings.get(self._source_key(), "")
		dest = self._settings.get(self._dest_key(), "")
		if isinstance(source, str) and source:
			set_source_path(self.state, source)
		if isinstance(dest, str) and dest:
			set_dest_path(self.state, dest)

	def _persist_settings(self) -> None:
		self._settings[self._source_key()] = self.state.source_path
		self._settings[self._dest_key()] = self.state.dest_path
		self._settings[self._export_key()] = self.export_var.get()
		save_settings(self._settings)

	def _on_close(self) -> None:
		# Ensure background threads stop cleanly.
		try:
			if self._watcher is not None:
				self._watcher.stop()
		except Exception:
			pass
		self._watcher = None
		try:
			self._worker.stop()
		except Exception:
			pass
		try:
			self.master.destroy()
		except Exception:
			pass

	def _restart_watcher_if_possible(self) -> None:
		# Stop existing watcher.
		if self._watcher is not None:
			try:
				self._watcher.stop()
			except Exception:
				pass
			self._watcher = None

		reset_watch_state(self.state)

		if not self.state.source_path:
			return
		# Only start when the source path is a valid directory.
		# (If it's not yet valid, the UI poll loop will attempt again later.)
		if not os.path.isdir(self.state.source_path):
			return

		self._watcher = FolderWatcher(self.state.source_path, self._fs_event_queue)
		self._watcher.start()

	def _poll_background(self) -> None:
		# Ensure watcher is running when a valid Source exists.
		# This keeps wiring reliable even if Source is loaded after init.
		if self.state.source_path and (
			self._watcher is None or (hasattr(self._watcher, "is_alive") and not self._watcher.is_alive())
		):
			self._restart_watcher_if_possible()

		# Drain filesystem events.
		drained_any = False
		while True:
			try:
				path = self._fs_event_queue.get_nowait()
			except queue.Empty:
				break
			drained_any = True
			on_fs_event(self.state, path)

		# Drain worker events.
		prev_batch = self.state.active_batch
		while True:
			try:
				kind, batch_id, path = self._worker_event_queue.get_nowait()
			except queue.Empty:
				break
			if kind == "running":
				mark_item_running(self.state, batch_id, path)
			elif kind == "done":
				mark_item_done(self.state, batch_id, path)
				res = self._worker.take_result(batch_id, path)
				if res is not None:
					if (
						res.kind == "processed"
						and res.renamed_path
						and _norm(res.renamed_path) != _norm(res.original_path)
					):
						try:
							orig_pn = _norm(res.original_path)
							self.state.known_paths.discard(orig_pn)
							self.state.pending_paths.discard(orig_pn)
							self.state.phase1_completed_paths.discard(orig_pn)
						except Exception:
							pass
					if res.kind == "duplicate_skipped":
						pn = _norm(res.original_path)
						try:
							self.state.known_paths.discard(pn)
							self.state.pending_paths.discard(pn)
							self.state.phase1_completed_paths.discard(pn)
						except Exception:
							pass
						continue
					if add_row_from_phase1_result(self.state, res=res):
						self.files_grid.refresh()

		# Reconcile deleted/missing files (UI-thread only): remove non-Processed rows whose source no longer exists,
		# and recompute collision groups for impacted display names.
		removed_canons: set[str] = set()
		removed_any = False
		for r in list(self.state.rows):
			if r.status == RowStatus.Processed:
				continue
			p = (r.source_path or "").strip()
			if p and (not os.path.exists(p)):
				fp = (getattr(r, "fingerprint_sha256", "") or "").strip().lower()
				if fp:
					try:
						self._worker.forget_fingerprint(fp)
					except Exception:
						pass
					try:
						if hasattr(self.state, "known_fingerprints"):
							self.state.known_fingerprints.discard(fp)
					except Exception:
						pass
				removed_any = True
				canon = (r.display_name or "").strip().casefold()
				if canon and canon != "!":
					removed_canons.add(canon)
				pn = _norm(p)
				try:
					self.state.known_paths.discard(pn)
					self.state.pending_paths.discard(pn)
					self.state.phase1_completed_paths.discard(pn)
				except Exception:
					pass
				try:
					self.state.rows.remove(r)
				except Exception:
					pass
		for canon in removed_canons:
			enforce_display_name_group_status(self.state, canon)
		if removed_any:
			self.files_grid.refresh()
			self._sync_file_count()

		# If the active batch just completed, linger the final status briefly.
		if prev_batch is not None and self.state.active_batch is None and prev_batch.done_count >= prev_batch.total:
			pending = len(self.state.pending_paths)
			self._batch_done_linger_until = time.monotonic() + 1.0
			self._batch_done_linger_text = (
				f"Batch {prev_batch.batch_id}: {prev_batch.total}/{prev_batch.total} • Pending files: {pending}"
			)

		# Start next batch if idle.
		new_items = start_next_batch_if_idle(self.state)
		for item in new_items:
			self._worker.enqueue(item.batch_id, item.path)

		# Update status text.
		self._render_background_status(did_discover=drained_any)
		self._sync_deposit_enabled()
		self.after(100, self._poll_background)

	def _render_background_status(self, *, did_discover: bool) -> None:
		if not self.state.source_path:
			return
		# Don't override transient UI feedback like "Saved".
		if self.status_bar.has_transient_message():
			return

		pending = len(self.state.pending_paths)
		if self.state.active_batch is not None:
			done = self.state.active_batch.done_count
			total = self.state.active_batch.total
			batch_id = self.state.active_batch.batch_id
			self.status_bar.set_info(f"Batch {batch_id}: {done}/{total} • Pending files: {pending}")
			return

		if self._batch_done_linger_until and time.monotonic() < self._batch_done_linger_until:
			self.status_bar.set_info(self._batch_done_linger_text)
			return
		self._batch_done_linger_until = 0.0
		self._batch_done_linger_text = ""

		if pending > 0:
			self.status_bar.set_info(f"Pending files: {pending}")
			return

		# Idle: blank text.
		self.status_bar.set_info("")

	def _center_window_once(self) -> None:
		# Center only on initial creation; do not affect runtime resizing/maximize behavior.
		try:
			self.master.update_idletasks()

			win_w = self.master.winfo_width()
			win_h = self.master.winfo_height()

			# Windows work-area centering (excludes taskbar) via SPI_GETWORKAREA.
			work_x = 0
			work_y = 0
			work_w = 0
			work_h = 0
			try:
				import ctypes

				SPI_GETWORKAREA = 48

				class RECT(ctypes.Structure):
					_fields_ = [
						("left", ctypes.c_long),
						("top", ctypes.c_long),
						("right", ctypes.c_long),
						("bottom", ctypes.c_long),
					]

				rect = RECT()
				ok = ctypes.windll.user32.SystemParametersInfoW(
					SPI_GETWORKAREA,
					0,
					ctypes.byref(rect),
					0,
				)
				if ok:
					work_x = int(rect.left)
					work_y = int(rect.top)
					work_w = int(rect.right - rect.left)
					work_h = int(rect.bottom - rect.top)
			except Exception:
				pass

			if work_w <= 1 or work_h <= 1:
				work_x = 0
				work_y = 0
				work_w = self.master.winfo_screenwidth()
				work_h = self.master.winfo_screenheight()

			if win_w <= 1 or win_h <= 1:
				return

			x = work_x + (work_w - win_w) // 2
			y = work_y + (work_h - win_h) // 2
			if x < 0:
				x = 0
			if y < 0:
				y = 0
			self.master.geometry(f"+{x}+{y}")
		except Exception:
			return

	def _center_toplevel(self, win: tk.Toplevel) -> None:
		try:
			win.update_idletasks()
		except Exception:
			return

		# Prefer centering over the main window.
		try:
			parent = self.master
			parent.update_idletasks()
			pw = int(parent.winfo_width())
			ph = int(parent.winfo_height())
			px = int(parent.winfo_rootx())
			py = int(parent.winfo_rooty())
		except Exception:
			pw = ph = px = py = 0

		try:
			ww = int(win.winfo_width())
			wh = int(win.winfo_height())
		except Exception:
			ww = wh = 0

		if pw > 1 and ph > 1 and ww > 1 and wh > 1:
			x = px + (pw - ww) // 2
			y = py + (ph - wh) // 2
		else:
			try:
				sw = int(win.winfo_screenwidth())
				sh = int(win.winfo_screenheight())
			except Exception:
				return
			x = (sw - max(ww, 1)) // 2
			y = (sh - max(wh, 1)) // 2

		if x < 0:
			x = 0
		if y < 0:
			y = 0
		try:
			win.geometry(f"+{x}+{y}")
		except Exception:
			return

	def _build_layout(self) -> None:
		self.grid(row=0, column=0, sticky="nsew")
		self.master.rowconfigure(0, weight=1)
		self.master.columnconfigure(0, weight=1)

		self.columnconfigure(0, weight=1)
		self.rowconfigure(2, weight=1)

		# Input
		input_frame = ttk.LabelFrame(self, text="Input", padding=(10, 6))
		input_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
		input_frame.columnconfigure(0, weight=1)
		_white_address_entry(input_frame, self.source_var).grid(row=0, column=0, sticky="ew")
		ttk.Button(input_frame, text="Browse...", command=self._browse_source).grid(
			row=0, column=1, sticky="e", padx=(8, 0)
		)

		# Options
		options_frame = ttk.LabelFrame(self, text="Options", padding=(10, 6))
		options_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
		options_frame.columnconfigure(0, weight=1)

		left_opts = ttk.Frame(options_frame)
		left_opts.grid(row=0, column=0, sticky="w")
		ttk.Button(left_opts, text="...", width=3, state="disabled").grid(row=0, column=0, padx=(0, 8))
		ttk.Button(left_opts, text="Export Data (.xlsx)", state="disabled").grid(row=0, column=1)
		ttk.Button(options_frame, text="⚙ Calibration", state="disabled").grid(row=0, column=1, sticky="e")

		# Files
		files_frame = ttk.LabelFrame(self, text="Files", padding=(10, 6))
		files_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
		files_frame.columnconfigure(0, weight=1)
		files_frame.rowconfigure(1, weight=1)

		header_strip = ttk.Frame(files_frame)
		header_strip.grid(row=0, column=0, sticky="ew", pady=(0, 6))
		header_strip.columnconfigure(0, weight=1)
		header_strip.columnconfigure(1, weight=1)
		header_strip.columnconfigure(2, weight=1)

		ttk.Label(header_strip, text="").grid(
			row=0, column=0, sticky="w"
		)
		ttk.Label(header_strip, textvariable=self.viewing_text_var).grid(row=0, column=1)

		right_hdr = ttk.Frame(header_strip)
		right_hdr.grid(row=0, column=2, sticky="e")
		ttk.Label(right_hdr, textvariable=self.file_count_var).grid(row=0, column=0, padx=(0, 6))
		ttk.Button(right_hdr, text="🗑", width=3, state="disabled").grid(row=0, column=1)

		table_frame = ttk.Frame(files_frame)
		table_frame.grid(row=1, column=0, sticky="nsew")
		table_frame.columnconfigure(0, weight=1)
		table_frame.rowconfigure(0, weight=1)

		# Grid renders only from AppState.
		self.files_grid = FilesGrid(table_frame, self.state)
		self.files_grid.grid(row=0, column=0, sticky="nsew")
		self.files_grid.on_visible_count_changed = lambda *_: self._sync_file_count()
		self.files_grid.on_manual_input_requested = self._manual_input_for_row
		self.files_grid.on_collision_review_requested = self._show_collision_review_dialog

		bottom_strip = ttk.Frame(files_frame)
		bottom_strip.grid(row=2, column=0, sticky="ew", pady=(6, 0))
		ttk.Button(bottom_strip, text="Open folder", state="disabled").grid(row=0, column=0, padx=(0, 8))
		ttk.Button(bottom_strip, text="Highlights off", state="disabled").grid(row=0, column=1, padx=(0, 8))
		ttk.Button(bottom_strip, text="Clear highlights", state="disabled").grid(row=0, column=2, padx=(0, 8))
		ttk.Button(bottom_strip, text="Copy Debug Data", state="disabled").grid(row=0, column=3)

		# Output
		output_frame = ttk.LabelFrame(self, text="Output", padding=(10, 6))
		output_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))
		output_frame.columnconfigure(1, weight=1)
		ttk.Button(output_frame, text="Browse...", command=self._browse_dest).grid(row=0, column=0, padx=(0, 8))
		_white_address_entry(output_frame, self.dest_var).grid(row=0, column=1, sticky="ew")
		self.deposit_btn = ttk.Button(
			output_frame,
			text="Deposit Files",
			state="disabled",
			command=self._on_deposit_clicked,
		)
		self.deposit_btn.grid(row=0, column=2, padx=(8, 0))

		self.status_bar = StatusBar(self)
		self.status_bar.grid(row=4, column=0, sticky="ew")

	def _sync_viewing_text(self) -> None:
		self.viewing_text_var.set(f"Viewing: {self.source_var.get()}")

	def _sync_file_count(self) -> None:
		count = self.files_grid.get_visible_count()
		self.file_count_var.set(f"Files: {count}")

	def _sync_deposit_enabled(self) -> None:
		try:
			has_ready = any(r.status == RowStatus.Ready for r in self.state.rows)
		except Exception:
			return
		try:
			self.deposit_btn.configure(state=("normal" if has_ready else "disabled"))
		except Exception:
			pass

	def _on_deposit_clicked(self) -> None:
		dest = (self.state.dest_path or "").strip()
		if dest and os.path.isdir(dest):
			try:
				existing = {name.casefold() for name in os.listdir(dest)}
			except Exception:
				existing = set()
			collided_any = False
			if existing:
				for row in self.state.rows:
					if row.status != RowStatus.Ready:
						continue
					stem = _sanitize_windows_filename_stem(row.display_name)
					fname = f"{stem}.pdf"
					if fname.casefold() in existing:
						row.status = RowStatus.Review
						collided_any = True
		else:
			collided_any = False

		changed = deposit_ready_rows(self.state)
		if changed or collided_any:
			self.files_grid.refresh()
			self._sync_file_count()
		self._sync_deposit_enabled()

	def _browse_source(self) -> None:
		selected = self._browse_directory(self.source_var.get() or None)
		if not selected:
			return
		set_source_path(self.state, selected)
		self.source_var.set(self.state.source_path)
		self._persist_settings()
		self._restart_watcher_if_possible()
		self.status_bar.set_success("Saved")
		self._sync_deposit_enabled()

	def _browse_dest(self) -> None:
		selected = self._browse_directory(self.dest_var.get() or None)
		if not selected:
			return
		set_dest_path(self.state, selected)
		self.dest_var.set(self.state.dest_path)
		self._persist_settings()
		self.status_bar.set_success("Saved")

	def _browse_export(self) -> None:
		selected = self._browse_directory(self.export_var.get() or None)
		if not selected:
			return
		self.export_var.set(selected)
		self._persist_settings()
		self.status_bar.set_success("Saved")

	@staticmethod
	def _browse_directory(initial: str | None) -> str:
		selected = filedialog.askdirectory(initialdir=initial, mustexist=False)
		return selected or ""

	def _manual_input_for_row(self, row_id: str) -> None:
		row = next((r for r in self.state.rows if r.id == row_id), None)
		if row is None:
			return
		if row.status != RowStatus.Review:
			return

		old_display_name = row.display_name

		result = self._show_manual_input_dialog(
			initial_doc_no=row.file_name,
			initial_file_type=row.file_type,
			pdf_path=row.source_path,
		)
		if result is None:
			return
		doc_no, file_type = result
		doc_no = _sanitize_windows_filename_stem(doc_no)
		if not doc_no or doc_no == "!":
			messagebox.showwarning("Manual Input", "Document number is invalid.")
			return

		new_display_name = doc_no
		if new_display_name == old_display_name:
			old_status = row.status
			row.file_type = file_type
			row.status = old_status
			self.files_grid.refresh()
			self.status_bar.set_success("Saved")
			return

		src_path = row.source_path
		if not src_path:
			messagebox.showerror("Manual Input", "This row has no source path.")
			return
		dir_path = os.path.dirname(src_path)
		if not dir_path or not os.path.isdir(dir_path):
			messagebox.showerror("Manual Input", "Source folder is missing or invalid.")
			return

		target = _choose_collision_free_path(dir_path, doc_no, ext=".pdf")
		renamed_norm = _attempt_rename(_norm(src_path), target)
		if renamed_norm is None:
			messagebox.showerror(
				"Manual Input",
				"Rename failed. The file may be open, missing, or blocked by permissions.",
			)
			return

		ok = resolve_review_row_manual(
			self.state,
			row_id=row_id,
			doc_no=doc_no,
			file_type=file_type,
			new_source_path=renamed_norm,
		)
		if ok:
			self.files_grid.refresh()
			self.status_bar.set_success("Saved")

	def _show_collision_review_dialog(self, row_id: str) -> None:
		row = next((r for r in self.state.rows if r.id == row_id), None)
		if row is None:
			return
		c = (row.display_name or "").strip().casefold()
		if not c or c == "!":
			return
		dn = (row.display_name or "").strip()
		if not dn or dn == "!":
			return
		canon = dn.casefold()

		group2 = [
			r
			for r in self.state.rows
			if (r.display_name or "").strip().casefold() == canon
		]
		if row.status == RowStatus.Review and len(group2) == 2:
			row_a, row_b = sorted(group2, key=lambda r: r.id)

			resolver = tk.Toplevel(self)
			resolver.title("Collision Review")
			resolver.resizable(True, True)
			try:
				resolver.transient(self.master)
			except Exception:
				pass
			resolver.minsize(900, 520)

			main2 = ttk.Frame(resolver, padding=12)
			main2.grid(row=0, column=0, sticky="nsew")
			resolver.columnconfigure(0, weight=1)
			resolver.rowconfigure(0, weight=1)
			main2.columnconfigure(0, weight=1)
			main2.columnconfigure(1, weight=1)
			main2.rowconfigure(0, weight=1)

			pane_a = ttk.Labelframe(main2, text=f"Row A: {row_a.id}")
			pane_a.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
			pane_a.columnconfigure(0, weight=1)
			pane_a.rowconfigure(0, weight=1)

			pane_b = ttk.Labelframe(main2, text=f"Row B: {row_b.id}")
			pane_b.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
			pane_b.columnconfigure(0, weight=1)
			pane_b.rowconfigure(0, weight=1)

			content_a = ttk.Frame(pane_a, padding=10)
			content_a.grid(row=0, column=0, sticky="nsew")
			content_a.columnconfigure(0, weight=1)
			content_a.rowconfigure(0, weight=1)
			prev_a_container = ttk.Frame(content_a)
			prev_a_container.grid(row=0, column=0, sticky="nsew")
			prev_a_container.columnconfigure(0, weight=1)
			prev_a_container.rowconfigure(0, weight=1)
			prev_a = PdfPage1Preview(prev_a_container)
			prev_a.grid(row=0, column=0, sticky="nsew")
			prev_a.set_pdf_path(row_a.source_path)
			ttk.Label(content_a, text="Rename").grid(row=1, column=0, sticky="w", pady=(10, 0))
			rename_a_var = tk.StringVar(value=dn)
			_rename_a = ttk.Entry(content_a, width=40, textvariable=rename_a_var)
			_rename_a.grid(row=2, column=0, sticky="ew")
			ft_a = getattr(row_a.file_type, "value", str(row_a.file_type))
			ttk.Label(content_a, text=f"Type: {ft_a}").grid(row=3, column=0, sticky="w", pady=(10, 0))

			content_b = ttk.Frame(pane_b, padding=10)
			content_b.grid(row=0, column=0, sticky="nsew")
			content_b.columnconfigure(0, weight=1)
			content_b.rowconfigure(0, weight=1)
			prev_b_container = ttk.Frame(content_b)
			prev_b_container.grid(row=0, column=0, sticky="nsew")
			prev_b_container.columnconfigure(0, weight=1)
			prev_b_container.rowconfigure(0, weight=1)
			prev_b = PdfPage1Preview(prev_b_container)
			prev_b.grid(row=0, column=0, sticky="nsew")
			prev_b.set_pdf_path(row_b.source_path)
			ttk.Label(content_b, text="Rename").grid(row=1, column=0, sticky="w", pady=(10, 0))
			rename_b_var = tk.StringVar(value=f"{dn}(1)")
			rename_a_var.trace_add("write", lambda *_: _upper_var(rename_a_var))
			rename_b_var.trace_add("write", lambda *_: _upper_var(rename_b_var))
			_rename_b = ttk.Entry(content_b, width=40, textvariable=rename_b_var)
			_rename_b.grid(row=2, column=0, sticky="ew")
			ft_b = getattr(row_b.file_type, "value", str(row_b.file_type))
			ttk.Label(content_b, text=f"Type: {ft_b}").grid(row=3, column=0, sticky="w", pady=(10, 0))

			def _sanitized_stem(raw: str) -> str:
				return _sanitize_windows_filename_stem((raw or "").strip())

			def _sync_save_enabled(*_args) -> None:
				a = _sanitized_stem(rename_a_var.get())
				b = _sanitized_stem(rename_b_var.get())
				a = (a or "").upper()
				b = (b or "").upper()
				base_pat = r"^\d{6}([A-Z])?$"
				base_a = a[:-3] if (len(a) >= 3 and a[-3] == "(" and a[-2] in "123456789" and a[-1] == ")") else a
				base_b = b[:-3] if (len(b) >= 3 and b[-3] == "(" and b[-2] in "123456789" and b[-1] == ")") else b
				base_a_ok = re.fullmatch(base_pat, base_a) is not None
				base_b_ok = re.fullmatch(base_pat, base_b) is not None
				ok = bool(a) and bool(b) and a != "!" and b != "!" and a.casefold() != b.casefold() and base_a_ok and base_b_ok
				try:
					save_btn.configure(state=("normal" if ok else "disabled"))
				except Exception:
					pass

			def _on_save() -> None:
				stem_a = _sanitized_stem(rename_a_var.get())
				stem_b = _sanitized_stem(rename_b_var.get())
				if (not stem_a) or (not stem_b) or stem_a == "!" or stem_b == "!" or stem_a.casefold() == stem_b.casefold():
					messagebox.showwarning("Collision Review", "Rename values are invalid or not distinct.")
					_sync_save_enabled()
					return

				src_a = row_a.source_path
				src_b = row_b.source_path
				if not src_a or not src_b:
					messagebox.showerror("Collision Review", "One or more rows have no source path.")
					return

				dir_a = os.path.dirname(src_a)
				dir_b = os.path.dirname(src_b)
				if not dir_a or not dir_b or _norm(dir_a) != _norm(dir_b):
					messagebox.showerror("Collision Review", "Both files must be in the same folder to resolve a 2-row collision.")
					return
				if not os.path.isdir(dir_a):
					messagebox.showerror("Collision Review", "Source folder is missing or invalid.")
					return

				final_a = os.path.join(dir_a, f"{stem_a}.pdf")
				final_b = os.path.join(dir_a, f"{stem_b}.pdf")

				orig_norms = {_norm(src_a), _norm(src_b)}
				final_a_norm = _norm(final_a)
				final_b_norm = _norm(final_b)
				if os.path.exists(final_a) and final_a_norm not in orig_norms:
					messagebox.showerror("Collision Review", "Target name for Row A already exists.")
					return
				if os.path.exists(final_b) and final_b_norm not in orig_norms:
					messagebox.showerror("Collision Review", "Target name for Row B already exists.")
					return

				# Allocate two deterministic temp paths.
				temp_paths: list[str] = []
				for counter in range(10):
					cand = os.path.join(dir_a, f".__ai_tmp__collision__{row_id}__{counter}.pdf")
					if os.path.exists(cand):
						continue
					cand_norm = _norm(cand)
					if cand_norm in orig_norms or cand_norm in {final_a_norm, final_b_norm}:
						continue
					temp_paths.append(cand)
					if len(temp_paths) >= 2:
						break
				if len(temp_paths) < 2:
					messagebox.showerror("Collision Review", "Unable to allocate temporary filenames.")
					return
				temp_a, temp_b = temp_paths[0], temp_paths[1]

				# Phase 1: move both out of the way.
				temp_a_norm = _attempt_rename(_norm(src_a), temp_a)
				if temp_a_norm is None:
					messagebox.showerror(
						"Collision Review",
						"Rename failed. The file may be open, missing, or blocked by permissions.",
					)
					return
				temp_b_norm = _attempt_rename(_norm(src_b), temp_b)
				if temp_b_norm is None:
					_attempt_rename(_norm(temp_a_norm), src_a)
					messagebox.showerror(
						"Collision Review",
						"Rename failed. No changes were applied.",
					)
					return

				# Phase 2: apply exact final targets.
				new_a_norm = _attempt_rename(_norm(temp_a_norm), final_a)
				if new_a_norm is None:
					_attempt_rename(_norm(temp_a_norm), src_a)
					_attempt_rename(_norm(temp_b_norm), src_b)
					messagebox.showerror(
						"Collision Review",
						"Rename failed. No changes were applied.",
					)
					return
				new_b_norm = _attempt_rename(_norm(temp_b_norm), final_b)
				if new_b_norm is None:
					# Best-effort rollback.
					_attempt_rename(_norm(new_a_norm), temp_a)
					_attempt_rename(_norm(temp_a), src_a)
					_attempt_rename(_norm(temp_b_norm), src_b)
					messagebox.showerror(
						"Collision Review",
						"Rename failed. No changes were applied.",
					)
					return

				ok_a = resolve_review_row_manual(
					self.state,
					row_id=row_a.id,
					doc_no=stem_a,
					file_type=row_a.file_type,
					new_source_path=new_a_norm,
				)
				ok_b = resolve_review_row_manual(
					self.state,
					row_id=row_b.id,
					doc_no=stem_b,
					file_type=row_b.file_type,
					new_source_path=new_b_norm,
				)
				if not (ok_a and ok_b):
					messagebox.showerror("Collision Review", "Save failed.")
					return

				self.files_grid.refresh()
				self.status_bar.set_success("Saved")
				resolver.destroy()

			btns2 = ttk.Frame(resolver, padding=(12, 0, 12, 12))
			btns2.grid(row=1, column=0, sticky="e")
			save_btn = ttk.Button(btns2, text="Save", state="disabled", command=_on_save)
			save_btn.grid(row=0, column=0, padx=(0, 8))
			ttk.Button(btns2, text="Cancel", command=resolver.destroy).grid(row=0, column=1)

			rename_a_var.trace_add("write", _sync_save_enabled)
			rename_b_var.trace_add("write", _sync_save_enabled)
			_sync_save_enabled()

			resolver.bind("<Escape>", lambda _e: resolver.destroy())
			try:
				resolver.update_idletasks()
			except Exception:
				pass
			self._center_toplevel(resolver)

			try:
				resolver.grab_set()
			except Exception:
				pass
			self.master.wait_window(resolver)
			return

		win = tk.Toplevel(self)
		win.title("Collision Review")
		win.resizable(True, True)
		try:
			win.transient(self.master)
		except Exception:
			pass
		win.minsize(640, 320)

		main = ttk.Frame(win, padding=12)
		main.grid(row=0, column=0, sticky="nsew")
		win.columnconfigure(0, weight=1)
		win.rowconfigure(0, weight=1)
		main.columnconfigure(0, weight=1)
		main.rowconfigure(1, weight=1)

		ttk.Label(
			main,
			text="These rows share the same document name and require review.",
		).grid(row=0, column=0, sticky="w")

		list_frame = ttk.Frame(main)
		list_frame.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
		list_frame.columnconfigure(0, weight=1)
		list_frame.rowconfigure(0, weight=1)

		list_var = tk.StringVar(value=[])
		listbox = tk.Listbox(list_frame, listvariable=list_var, height=10, exportselection=False)
		listbox.grid(row=0, column=0, sticky="nsew")
		vsb = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
		vsb.grid(row=0, column=1, sticky="ns")
		listbox.configure(yscrollcommand=vsb.set)

		selected_ids: list[str] = []

		def _format_row(r) -> str:
			base = os.path.basename(r.source_path or "")
			ft = getattr(r.file_type, "value", str(r.file_type))
			return f"{r.id}  |  {base}  |  {ft}"

		def _recompute_group() -> list:
			return [
				r
				for r in self.state.rows
				if (r.display_name or "").strip().casefold() == canon
			]

		def _refresh_list() -> None:
			group = _recompute_group()
			selected_ids.clear()
			items = [_format_row(r) for r in group]
			list_var.set(items)
			try:
				listbox.selection_clear(0, "end")
			except Exception:
				pass
			try:
				manual_btn.configure(state="disabled")
			except Exception:
				pass

		def _selected_row_id() -> str:
			try:
				sel = listbox.curselection()
			except Exception:
				sel = ()
			if not sel:
				return ""
			idx = int(sel[0])
			group = _recompute_group()
			if idx < 0 or idx >= len(group):
				return ""
			return group[idx].id

		def _on_select(_evt=None) -> None:
			sid = _selected_row_id()
			manual_btn.configure(state=("normal" if sid else "disabled"))

		listbox.bind("<<ListboxSelect>>", _on_select, add=True)

		btns = ttk.Frame(main)
		btns.grid(row=2, column=0, sticky="e", pady=(12, 0))

		def _do_manual_input() -> None:
			sid = _selected_row_id()
			if not sid:
				return
			self._manual_input_for_row(sid)
			if not win.winfo_exists():
				return
			group = _recompute_group()
			if len(group) < 2:
				win.destroy()
				return
			_refresh_list()

		manual_btn = ttk.Button(btns, text="Manual Input...", command=_do_manual_input, state="disabled")
		manual_btn.grid(row=0, column=0, padx=(0, 8))
		ttk.Button(btns, text="Close", command=win.destroy).grid(row=0, column=1)

		_refresh_list()

		win.bind("<Escape>", lambda _e: win.destroy())
		try:
			win.update_idletasks()
		except Exception:
			pass
		self._center_toplevel(win)

		try:
			win.grab_set()
		except Exception:
			pass
		self.master.wait_window(win)

	def _show_manual_input_dialog(
		self,
		*,
		initial_doc_no: str,
		initial_file_type: FileType,
		pdf_path: str,
	) -> tuple[str, FileType] | None:
		win = tk.Toplevel(self)
		win.title("Manual Input")
		win.resizable(True, True)
		try:
			win.transient(self.master)
		except Exception:
			pass
		win.geometry("900x600")
		win.minsize(700, 450)

		main = ttk.Frame(win, padding=12)
		main.grid(row=0, column=0, sticky="nsew")
		win.columnconfigure(0, weight=1)
		win.rowconfigure(0, weight=1)
		main.columnconfigure(0, weight=0)
		main.columnconfigure(1, weight=1)
		main.rowconfigure(0, weight=1)

		left = ttk.Frame(main)
		left.grid(row=0, column=0, sticky="ns", padx=(0, 12))

		preview_container = ttk.Frame(main)
		preview_container.grid(row=0, column=1, sticky="nsew")
		preview_container.grid_propagate(False)
		preview_container.columnconfigure(0, weight=1)
		preview_container.rowconfigure(0, weight=1)
		preview = PdfPage1Preview(preview_container)
		preview.grid(row=0, column=0, sticky="nsew")
		preview.set_pdf_path(pdf_path)

		ttk.Label(left, text="Document No").grid(row=0, column=0, sticky="w")
		doc_var = tk.StringVar(value="" if initial_doc_no == "!" else (initial_doc_no or ""))
		doc_var.trace_add("write", lambda *_: _upper_var(doc_var))
		doc_entry = ttk.Entry(left, textvariable=doc_var, width=36)
		doc_entry.grid(row=1, column=0, sticky="ew", pady=(0, 10))

		ttk.Label(left, text="File Type").grid(row=2, column=0, sticky="w")
		type_values = [
			FileType.TaxInvoice.value,
			FileType.Order.value,
			FileType.Proforma.value,
			FileType.Transfer.value,
			FileType.Credit.value,
			FileType.Unknown.value,
		]
		type_var = tk.StringVar(value=(initial_file_type.value if initial_file_type else FileType.Unknown.value))
		type_combo = ttk.Combobox(left, textvariable=type_var, values=type_values, state="readonly", width=34)
		type_combo.grid(row=3, column=0, sticky="ew", pady=(0, 10))

		btns = ttk.Frame(left)
		btns.grid(row=4, column=0, sticky="e")

		result: tuple[str, FileType] | None = None

		def on_ok() -> None:
			nonlocal result
			doc_no = (doc_var.get() or "").strip()
			if not doc_no:
				messagebox.showwarning("Manual Input", "Please enter a document number.")
				return
			ft_str = (type_var.get() or "").strip()
			ft = next((t for t in FileType if t.value == ft_str), FileType.Unknown)
			result = (doc_no, ft)
			win.destroy()

		def on_cancel() -> None:
			win.destroy()

		ttk.Button(btns, text="Cancel", command=on_cancel).grid(row=0, column=0, padx=(0, 8))
		ttk.Button(btns, text="OK", command=on_ok).grid(row=0, column=1)

		win.bind("<Escape>", lambda _e: on_cancel())
		doc_entry.bind("<Return>", lambda _e: on_ok())
		try:
			doc_entry.focus_set()
		except Exception:
			pass

		try:
			win.update_idletasks()
		except Exception:
			pass
		self._center_toplevel(win)

		try:
			win.grab_set()
		except Exception:
			pass
		self.master.wait_window(win)
		return result

