from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any

import fitz  # PyMuPDF
from PIL import Image, ImageOps


def load_ocr_profile() -> dict:
	appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
	path = os.path.join(appdata, "Auto-Invoice_V2", "ocr_profile.json")
	try:
		with open(path, "r", encoding="utf-8") as f:
			loaded = json.load(f)
		return loaded if isinstance(loaded, dict) else {}
	except Exception:
		return {}


def get_required_roi_tab_keys() -> list[str]:
	return [
		"primary_file_type.file_type_roi",
		"tax_invoice.doc_no",
		"tax_invoice.date",
		"tax_invoice.account_no",
		"tax_invoice.total",
		"proforma.doc_no",
		"proforma.date",
		"proforma.account_no",
		"proforma.total",
		"order.doc_no",
		"transfer.doc_no",
		"credit.doc_no",
	]


def is_profile_complete(profile: dict) -> bool:
	if not isinstance(profile, dict):
		return False
	for tab_key in get_required_roi_tab_keys():
		try:
			section, roi_key = tab_key.split(".", 1)
		except Exception:
			return False
		section_obj = profile.get(section)
		if not isinstance(section_obj, dict):
			return False
		roi = section_obj.get(roi_key)
		if not isinstance(roi, dict):
			return False

		x = roi.get("x")
		y = roi.get("y")
		w = roi.get("w")
		h = roi.get("h")
		if x is None or y is None or w is None or h is None:
			return False
		try:
			w_f = float(w)
			h_f = float(h)
		except Exception:
			return False
		if w_f <= 0.0 or h_f <= 0.0:
			return False
	return True


def render_normalized_roi_to_pixmap(pdf_path: str, page_index: int, *, dpi: int, roi: dict) -> fitz.Pixmap:
	doc = fitz.open(pdf_path)
	try:
		page = doc.load_page(int(page_index))
		page_rect = page.rect

		try:
			x = float((roi or {}).get("x"))
			y = float((roi or {}).get("y"))
			w = float((roi or {}).get("w"))
			h = float((roi or {}).get("h"))
		except Exception:
			x, y, w, h = 0.0, 0.0, 1.0, 1.0

		x0 = page_rect.x0 + x * page_rect.width
		y0 = page_rect.y0 + y * page_rect.height
		x1 = x0 + w * page_rect.width
		y1 = y0 + h * page_rect.height

		if x0 < page_rect.x0:
			x0 = page_rect.x0
		if y0 < page_rect.y0:
			y0 = page_rect.y0
		if x1 > page_rect.x1:
			x1 = page_rect.x1
		if y1 > page_rect.y1:
			y1 = page_rect.y1
		if x1 <= x0 or y1 <= y0:
			x0, y0, x1, y1 = page_rect.x0, page_rect.y0, page_rect.x1, page_rect.y1

		clip = fitz.Rect(x0, y0, x1, y1)

		scale = float(dpi) / 72.0
		mat = fitz.Matrix(scale, scale)
		pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
		return pix
	finally:
		try:
			doc.close()
		except Exception:
			pass


def ocr_pixmap(
	pix: fitz.Pixmap,
	*,
	psm: int = 6,
	lang: str = "eng",
	whitelist: str | None = None,
	timeout_s: float = 10.0,
	preprocess: bool = True,
) -> str:
	tmp_path = ""
	try:
		with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
			tmp_path = f.name
		try:
			if preprocess:
				try:
					width = pix.width
					height = pix.height
					samples = pix.samples
					mode = "RGBA" if pix.alpha else "RGB"
					img = Image.frombytes(mode, (width, height), samples)
					if mode == "RGBA":
						img = img.convert("RGB")
					img = img.convert("L")
					img = ImageOps.autocontrast(img, cutoff=2)
					img.save(tmp_path, format="PNG", optimize=False)
				except Exception:
					pix.save(tmp_path)
			else:
				pix.save(tmp_path)
		except Exception:
			return ""

		repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
		exe_path = os.path.join(repo_root, "tesseract", "tesseract.exe")
		tessdata_dir = os.path.join(repo_root, "tesseract", "tessdata")
		if not os.path.exists(exe_path):
			return ""

		args = [
			exe_path,
			tmp_path,
			"stdout",
			"--tessdata-dir",
			tessdata_dir,
			"-l",
			str(lang or "eng"),
			"--psm",
			str(int(psm)),
		]
		if whitelist:
			args.extend(["-c", f"tessedit_char_whitelist={whitelist}"])

		env = dict(os.environ)
		env["TESSDATA_PREFIX"] = tessdata_dir
		creationflags = 0
		try:
			creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
		except Exception:
			creationflags = 0

		try:
			cp = subprocess.run(
				args,
				capture_output=True,
				text=True,
				timeout=float(timeout_s),
				env=env,
				creationflags=creationflags,
			)
		except Exception:
			return ""
		out = cp.stdout if isinstance(cp.stdout, str) else ""
		return out
	finally:
		if tmp_path:
			try:
				os.unlink(tmp_path)
			except Exception:
				pass


