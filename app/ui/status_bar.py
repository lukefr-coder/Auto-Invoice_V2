from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class StatusBar(ttk.Frame):
	def __init__(self, master: tk.Misc):
		super().__init__(master)

		self._label = ttk.Label(self, text="", anchor="w")
		self._label.pack(fill="x")

		self._working_after_id: str | None = None
		self._auto_clear_after_id: str | None = None
		self._working_message: str = ""
		self._working_phase: int = 0

		self.clear()

	def clear(self) -> None:
		self._cancel_timers()
		self._label.configure(text="", foreground="black")

	def set_working(self, message: str) -> None:
		self._cancel_timers()
		self._label.configure(foreground="black")
		self._working_message = message
		self._working_phase = 0
		self._tick_working()

	def set_success(self, message: str) -> None:
		self._cancel_timers()
		self._label.configure(text=message, foreground="blue")
		self._auto_clear_after_id = self.after(2000, self.clear)

	def set_error(self, message: str) -> None:
		self._cancel_timers()
		self._label.configure(text=message, foreground="black")

	def set_info(self, message: str) -> None:
		self._cancel_timers()
		self._label.configure(text=message, foreground="black")

	def has_transient_message(self) -> bool:
		return (self._working_after_id is not None) or (self._auto_clear_after_id is not None)

	def _tick_working(self) -> None:
		dots = "." * (self._working_phase + 1)
		self._label.configure(text=f"{self._working_message}{dots}")
		self._working_phase = (self._working_phase + 1) % 3
		self._working_after_id = self.after(450, self._tick_working)

	def _cancel_timers(self) -> None:
		if self._working_after_id is not None:
			try:
				self.after_cancel(self._working_after_id)
			except Exception:
				pass
			self._working_after_id = None

		if self._auto_clear_after_id is not None:
			try:
				self.after_cancel(self._auto_clear_after_id)
			except Exception:
				pass
			self._auto_clear_after_id = None

