from __future__ import annotations

import tkinter as tk
from tkinter import ttk, filedialog
import queue
import os
import time

from core.demo_seed import make_initial_state
from core.mutations import set_dest_path, set_source_path
from core.app_state import (
	mark_item_done,
	mark_item_running,
	on_fs_event,
	reset_watch_state,
	start_next_batch_if_idle,
)
from services.watcher import FakeWorkProcessor, FolderWatcher
from state import persistence
from state.persistence import load_settings, save_settings
from ui.grid import FilesGrid
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


class AppWindow(ttk.Frame):
	def __init__(self, master: tk.Tk):
		super().__init__(master, padding=10)

		self.master = master
		self.state = make_initial_state()
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

		# Slice 03: background watching + fake work processing (no Tk calls off-thread).
		self._fs_event_queue: queue.Queue[str] = queue.Queue()
		self._worker_event_queue: queue.Queue[tuple[str, int, str]] = queue.Queue()
		self._watcher: FolderWatcher | None = None
		self._worker = FakeWorkProcessor(self._worker_event_queue)
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
		ttk.Button(output_frame, text="Deposit Files", state="disabled").grid(row=0, column=2, padx=(8, 0))

		self.status_bar = StatusBar(self)
		self.status_bar.grid(row=4, column=0, sticky="ew")

	def _sync_viewing_text(self) -> None:
		self.viewing_text_var.set(f"Viewing: {self.source_var.get()}")

	def _sync_file_count(self) -> None:
		count = self.files_grid.get_visible_count()
		self.file_count_var.set(f"Files: {count}")

	def _browse_source(self) -> None:
		selected = self._browse_directory(self.source_var.get() or None)
		if not selected:
			return
		set_source_path(self.state, selected)
		self.source_var.set(self.state.source_path)
		self._persist_settings()
		self._restart_watcher_if_possible()
		self.status_bar.set_success("Saved")

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

