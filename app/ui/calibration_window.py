from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from ui.pdf_preview import PdfPage1Preview


def _profile_path() -> str:
	appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
	return os.path.join(appdata, "Auto-Invoice_V2", "ocr_profile.json")


def _default_roi() -> dict[str, Any]:
	return {"x": None, "y": None, "w": None, "h": None, "dpi": 150}


def _default_profile() -> dict[str, Any]:
	# Schema must match exactly.
	return {
		"primary_file_type": {"file_type_roi": _default_roi()},
		"tax_invoice": {
			"doc_no": _default_roi(),
			"date": _default_roi(),
			"account_no": _default_roi(),
			"total": _default_roi(),
		},
		"proforma": {
			"doc_no": _default_roi(),
			"date": _default_roi(),
			"account_no": _default_roi(),
			"total": _default_roi(),
		},
		"order": {"doc_no": _default_roi()},
		"transfer": {"doc_no": _default_roi()},
		"credit": {"doc_no": _default_roi()},
	}


def _ensure_schema_defaults(loaded: Any) -> dict[str, Any]:
	"""Merge loaded JSON into defaults, preserving required keys."""
	base = _default_profile()
	if not isinstance(loaded, dict):
		return base

	def merge(dst: dict[str, Any], src: Any) -> dict[str, Any]:
		if not isinstance(src, dict):
			return dst
		for k, v in src.items():
			if k not in dst:
				# Ignore unknown keys; schema is strict.
				continue
			if isinstance(dst[k], dict):
				dst[k] = merge(dict(dst[k]), v)
			else:
				dst[k] = v
		return dst

	return merge(base, loaded)


@dataclass(frozen=True)
class _TabSpec:
	tab_key: str  # e.g. "tax_invoice.doc_no"
	title: str  # notebook tab label


