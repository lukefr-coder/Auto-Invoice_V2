from __future__ import annotations

import base64
import tkinter as tk
from tkinter import ttk


class PdfPage1Preview(ttk.Frame):
	"""Preview widget for rendering page 1 of a PDF.

	- Page 1 only (index 0)
	- Fit-to-window by default
	- Debounced re-render on resize
	- No panning/scrolling in this slice
	"""

	def __init__(self, master: tk.Misc):
		super().__init__(master)

		self._pdf_path: str = ""
		self._photo: tk.PhotoImage | None = None
		self._render_after_id: str | None = None
		self._last_render_size: tuple[int, int] = (0, 0)
		self._last_render_path: str = ""

		self._host = ttk.Label(self, text="", anchor="center")
		self._host.grid(row=0, column=0, sticky="nsew")
		self.rowconfigure(0, weight=1)
		self.columnconfigure(0, weight=1)

		# Re-render on size changes, but debounce to avoid storms.
		self.bind("<Configure>", self._on_configure, add=True)

	def clear(self) -> None:
		self._pdf_path = ""
		self._photo = None
		self._last_render_size = (0, 0)
		self._last_render_path = ""
		try:
			self._host.configure(image="", text="")
		except Exception:
			pass

	def set_pdf_path(self, path: str) -> None:
		new_path = path or ""
		if new_path != self._pdf_path:
			self._last_render_size = (0, 0)
			self._last_render_path = ""
		self._pdf_path = new_path
		self._schedule_render()

	def _on_configure(self, _event: tk.Event) -> None:
		# If no PDF is set, do nothing.
		if not self._pdf_path:
			return
		w = int(self.winfo_width())
		h = int(self.winfo_height())
		if w < 50 or h < 50:
			return
		lw, lh = self._last_render_size
		if lw and lh and abs(w - lw) < 10 and abs(h - lh) < 10:
			return
		self._schedule_render()

	def _schedule_render(self) -> None:
		if self._render_after_id is not None:
			try:
				self.after_cancel(self._render_after_id)
			except Exception:
				pass
			self._render_after_id = None

		self._render_after_id = self.after(100, self._render_now)

	def _show_unavailable(self) -> None:
		self._photo = None
		try:
			self._host.configure(image="", text="Preview unavailable")
		except Exception:
			pass

	def _render_now(self) -> None:
		self._render_after_id = None

		path = self._pdf_path
		if not path:
			self.clear()
			return

		avail_w = int(self.winfo_width())
		avail_h = int(self.winfo_height())
		if avail_w < 50 or avail_h < 50:
			return

		lw, lh = self._last_render_size
		if (
			self._last_render_path == path
			and lw
			and lh
			and abs(avail_w - lw) < 10
			and abs(avail_h - lh) < 10
		):
			return

		try:
			import fitz  # PyMuPDF
		except Exception:
			self._show_unavailable()
			return

		try:
			doc = fitz.open(path)
			try:
				page = doc.load_page(0)
				rect = page.rect
				page_w = float(rect.width) if rect else 1.0
				page_h = float(rect.height) if rect else 1.0

				scale = min(avail_w / max(page_w, 1.0), avail_h / max(page_h, 1.0))
				# Keep scale sane.
				scale = max(min(scale, 8.0), 0.05)
				mat = fitz.Matrix(scale, scale)

				pix = page.get_pixmap(matrix=mat, alpha=False)
				ppm_bytes = pix.tobytes("ppm")
			finally:
				try:
					doc.close()
				except Exception:
					pass

			# Tk PhotoImage: attempt raw PPM data first (latin1), fall back to base64 if needed.
			photo: tk.PhotoImage | None = None
			try:
				photo = tk.PhotoImage(data=ppm_bytes.decode("latin1"))
			except Exception:
				try:
					photo = tk.PhotoImage(data=base64.b64encode(ppm_bytes))
				except Exception:
					photo = None

			if photo is None:
				self._show_unavailable()
				return

			self._photo = photo  # keep reference
			self._host.configure(image=self._photo, text="")
			self._last_render_size = (avail_w, avail_h)
			self._last_render_path = path
		except Exception:
			self._show_unavailable()