def ocr_pixmap_tsv(
        pix: fitz.Pixmap,
        *,
        psm: int = 6,
        lang: str = "eng",
        timeout_s: float = 10.0,
        preprocess: bool = True,
) -> str:
        tmp_path = ""
        try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
                        tmp_path = f.name
                try:
                        if preprocess:
                                try:
                                        width = pix.width
                                        height = pix.height
                                        samples = pix.samples
                                        mode = "RGBA" if pix.alpha else "RGB"
                                        img = Image.frombytes(mode, (width, height), samples)
                                        if mode == "RGBA":
                                                img = img.convert("RGB")
                                        img = img.convert("L")
                                        img = ImageOps.autocontrast(img, cutoff=2)
                                        img.save(tmp_path, format="PNG", optimize=False)
                                except Exception:
                                        pix.save(tmp_path)
                        else:
                                pix.save(tmp_path)
                except Exception:
                        return ""

                repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
                exe_path = os.path.join(repo_root, "tesseract", "tesseract.exe")
                tessdata_dir = os.path.join(repo_root, "tesseract", "tessdata")
                if not os.path.exists(exe_path):
                        return ""

                args = [
                        exe_path,
                        tmp_path,
                        "stdout",
                        "--tessdata-dir",
                        tessdata_dir,
                        "-l",
                        str(lang or "eng"),
                        "--psm",
                        str(int(psm)),
                        "tsv",
                ]

                env = dict(os.environ)
                env["TESSDATA_PREFIX"] = tessdata_dir
                creationflags = 0
                try:
                        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
                except Exception:
                        creationflags = 0

                try:
                        cp = subprocess.run(
                                args,
                                capture_output=True,
                                text=True,
                                timeout=float(timeout_s),
                                env=env,
                                creationflags=creationflags,
                        )
                except Exception:
                        return ""
                out = cp.stdout if isinstance(cp.stdout, str) else ""
                return out
        finally:
                if tmp_path:
                        try:
                                os.unlink(tmp_path)
                        except Exception:
                                pass


def pixmap_to_pil_gray(pix: fitz.Pixmap) -> Image.Image:
	"""Convert a fitz.Pixmap to a grayscale PIL Image with autocontrast."""
	mode = "RGBA" if pix.alpha else "RGB"
	img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
	if mode == "RGBA":
		img = img.convert("RGB")
	img = img.convert("L")
	img = ImageOps.autocontrast(img, cutoff=2)
	return img


def tighten_text_crop(
	img: Image.Image,
	*,
	pad_px: int = 4,
	border_strip_px: int = 3,
	ink_threshold: int = 160,
) -> Image.Image | None:
	"""Crop a grayscale image to its text-content bbox, removing edge borders.

	Erases thin horizontal/vertical lines within *border_strip_px* of each
	edge, then returns the content bounding-box crop with *pad_px* padding.
	Returns ``None`` if no text content is detected.
	"""
	if img.mode != "L":
		img = img.convert("L")
	w, h = img.size
	if w < 5 or h < 5:
		return None

	cleaned = img.copy()
	px = cleaned.load()

	# Erase horizontal border lines near top and bottom
	for row in range(min(border_strip_px, h)):
		if sum(1 for col in range(w) if px[col, row] < ink_threshold) > w * 0.4:
			for col in range(w):
				px[col, row] = 255
	for row in range(max(0, h - border_strip_px), h):
		if sum(1 for col in range(w) if px[col, row] < ink_threshold) > w * 0.4:
			for col in range(w):
				px[col, row] = 255

	# Erase vertical border lines near left and right
	for col in range(min(border_strip_px, w)):
		if sum(1 for row_i in range(h) if px[col, row_i] < ink_threshold) > h * 0.4:
			for row_i in range(h):
				px[col, row_i] = 255
	for col in range(max(0, w - border_strip_px), w):
		if sum(1 for row_i in range(h) if px[col, row_i] < ink_threshold) > h * 0.4:
			for row_i in range(h):
				px[col, row_i] = 255

	# Build a dark-pixel mask and find its bounding box
	mask = cleaned.point(lambda p: 255 if p < ink_threshold else 0)
	bbox = mask.getbbox()
	if bbox is None:
		return None

	x0, y0, x1, y1 = bbox
	return cleaned.crop((
		max(0, x0 - pad_px),
		max(0, y0 - pad_px),
		min(w, x1 + pad_px),
		min(h, y1 + pad_px),
	))


def ocr_pil_image(
	img: Image.Image,
	*,
	psm: int = 7,
	lang: str = "eng",
	whitelist: str | None = None,
	timeout_s: float = 10.0,
) -> str:
	"""OCR a PIL Image directly via Tesseract.  Returns raw stdout text."""
	tmp_path = ""
	try:
		with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
			tmp_path = f.name
		try:
			img.save(tmp_path, format="PNG", optimize=False)
		except Exception:
			return ""

		repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
		exe_path = os.path.join(repo_root, "tesseract", "tesseract.exe")
		tessdata_dir = os.path.join(repo_root, "tesseract", "tessdata")
		if not os.path.exists(exe_path):
			return ""

		args = [
			exe_path,
			tmp_path,
			"stdout",
			"--tessdata-dir",
			tessdata_dir,
			"-l",
			str(lang or "eng"),
			"--psm",
			str(int(psm)),
		]
		if whitelist:
			args.extend(["-c", f"tessedit_char_whitelist={whitelist}"])

		env = dict(os.environ)
		env["TESSDATA_PREFIX"] = tessdata_dir
		creationflags = 0
		try:
			creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
		except Exception:
			creationflags = 0

		try:
			cp = subprocess.run(
				args,
				capture_output=True,
				text=True,
				timeout=float(timeout_s),
				env=env,
				creationflags=creationflags,
			)
		except Exception:
			return ""
		return cp.stdout if isinstance(cp.stdout, str) else ""
	finally:
		if tmp_path:
			try:
				os.unlink(tmp_path)
			except Exception:
				pass