class CalibrationWindow(tk.Toplevel):
	"""UI-only Calibration window.

	- Modal behavior (transient/grab/wait_window) is applied by the caller (AppWindow).
	- This class does not reference AppState, workers, watchers, or services.
	"""

	def __init__(self, master: tk.Misc):
		super().__init__(master)
		self.title("Calibration")
		self.resizable(True, True)
		self.geometry("1100x720")
		self.minsize(900, 560)

		self.protocol("WM_DELETE_WINDOW", self._on_cancel)
		self.bind("<Escape>", lambda _e: self._on_cancel(), add=True)

		# Data
		self._profile: dict[str, Any] = self._load_profile()
		self._current_pdf_path: str = ""
		self._active_section: str = "primary_file_type"
		self._active_tab_key: str = "primary_file_type.file_type_roi"

		# UI state (per tab key)
		self._dpi_vars: dict[str, tk.StringVar] = {}
		self._rect_text_vars: dict[str, tk.StringVar] = {}

		# Rectangle drawing state
		self._draw_active: bool = False
		self._draw_start_ix: float = 0.0
		self._draw_start_iy: float = 0.0
		self._overlay_rect_id: int | None = None

		self._build_layout()
		self._load_profile_into_ui()
		self._select_section(self._active_section)

	def _load_profile(self) -> dict[str, Any]:
		path = _profile_path()
		try:
			if os.path.isfile(path):
				with open(path, "r", encoding="utf-8") as f:
					return _ensure_schema_defaults(json.load(f))
		except Exception:
			# Fall back to defaults on any load/parse issue.
			return _default_profile()
		return _default_profile()

	def _save_profile(self) -> None:
		path = _profile_path()
		folder = os.path.dirname(path)
		try:
			os.makedirs(folder, exist_ok=True)
		except Exception as ex:
			raise RuntimeError(f"Failed to create folder: {folder}") from ex

		with open(path, "w", encoding="utf-8") as f:
			json.dump(self._profile, f, indent=2, ensure_ascii=False)

	def _build_layout(self) -> None:
		main = ttk.Frame(self, padding=12)
		main.grid(row=0, column=0, sticky="nsew")
		self.columnconfigure(0, weight=1)
		self.rowconfigure(0, weight=1)

		# Columns: sidebar | content
		main.columnconfigure(0, weight=0)
		main.columnconfigure(1, weight=1)
		main.rowconfigure(0, weight=1)

		# Sidebar
		sidebar = ttk.Frame(main)
		sidebar.grid(row=0, column=0, sticky="ns", padx=(0, 12))
		ttk.Label(sidebar, text="Calibration Targets", font=("Segoe UI", 10, "bold")).grid(
			row=0, column=0, sticky="w", pady=(0, 8)
		)
		ttk.Button(sidebar, text="Load Sample PDF", command=self._on_load_sample_pdf).grid(
			row=1,
			column=0,
			sticky="ew",
			pady=(0, 10),
		)

		self._sidebar_buttons: dict[str, ttk.Button] = {}
		sidebar_items = [
			("primary_file_type", "Primary File Type"),
			("tax_invoice", "Tax Invoice"),
			("proforma", "Proforma"),
			("order", "Order"),
			("transfer", "Transfer"),
			("credit", "Credit"),
		]
		for i, (key, label) in enumerate(sidebar_items, start=2):
			btn = ttk.Button(sidebar, text=label, width=20, command=lambda k=key: self._select_section(k))
			btn.grid(row=i, column=0, sticky="ew", pady=2)
			self._sidebar_buttons[key] = btn

		# Content: notebook (top), preview (center), buttons (bottom-right)
		content = ttk.Frame(main)
		content.grid(row=0, column=1, sticky="nsew")
		content.columnconfigure(0, weight=1)
		content.rowconfigure(0, weight=0)
		content.rowconfigure(1, weight=1)
		content.rowconfigure(2, weight=0)

		self._notebook = ttk.Notebook(content)
		self._notebook.grid(row=0, column=0, sticky="ew", pady=(0, 10))
		self._notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed, add=True)

		preview_container = ttk.Frame(content)
		preview_container.grid(row=1, column=0, sticky="nsew")
		preview_container.columnconfigure(0, weight=1)
		preview_container.rowconfigure(0, weight=1)

		self._preview = PdfPage1Preview(preview_container)
		self._preview.grid(row=0, column=0, sticky="nsew")
		# UI-only slice: no default PDF path.
		self._preview.set_pdf_path("")
		self._current_pdf_path = ""

		# Keep ROI overlay locked to the rendered image when the canvas layout changes.
		try:
			self._preview._canvas.bind("<Configure>", self._on_preview_canvas_configure, add=True)
		except Exception:
			pass

		# Zoom/pan in PdfPage1Preview does not always trigger <Configure>, so schedule redraw
		# after the relevant input events. Do not unbind any preview bindings.
		try:
			self._preview._canvas.bind("<MouseWheel>", lambda _e: self._schedule_roi_redraw(), add=True)
			self._preview._canvas.bind("<Button-4>", lambda _e: self._schedule_roi_redraw(), add=True)
			self._preview._canvas.bind("<Button-5>", lambda _e: self._schedule_roi_redraw(), add=True)
			self._preview._canvas.bind("<B2-Motion>", lambda _e: self._schedule_roi_redraw(), add=True)
			self._preview._canvas.bind("<ButtonRelease-2>", lambda _e: self._schedule_roi_redraw(), add=True)
		except Exception:
			pass

		# Overlay bindings (external; do not modify PdfPage1Preview)
		self._install_overlay_bindings()

		btns = ttk.Frame(content)
		btns.grid(row=2, column=0, sticky="e", pady=(10, 0))
		self._save_btn = ttk.Button(btns, text="Save", command=self._on_save)
		self._save_btn.grid(row=0, column=0, padx=(0, 8))
		self._cancel_btn = ttk.Button(btns, text="Cancel", command=self._on_cancel)
		self._cancel_btn.grid(row=0, column=1)

	def _install_overlay_bindings(self) -> None:
		canvas = getattr(self._preview, "_canvas", None)
		if canvas is None:
			return

		# Override left-drag in this window to draw rectangles.
		try:
			canvas.unbind("<ButtonPress-1>")
			canvas.unbind("<B1-Motion>")
			canvas.unbind("<ButtonRelease-1>")
		except Exception:
			pass

		canvas.bind("<ButtonPress-1>", self._on_draw_press, add=True)
		canvas.bind("<B1-Motion>", self._on_draw_motion, add=True)
		canvas.bind("<ButtonRelease-1>", self._on_draw_release, add=True)

	def _on_preview_canvas_configure(self, _event: tk.Event) -> None:
		self._schedule_roi_redraw()

	def _schedule_roi_redraw(self) -> None:
		canvas = self._canvas()
		if canvas is None:
			return
		try:
			canvas.after_idle(self._render_active_roi)
		except Exception:
			pass

	def _on_load_sample_pdf(self) -> None:
		path = filedialog.askopenfilename(filetypes=[("PDF files", "*.pdf")])
		if not path:
			return
		self._current_pdf_path = path
		try:
			self._preview.set_pdf_path(path)
		except Exception as ex:
			messagebox.showerror("Load Sample PDF", str(ex), parent=self)
			return

		# Render now, and once more after the debounced preview render.
		self._render_active_roi()
		try:
			self.after(200, self._render_active_roi)
		except Exception:
			pass

	def _section_tab_specs(self, section: str) -> list[_TabSpec]:
		if section == "primary_file_type":
			return [_TabSpec("primary_file_type.file_type_roi", "File Type ROI")]
		if section == "tax_invoice":
			return [
				_TabSpec("tax_invoice.doc_no", "Doc Number"),
				_TabSpec("tax_invoice.date", "Date"),
				_TabSpec("tax_invoice.account_no", "Account Number"),
				_TabSpec("tax_invoice.total", "Total"),
			]
		if section == "proforma":
			return [
				_TabSpec("proforma.doc_no", "Doc Number"),
				_TabSpec("proforma.date", "Date"),
				_TabSpec("proforma.account_no", "Account Number"),
				_TabSpec("proforma.total", "Total"),
			]
		if section == "order":
			return [_TabSpec("order.doc_no", "Doc Number")]
		if section == "transfer":
			return [_TabSpec("transfer.doc_no", "Doc Number")]
		if section == "credit":
			return [_TabSpec("credit.doc_no", "Doc Number")]
		return []

	def _select_section(self, section: str) -> None:
		self._active_section = section

		for k, b in self._sidebar_buttons.items():
			try:
				b.state(["pressed"] if k == section else ["!pressed"])
			except Exception:
				pass

		# Rebuild notebook tabs for chosen section
		for tab_id in list(self._notebook.tabs()):
			try:
				self._notebook.forget(tab_id)
			except Exception:
				pass

		specs = self._section_tab_specs(section)
		first_key = specs[0].tab_key if specs else ""
		for spec in specs:
			frame = self._build_tab_frame(self._notebook, spec.tab_key)
			self._notebook.add(frame, text=spec.title)

		if first_key:
			self._active_tab_key = first_key
			self._render_active_roi()

	def _build_tab_frame(self, master: tk.Misc, tab_key: str) -> ttk.Frame:
		root = ttk.Frame(master, padding=(8, 6))
		root.columnconfigure(0, weight=1)

		rect_var = self._rect_text_vars.get(tab_key)
		if rect_var is None:
			rect_var = tk.StringVar(value="Rectangle: (none)")
			self._rect_text_vars[tab_key] = rect_var

		ttk.Label(root, textvariable=rect_var).grid(row=0, column=0, sticky="w", pady=(0, 6))

		row = ttk.Frame(root)
		row.grid(row=1, column=0, sticky="w")
		ttk.Label(row, text="DPI:").grid(row=0, column=0, sticky="w", padx=(0, 8))

		dpi_var = self._dpi_vars.get(tab_key)
		if dpi_var is None:
			dpi_var = tk.StringVar(value="150")
			self._dpi_vars[tab_key] = dpi_var

		dpi = ttk.Combobox(row, textvariable=dpi_var, width=6, state="readonly", values=[150, 200, 250, 300, 350, 400, 450, 600])
		dpi.grid(row=0, column=1, sticky="w")
		dpi.bind("<<ComboboxSelected>>", lambda _e, k=tab_key: self._on_dpi_changed(k), add=True)

		help_text = "Draw a rectangle on the preview (click-drag). One rectangle per tab."
		ttk.Label(root, text=help_text, foreground="#666666").grid(row=2, column=0, sticky="w", pady=(8, 0))

		setattr(root, "_tab_key", tab_key)
		return root

	def _on_tab_changed(self, _event: tk.Event) -> None:
		try:
			current = self._notebook.nametowidget(self._notebook.select())
			tab_key = getattr(current, "_tab_key", "") or ""
		except Exception:
			tab_key = ""
		if tab_key:
			self._active_tab_key = tab_key
			self._render_active_roi()

	def _on_dpi_changed(self, tab_key: str) -> None:
		roi = self._roi_ref(tab_key)
		try:
			roi["dpi"] = int(self._dpi_vars[tab_key].get())
		except Exception:
			roi["dpi"] = 150

	def _roi_ref(self, tab_key: str) -> dict[str, Any]:
		section, field = tab_key.split(".", 1)
		container = self._profile.get(section)
		if not isinstance(container, dict):
			container = {}
			self._profile[section] = container
		roi = container.get(field)
		if not isinstance(roi, dict):
			roi = _default_roi()
			container[field] = roi

		for k in ("x", "y", "w", "h", "dpi"):
			if k not in roi:
				roi[k] = 150 if k == "dpi" else None
		return roi

	def _load_profile_into_ui(self) -> None:
		for section in ("primary_file_type", "tax_invoice", "proforma", "order", "transfer", "credit"):
			for spec in self._section_tab_specs(section):
				roi = self._roi_ref(spec.tab_key)
				dpi_var = self._dpi_vars.get(spec.tab_key)
				if dpi_var is None:
					dpi_var = tk.StringVar()
					self._dpi_vars[spec.tab_key] = dpi_var
				try:
					dpi_var.set(str(int(roi.get("dpi") or 150)))
				except Exception:
					dpi_var.set("150")
				self._update_rect_summary(spec.tab_key)

	def _update_rect_summary(self, tab_key: str) -> None:
		roi = self._roi_ref(tab_key)
		x, y, w, h = roi.get("x"), roi.get("y"), roi.get("w"), roi.get("h")
		var = self._rect_text_vars.get(tab_key)
		if var is None:
			var = tk.StringVar()
			self._rect_text_vars[tab_key] = var

		if x is None or y is None or w is None or h is None:
			var.set("Rectangle: (none)")
		else:
			try:
				var.set(f"Rectangle: x={float(x):.3f} y={float(y):.3f} w={float(w):.3f} h={float(h):.3f}")
			except Exception:
				var.set("Rectangle: (set)")

	def _image_bbox(self) -> tuple[float, float, float, float] | None:
		"""Return canvas bbox (x1,y1,x2,y2) of the rendered PDF image item."""
		canvas = self._canvas()
		if canvas is None:
			return None
		item_id = getattr(self._preview, "_img_item_id", None)
		if not isinstance(item_id, int):
			return None
		try:
			bbox = canvas.bbox(item_id)
		except Exception:
			bbox = None
		if not bbox or len(bbox) != 4:
			return None
		x1, y1, x2, y2 = bbox
		try:
			fx1, fy1, fx2, fy2 = float(x1), float(y1), float(x2), float(y2)
		except Exception:
			return None
		if fx2 - fx1 <= 1 or fy2 - fy1 <= 1:
			return None
		return fx1, fy1, fx2, fy2

	def _canvas(self) -> tk.Canvas | None:
		c = getattr(self._preview, "_canvas", None)
		return c if isinstance(c, tk.Canvas) else None

	def _render_active_roi(self) -> None:
		"""Clear overlay and redraw ROI for the active tab (if available)."""
		self._delete_overlay_rect()
		self._update_rect_summary(self._active_tab_key)

		# No PDF loaded -> no overlay.
		if not (self._current_pdf_path or "").strip():
			return

		bbox = self._image_bbox()
		if bbox is None:
			return
		x1, y1, x2, y2 = bbox
		bw = x2 - x1
		bh = y2 - y1

		roi = self._roi_ref(self._active_tab_key)
		xn, yn, wn, hn = roi.get("x"), roi.get("y"), roi.get("w"), roi.get("h")
		if xn is None or yn is None or wn is None or hn is None:
			return

		try:
			fxn, fyn, fwn, fhn = float(xn), float(yn), float(wn), float(hn)
		except Exception:
			return

		rx1 = x1 + fxn * bw
		ry1 = y1 + fyn * bh
		rx2 = x1 + (fxn + fwn) * bw
		ry2 = y1 + (fyn + fhn) * bh
		self._draw_overlay_rect(rx1, ry1, rx2, ry2)

	def _delete_overlay_rect(self) -> None:
		canvas = self._canvas()
		if canvas is None:
			return
		if self._overlay_rect_id is not None:
			try:
				canvas.delete(self._overlay_rect_id)
			except Exception:
				pass
			self._overlay_rect_id = None

	def _draw_overlay_rect(self, x1: float, y1: float, x2: float, y2: float) -> None:
		canvas = self._canvas()
		if canvas is None:
			return
		if self._overlay_rect_id is None:
			try:
				self._overlay_rect_id = int(
					canvas.create_rectangle(
						x1,
						y1,
						x2,
						y2,
						outline="#d40000",
						width=2,
					)
				)
			except Exception:
				self._overlay_rect_id = None
				return
		else:
			try:
				canvas.coords(self._overlay_rect_id, x1, y1, x2, y2)
			except Exception:
				pass

		try:
			canvas.tag_raise(self._overlay_rect_id)
		except Exception:
			pass

	def _event_to_image_xy(self, event: tk.Event) -> tuple[float, float] | None:
		if not (self._current_pdf_path or "").strip():
			return None

		bbox = self._image_bbox()
		if bbox is None:
			return None
		x1, y1, x2, y2 = bbox
		bw = x2 - x1
		bh = y2 - y1
		if bw <= 1 or bh <= 1:
			return None

		cx = float(getattr(event, "x", 0) or 0)
		cy = float(getattr(event, "y", 0) or 0)

		ix = cx - x1
		iy = cy - y1

		if ix < 0:
			ix = 0.0
		elif ix > bw:
			ix = bw

		if iy < 0:
			iy = 0.0
		elif iy > bh:
			iy = bh

		return ix, iy

	def _on_draw_press(self, event: tk.Event) -> None:
		pt = self._event_to_image_xy(event)
		if pt is None:
			return
		self._draw_active = True
		self._draw_start_ix, self._draw_start_iy = pt

		bbox = self._image_bbox()
		if bbox is None:
			return
		bx1, by1, _bx2, _by2 = bbox
		x1 = bx1 + self._draw_start_ix
		y1 = by1 + self._draw_start_iy
		self._draw_overlay_rect(x1, y1, x1, y1)

	def _on_draw_motion(self, event: tk.Event) -> None:
		if not self._draw_active:
			return
		pt = self._event_to_image_xy(event)
		if pt is None:
			return
		ix, iy = pt

		bbox = self._image_bbox()
		if bbox is None:
			return
		bx1, by1, _bx2, _by2 = bbox
		x1 = bx1 + self._draw_start_ix
		y1 = by1 + self._draw_start_iy
		x2 = bx1 + ix
		y2 = by1 + iy
		self._draw_overlay_rect(x1, y1, x2, y2)

	def _on_draw_release(self, event: tk.Event) -> None:
		if not self._draw_active:
			return
		self._draw_active = False

		end = self._event_to_image_xy(event)
		if end is None:
			return
		end_ix, end_iy = end

		bbox = self._image_bbox()
		if bbox is None:
			return
		bx1, by1, bx2, by2 = bbox
		bw = bx2 - bx1
		bh = by2 - by1
		if bw <= 1 or bh <= 1:
			return

		x1 = float(self._draw_start_ix)
		y1 = float(self._draw_start_iy)
		x2 = float(end_ix)
		y2 = float(end_iy)

		left = min(x1, x2)
		right = max(x1, x2)
		top = min(y1, y2)
		bottom = max(y1, y2)

		xn = left / bw
		yn = top / bh
		wn = (right - left) / bw
		hn = (bottom - top) / bh

		roi = self._roi_ref(self._active_tab_key)
		roi["x"] = max(0.0, min(1.0, xn))
		roi["y"] = max(0.0, min(1.0, yn))
		roi["w"] = max(0.0, min(1.0, wn))
		roi["h"] = max(0.0, min(1.0, hn))

		try:
			roi["dpi"] = int(self._dpi_vars[self._active_tab_key].get())
		except Exception:
			roi["dpi"] = int(roi.get("dpi") or 150)

		self._update_rect_summary(self._active_tab_key)
		self._render_active_roi()

	def _on_save(self) -> None:
		# Ensure schema is complete.
		self._profile = _ensure_schema_defaults(self._profile)

		# Push DPI vars into profile (covers tabs not visited this session)
		for section in ("primary_file_type", "tax_invoice", "proforma", "order", "transfer", "credit"):
			for spec in self._section_tab_specs(section):
				roi = self._roi_ref(spec.tab_key)
				v = self._dpi_vars.get(spec.tab_key)
				if v is not None:
					try:
						roi["dpi"] = int(v.get())
					except Exception:
						roi["dpi"] = int(roi.get("dpi") or 150)

		try:
			self._save_profile()
		except Exception as ex:
			messagebox.showerror("Save failed", str(ex), parent=self)
			return

		self.destroy()

	def _on_cancel(self) -> None:
		self.destroy()
