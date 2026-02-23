from __future__ import annotations

import re

from core.row_model import FileType
from services.ocr_runtime import (
	is_profile_complete,
	load_ocr_profile,
	ocr_pixmap,
	render_normalized_roi_to_pixmap,
)


_MONTHS: dict[str, int] = {
	"JAN": 1,
	"FEB": 2,
	"MAR": 3,
	"APR": 4,
	"MAY": 5,
	"JUN": 6,
	"JUL": 7,
	"AUG": 8,
	"SEP": 9,
	"OCT": 10,
	"NOV": 11,
	"DEC": 12,
}


def extract_phase2_fields(pdf_path: str, file_type: FileType) -> tuple[str, str, str]:
	profile = load_ocr_profile()
	if not is_profile_complete(profile):
		return ("!", "!", "!")

	section_key = ""
	if file_type == FileType.TaxInvoice:
		section_key = "tax_invoice"
	elif file_type == FileType.Proforma:
		section_key = "proforma"
	else:
		return ("!", "!", "!")

	section = profile.get(section_key)
	if not isinstance(section, dict):
		return ("!", "!", "!")

	date_str = "!"
	account_str = "!"
	total_str = "!"

	# Date
	try:
		roi = section.get("date")
		dpi = int((roi or {}).get("dpi") or 150)
		pix = render_normalized_roi_to_pixmap(pdf_path, 0, dpi=dpi, roi=roi or {})
		raw = ocr_pixmap(pix, psm=6, lang="eng")
		t = (raw or "").upper()
		m = re.search(r"\b(\d{1,2})\s*[-./\s]\s*([A-Z]{3})\s*[-./\s]\s*(\d{2,4})\b", t)
		if m:
			dd = int(m.group(1))
			mon = _MONTHS.get(m.group(2) or "")
			yy_raw = m.group(3) or ""
			yy = int(yy_raw[-2:]) if yy_raw else -1
			if 1 <= dd <= 31 and mon and 0 <= yy <= 99:
				date_str = f"{dd:02d}.{mon:02d}.{yy:02d}"
	except Exception:
		date_str = "!"

	# Account
	try:
		roi = section.get("account_no")
		dpi = int((roi or {}).get("dpi") or 150)
		pix = render_normalized_roi_to_pixmap(pdf_path, 0, dpi=dpi, roi=roi or {})
		raw = ocr_pixmap(pix, psm=6, lang="eng")
		t = (raw or "").upper()
		matches = re.findall(r"\b[A-Z][0-9]{4}\b", t)
		cands: set[str] = {m for m in matches if m}
		if len(cands) == 1:
			account_str = next(iter(cands))
	except Exception:
		account_str = "!"

	# Total
	try:
		roi = section.get("total")
		dpi = int((roi or {}).get("dpi") or 150)
		pix = render_normalized_roi_to_pixmap(pdf_path, 0, dpi=dpi, roi=roi or {})
		raw = ocr_pixmap(pix, psm=6, lang="eng")
		t = (raw or "")
		matches = re.findall(
			r"(?<!\d)(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+(?:\.\d{2})?)(?!\d)",
			t,
		)
		values: list[float] = []
		for s in matches:
			try:
				values.append(float(str(s).replace(",", "")))
			except Exception:
				pass
		if values:
			v = max(values)
			total_str = f"{v:.2f}"
	except Exception:
		total_str = "!"

	return (date_str, account_str, total_str)
