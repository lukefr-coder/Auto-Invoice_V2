from __future__ import annotations

import re

from core.row_model import FileType
from services.ocr_runtime import (
	is_profile_complete,
	load_ocr_profile,
	ocr_pixmap,
	ocr_pixmap_tsv,
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
		raw_tsv = ocr_pixmap_tsv(pix, psm=6, lang="eng")
		lines = (raw_tsv or "").splitlines()
		tokens: list[dict] = []
		for line in lines:
			if not line or not line.strip():
				continue
			cols = line.split("\t")
			if len(cols) < 12:
				continue
			if cols[0].strip().lower() == "level":
				continue
			text = cols[11]
			if not text or not text.strip():
				continue
			try:
				token = {
					"block_num": int(cols[2]),
					"par_num": int(cols[3]),
					"line_num": int(cols[4]),
					"word_num": int(cols[5]),
					"left": int(cols[6]),
					"top": int(cols[7]),
					"width": int(cols[8]),
					"height": int(cols[9]),
					"conf": float(cols[10]),
					"text": text,
				}
			except Exception:
				continue
			tokens.append(token)

		anchors: list[dict] = []
		for tok in tokens:
			text_u = str(tok.get("text") or "").strip().upper()
			if "TOTALEX" in text_u:
				continue
			if text_u != "TOTAL":
				continue
			line_key = (tok.get("block_num"), tok.get("par_num"), tok.get("line_num"))
			wn = tok.get("word_num")
			next_tok = None
			for other in tokens:
				if (other.get("block_num"), other.get("par_num"), other.get("line_num")) != line_key:
					continue
				try:
					if int(other.get("word_num")) <= int(wn):
						continue
				except Exception:
					continue
				if next_tok is None or int(other.get("word_num")) < int(next_tok.get("word_num")):
					next_tok = other
			if next_tok is not None:
				next_text_u = str(next_tok.get("text") or "").strip().upper()
				if next_text_u == "EX":
					continue
			anchors.append(tok)

		chosen_token = None
		if len(anchors) == 1:
			anchor = anchors[0]
			anchor_line_key = (anchor.get("block_num"), anchor.get("par_num"), anchor.get("line_num"))
			total_right = int(anchor.get("left")) + int(anchor.get("width"))
			height = int(anchor.get("height"))
			small_gap_px = max(5, int(height * 0.2))
			candidates: list[dict] = []
			for tok in tokens:
				if (tok.get("block_num"), tok.get("par_num"), tok.get("line_num")) != anchor_line_key:
					continue
				cand_left = int(tok.get("left"))
				if cand_left < total_right + small_gap_px:
					continue
				cand_text = str(tok.get("text") or "")
				if not re.match(r"^[\$\s]*\d[\d,]*(?:\.\d{2})?\s*$", cand_text):
					continue
				try:
					conf = float(tok.get("conf"))
				except Exception:
					continue
				if conf < 50:
					continue
				candidates.append(tok)

			if candidates:
				sorted_cands = sorted(
					candidates,
					key=lambda d: (
						float(d.get("conf")),
						int(d.get("left")),
					),
					reverse=True,
				)
				if len(sorted_cands) >= 2:
					c1 = sorted_cands[0]
					c2 = sorted_cands[1]
					if float(c1.get("conf")) == float(c2.get("conf")) and abs(int(c1.get("left")) - int(c2.get("left"))) <= 1:
						sorted_cands = []
				if sorted_cands:
					chosen_token = sorted_cands[0]
			else:
				anchor_block_par = (anchor.get("block_num"), anchor.get("par_num"))
				anchor_line_num = int(anchor.get("line_num"))
				next_line_num = anchor_line_num + 1
				next_line_nums: list[dict] = []
				for tok in tokens:
					if (tok.get("block_num"), tok.get("par_num")) != anchor_block_par:
						continue
					if int(tok.get("line_num")) != next_line_num:
						continue
					cand_text = str(tok.get("text") or "")
					if not re.match(r"^[\$\s]*\d[\d,]*(?:\.\d{2})?\s*$", cand_text):
						continue
					try:
						conf = float(tok.get("conf"))
					except Exception:
						continue
					if conf < 50:
						continue
					next_line_nums.append(tok)
				if len(next_line_nums) == 1:
					chosen_token = next_line_nums[0]

		if chosen_token is not None:
			raw = str(chosen_token.get("text") or "").strip()
			raw = raw.replace("$", "").replace(" ", "")
			normalized = None
			if re.match(r"^\d+,\d{2}$", raw):
				normalized = raw.replace(",", ".")
			elif re.match(r"^\d{1,3}(?:\.\d{3})+,\d{2}$", raw):
				normalized = raw.replace(".", "").replace(",", ".")
			elif re.match(r"^\d{1,3}(?:,\d{3})+(?:\.\d{2})$", raw):
				normalized = raw.replace(",", "")
			elif re.match(r"^\d+(\.\d{2})$", raw):
				normalized = raw
			if normalized is not None and re.match(r"^\d+(\.\d{2})$", normalized):
				v = float(normalized)
				total_str = f"{v:.2f}"
	except Exception:
		total_str = "!"

	return (date_str, account_str, total_str)
