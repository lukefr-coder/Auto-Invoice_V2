from __future__ import annotations

import base64
import tkinter as tk
from tkinter import ttk


class PdfPage1Preview(ttk.Frame):
	"""Preview widget for rendering page 1 of a PDF.

	- Page 1 only (index 0)
	- Fit-to-window by default
	- Debounced re-render on resize
	- Mouse-wheel zoom + left-drag pan (Canvas-internal)
	"""

	def __init__(self, master: tk.Misc):
		super().__init__(master)

		self._path: str | None = None
		self._pdf_path: str = ""
		self._photo: tk.PhotoImage | None = None
		self._render_after_id: str | None = None
		self._last_render_size: tuple[int, int] = (0, 0)
		self._last_render_path: str = ""
		self._last_render_zoom_factor: float = 1.0

		self._fit_scale: float = 1.0
		self._zoom_factor: float = 1.0
		self._pan_x: float = 0.0
		self._pan_y: float = 0.0
		self._drag_active: bool = False
		self._drag_start_x: int = 0
		self._drag_start_y: int = 0
		self._drag_start_pan_x: float = 0.0
		self._drag_start_pan_y: float = 0.0
		self._img_w: int = 0
		self._img_h: int = 0

		self._canvas = tk.Canvas(self, highlightthickness=0, bd=0)
		self._canvas.grid(row=0, column=0, sticky="nsew")
		self._img_item_id = int(self._canvas.create_image(0, 0, anchor="nw"))
		self._unavailable_item_id = int(
			self._canvas.create_text(
				0,
				0,
				text="",
				anchor="center",
				fill="#666666",
			)
		)
		self.rowconfigure(0, weight=1)
		self.columnconfigure(0, weight=1)

		# Re-render on size changes, but debounce to avoid storms.
		self.bind("<Configure>", self._on_configure, add=True)

		# Zoom + pan bindings.
		self._canvas.bind("<Enter>", self._on_canvas_enter, add=True)
		self._canvas.bind("<MouseWheel>", self._on_mousewheel, add=True)
		self._canvas.bind("<Button-4>", self._on_button4, add=True)
		self._canvas.bind("<Button-5>", self._on_button5, add=True)
		self._canvas.bind("<ButtonPress-2>", self._on_button_press_1, add=True)
		self._canvas.bind("<B2-Motion>", self._on_b1_motion, add=True)
		self._canvas.bind("<ButtonRelease-2>", self._on_button_release_1, add=True)

	def clear(self) -> None:
		self._path = None
		self._pdf_path = ""
		self._photo = None
		self._last_render_size = (0, 0)
		self._last_render_path = ""
		self._last_render_zoom_factor = 1.0
		self._fit_scale = 1.0
		self._zoom_factor = 1.0
		self._pan_x = 0.0
		self._pan_y = 0.0
		self._drag_active = False
		self._img_w = 0
		self._img_h = 0
		try:
			self._canvas.itemconfigure(self._img_item_id, image="")
			self._canvas.itemconfigure(self._unavailable_item_id, text="")
			self._canvas.coords(self._img_item_id, 0, 0)
		except Exception:
			pass

	def set_pdf_path(self, path: str) -> None:
		new_path = path or ""
		if new_path != self._pdf_path:
			self._last_render_size = (0, 0)
			self._last_render_path = ""
			self._last_render_zoom_factor = 1.0
			self._zoom_factor = 1.0
			self._pan_x = 0.0
			self._pan_y = 0.0
		self._pdf_path = new_path
		self._path = new_path or None
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
			self._canvas.itemconfigure(self._img_item_id, image="")
			self._canvas.itemconfigure(self._unavailable_item_id, text="Preview unavailable")
			self._center_unavailable_text()
		except Exception:
			pass

	def _center_unavailable_text(self) -> None:
		try:
			w = int(self._canvas.winfo_width())
			h = int(self._canvas.winfo_height())
			if w > 1 and h > 1:
				self._canvas.coords(self._unavailable_item_id, w // 2, h // 2)
		except Exception:
			pass

	def _on_canvas_enter(self, _event: tk.Event) -> None:
		try:
			self._canvas.focus_set()
		except Exception:
			pass

	def _clamp_zoom_factor(self, z: float) -> float:
		try:
			z = float(z)
		except Exception:
			z = 1.0
		if z < 1.0:
			return 1.0
		if z > 12.0:
			return 12.0
		return z

	def _soft_clamp_pan(self) -> None:
		try:
			vw = int(self._canvas.winfo_width())
			vh = int(self._canvas.winfo_height())
		except Exception:
			return
		if vw <= 1 or vh <= 1:
			return

		img_w = int(self._img_w)
		img_h = int(self._img_h)
		if img_w <= 0 or img_h <= 0:
			return

		margin_x = 0.10 * float(vw)
		margin_y = 0.10 * float(vh)

		if img_w <= vw:
			center_x = (float(vw) - float(img_w)) / 2.0
			min_x = center_x - margin_x
			max_x = center_x + margin_x
		else:
			min_x = -((float(img_w) - float(vw)) + margin_x)
			max_x = margin_x

		if img_h <= vh:
			center_y = (float(vh) - float(img_h)) / 2.0
			min_y = center_y - margin_y
			max_y = center_y + margin_y
		else:
			min_y = -((float(img_h) - float(vh)) + margin_y)
			max_y = margin_y

		if self._pan_x < min_x:
			self._pan_x = min_x
		elif self._pan_x > max_x:
			self._pan_x = max_x

		if self._pan_y < min_y:
			self._pan_y = min_y
		elif self._pan_y > max_y:
			self._pan_y = max_y

	def _update_canvas_image_coords(self) -> None:
		try:
			self._canvas.coords(self._img_item_id, self._pan_x, self._pan_y)
		except Exception:
			pass

	def _zoom_at(self, steps: int, mx: int, my: int) -> None:
		if steps == 0:
			return
		old_z = float(self._zoom_factor)
		new_z = self._clamp_zoom_factor(old_z * (1.10 ** int(steps)))
		if abs(new_z - old_z) < 1e-9:
			return

		old_scale = float(self._fit_scale) * old_z
		if old_scale <= 1e-9:
			self._zoom_factor = new_z
			self._schedule_render()
			return

		ix = (float(mx) - float(self._pan_x)) / old_scale
		iy = (float(my) - float(self._pan_y)) / old_scale

		new_scale = float(self._fit_scale) * new_z
		self._pan_x = float(mx) - ix * new_scale
		self._pan_y = float(my) - iy * new_scale
		self._zoom_factor = new_z

		# Wheel zoom should feel responsive; render immediately.
		if self._render_after_id is not None:
			try:
				self.after_cancel(self._render_after_id)
			except Exception:
				pass
			self._render_after_id = None
		self._render_now()

	def _on_mousewheel(self, event: tk.Event) -> None:
		delta = 0
		try:
			delta = int(getattr(event, "delta", 0) or 0)
		except Exception:
			delta = 0
		steps = 0
		if delta > 0:
			steps = 1
		elif delta < 0:
			steps = -1
		self._zoom_at(steps, int(getattr(event, "x", 0)), int(getattr(event, "y", 0)))

	def _on_button4(self, event: tk.Event) -> None:
		self._zoom_at(1, int(getattr(event, "x", 0)), int(getattr(event, "y", 0)))

	def _on_button5(self, event: tk.Event) -> None:
		self._zoom_at(-1, int(getattr(event, "x", 0)), int(getattr(event, "y", 0)))

	def _on_button_press_1(self, event: tk.Event) -> None:
		try:
			self._canvas.focus_set()
		except Exception:
			pass
		if float(self._zoom_factor) <= 1.0:
			self._drag_active = False
			return
		self._drag_active = True
		self._drag_start_x = int(getattr(event, "x", 0))
		self._drag_start_y = int(getattr(event, "y", 0))
		self._drag_start_pan_x = float(self._pan_x)
		self._drag_start_pan_y = float(self._pan_y)

	def _on_b1_motion(self, event: tk.Event) -> None:
		if not self._drag_active:
			return
		x = int(getattr(event, "x", 0))
		y = int(getattr(event, "y", 0))
		dx = float(x - int(self._drag_start_x))
		dy = float(y - int(self._drag_start_y))
		self._pan_x = float(self._drag_start_pan_x) + dx
		self._pan_y = float(self._drag_start_pan_y) + dy
		self._soft_clamp_pan()
		self._update_canvas_image_coords()

	def _on_button_release_1(self, _event: tk.Event) -> None:
		self._drag_active = False

	def _render_now(self) -> None:
		self._render_after_id = None

		path = self._pdf_path
		if not path:
			self.clear()
			return

		avail_w = int(self._canvas.winfo_width())
		avail_h = int(self._canvas.winfo_height())
		if avail_w < 50 or avail_h < 50:
			return

		lw, lh = self._last_render_size
		if (
			self._last_render_path == path
			and lw
			and lh
			and abs(avail_w - lw) < 10
			and abs(avail_h - lh) < 10
			and abs(float(self._zoom_factor) - float(self._last_render_zoom_factor)) < 1e-9
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

				new_fit_scale = min(avail_w / max(page_w, 1.0), avail_h / max(page_h, 1.0))
				# Keep fit scale sane (zoom factor controls magnification relative to fit).
				new_fit_scale = max(min(new_fit_scale, 8.0), 0.05)

				# Preserve view across resize when zoomed in.
				old_fit_scale = float(self._fit_scale)
				old_zoom_factor = float(self._zoom_factor)
				old_scale = old_fit_scale * old_zoom_factor
				new_scale = float(new_fit_scale) * old_zoom_factor
				if old_zoom_factor <= 1.0:
					self._pan_x = 0.0
					self._pan_y = 0.0
				elif old_scale > 1e-9 and new_scale > 1e-9 and lw > 0 and lh > 0:
					old_cx = float(lw) / 2.0
					old_cy = float(lh) / 2.0
					ix = (old_cx - float(self._pan_x)) / old_scale
					iy = (old_cy - float(self._pan_y)) / old_scale
					new_cx = float(avail_w) / 2.0
					new_cy = float(avail_h) / 2.0
					self._pan_x = new_cx - ix * new_scale
					self._pan_y = new_cy - iy * new_scale

				self._fit_scale = float(new_fit_scale)
				render_scale = float(self._fit_scale) * float(self._zoom_factor)
				render_scale = max(render_scale, 0.05)
				mat = fitz.Matrix(render_scale, render_scale)

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
			self._img_w = int(getattr(pix, "width", 0) or 0)
			self._img_h = int(getattr(pix, "height", 0) or 0)
			self._canvas.itemconfigure(self._img_item_id, image=self._photo)
			self._canvas.itemconfigure(self._unavailable_item_id, text="")
			self._soft_clamp_pan()
			self._update_canvas_image_coords()
			self._last_render_size = (avail_w, avail_h)
			self._last_render_path = path
			self._last_render_zoom_factor = float(self._zoom_factor)
		except Exception:
			self._show_unavailable()
