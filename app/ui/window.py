from __future__ import annotations

import tkinter as tk
from tkinter import ttk, filedialog

from state.persistence import load_settings, save_settings
from ui.grid import FilesGrid
from ui.status_bar import StatusBar


class AppWindow(ttk.Frame):
	def __init__(self, master: tk.Tk):
		super().__init__(master, padding=10)

		self.master = master
		self._settings = load_settings()

		self.source_var = tk.StringVar(value=self._settings.get("source_folder", ""))
		self.dest_var = tk.StringVar(value=self._settings.get("dest_folder", ""))
		self.export_var = tk.StringVar(value=self._settings.get("export_folder", ""))

		self._build_layout()

	def _build_layout(self) -> None:
		self.grid(row=0, column=0, sticky="nsew")
		self.master.rowconfigure(0, weight=1)
		self.master.columnconfigure(0, weight=1)

		self.columnconfigure(0, weight=1)
		self.rowconfigure(2, weight=1)

		# 1) Source section
		source_frame = ttk.Frame(self)
		source_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
		source_frame.columnconfigure(1, weight=1)

		ttk.Label(source_frame, text="Source").grid(row=0, column=0, sticky="w", padx=(0, 8))
		source_entry = ttk.Entry(source_frame, textvariable=self.source_var, state="readonly")
		source_entry.grid(row=0, column=1, sticky="ew")
		ttk.Button(source_frame, text="...", width=3, command=self._browse_source).grid(
			row=0, column=2, padx=(8, 0)
		)

		# 2) Export section
		export_frame = ttk.Frame(self)
		export_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
		export_frame.columnconfigure(1, weight=1)

		export_btn = ttk.Button(export_frame, text="Export (.xls)", state="disabled")
		export_btn.grid(row=0, column=0, sticky="w", padx=(0, 10))

		export_entry = ttk.Entry(export_frame, textvariable=self.export_var, state="readonly")
		export_entry.grid(row=0, column=1, sticky="ew")

		ttk.Button(export_frame, text="...", width=3, command=self._browse_export).grid(
			row=0, column=2, padx=(8, 0)
		)

		calib_btn = ttk.Button(export_frame, text="⚙", width=3, state="disabled")
		calib_btn.grid(row=0, column=3, padx=(8, 0))

		# 3) Grid
		grid_frame = ttk.Frame(self)
		grid_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
		grid_frame.rowconfigure(0, weight=1)
		grid_frame.columnconfigure(0, weight=1)

		self.files_grid = FilesGrid(grid_frame)
		self.files_grid.grid(row=0, column=0, sticky="nsew")

		# 4) Destination section
		dest_frame = ttk.Frame(self)
		dest_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))
		dest_frame.columnconfigure(1, weight=1)

		ttk.Label(dest_frame, text="Destination").grid(row=0, column=0, sticky="w", padx=(0, 8))
		dest_entry = ttk.Entry(dest_frame, textvariable=self.dest_var, state="readonly")
		dest_entry.grid(row=0, column=1, sticky="ew")
		ttk.Button(dest_frame, text="...", width=3, command=self._browse_dest).grid(
			row=0, column=2, padx=(8, 0)
		)
		deposit_btn = ttk.Button(dest_frame, text="Deposit", state="disabled")
		deposit_btn.grid(row=0, column=3, padx=(10, 0))

		# 5) Status bar
		self.status_bar = StatusBar(self)
		self.status_bar.grid(row=4, column=0, sticky="ew")

	def _browse_source(self) -> None:
		self._browse_into_var(self.source_var, "source_folder")

	def _browse_dest(self) -> None:
		self._browse_into_var(self.dest_var, "dest_folder")

	def _browse_export(self) -> None:
		self._browse_into_var(self.export_var, "export_folder")

	def _browse_into_var(self, var: tk.StringVar, key: str) -> None:
		initial = var.get() or None
		selected = filedialog.askdirectory(initialdir=initial, mustexist=False)
		if not selected:
			return

		var.set(selected)
		self._settings[key] = selected
		save_settings(self._settings)
		self.status_bar.set_success("Saved")

