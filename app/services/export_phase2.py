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


def _phase2_debug_log(tag: str, payload: dict):
	try:
		import json, datetime, os

		log_path = os.path.join(os.getcwd(), "_phase2_debug_log.txt")
		with open(log_path, "a", encoding="utf-8") as f:
			f.write("\n" + "=" * 100 + "\n")
			f.write(f"{datetime.datetime.now().isoformat()} | {tag}\n")
			f.write(json.dumps(payload, indent=2, ensure_ascii=False))
			f.write("\n")
	except Exception:
		pass


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

		# --- Date OCR normalization (strict and minimal) ---
		# Collapse split month tokens like "F EB" -> "FEB"
		t = re.sub(r"\b([A-Z])\s+([A-Z]{2})\b", r"\1\2", t)

		# Convert letter 'O' to digit '0' ONLY when part of a numeric token
		t = re.sub(r"(?<=\d)O(?=\d)", "0", t)
		t = re.sub(r"(?<=\b)O(?=\d)", "0", t)
		t = re.sub(r"\b0(\d{2})(?=[-./])", r"\1", t)
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
		t = re.sub(r"\b([A-Z])\s+(\d{4})\b", r"\1\2", t)
		t = re.sub(r"(?<=\d)O(?=\d)", "0", t)
		t = re.sub(r"(?<=\b[A-Z]\d)O(?=\d)", "0", t)
		t = re.sub(r"(?<=\d)[IL](?=\d)", "1", t)
		t = re.sub(r"(?<=\d)[IL](?=\b)", "1", t)
		t = re.sub(r"(?<=\d)O(?=\b)", "0", t)
		t = re.sub(
			r"\b[A-Z][0-9OIL]{4}\b",
			lambda m: m.group(0).replace("O", "0").replace("I", "1").replace("L", "1"),
			t,
		)
		if "ACCOUNT" in t:
			t = re.sub(r"\b1(\d{4})\b", r"I\1", t)
			t = re.sub(r"(?<![A-Z0-9])\$(?=\d{4}\b)", "S", t)
		matches = re.findall(r"\b[A-Z][0-9]{4}\b", t)
		cands: set[str] = {m for m in matches if m}
		if len(cands) == 1:
			account_str = next(iter(cands))
		if account_str == "!":
			reason = "UNKNOWN_ACCOUNT_FAIL"
			if not raw or raw.strip() == "":
				reason = "EMPTY_OCR"
			elif len(matches) == 0:
				reason = "NO_REGEX_MATCH"
			elif len(cands) > 1:
				reason = "MULTIPLE_REGEX_MATCH"
			_phase2_debug_log(
				"PHASE2_ACCOUNT_FAIL",
				{
					"pdf_path": pdf_path,
					"file_type": file_type,
					"roi": roi,
					"dpi": (roi or {}).get("dpi"),
					"raw_ocr": raw,
					"normalized_text": t,
					"regex_matches": matches,
					"unique_candidates": list(cands),
					"candidate_count": len(cands),
					"reason": reason,
				},
			)
		if account_str != "!":
			account_str_val = account_str
			raw_val = raw
			t_val = t
			account_gate_ok = False
			if isinstance(account_str_val, str) and len(account_str_val) == 5:
				if account_str_val[:1].isalpha() and account_str_val[:1].upper() == account_str_val[:1]:
					if account_str_val[1:].isdigit():
						account_gate_ok = True
			if (
				account_gate_ok
				and isinstance(account_str_val, str)
				and account_str_val[:1] in {"I", "L"}
				and (
					"ACCOUNT" in (t_val or "")
					or "ACCOUNT" in ((raw_val or "").upper())
				)
			):
				_phase2_debug_log(
					"PHASE2_ACCOUNT_SUSPECT_PASS",
					{
						"pdf_path": pdf_path,
						"file_type": file_type,
						"roi": roi,
						"dpi": (roi or {}).get("dpi"),
						"raw_ocr": raw_val,
						"normalized_text": t_val,
						"account_str": account_str_val,
						"matches": matches,
						"unique_candidates": list(cands),
						"reason": "SUSPECT_LEADING_LETTER",
					},
				)
	except Exception:
		account_str = "!"
		raw_val = locals().get("raw", None)
		roi_val = locals().get("roi", section.get("account_no"))
		t_val = locals().get("t", "")
		matches_val = locals().get("matches", [])
		cands_val = locals().get("cands", set())
		reason = "UNKNOWN_ACCOUNT_FAIL"
		if not raw_val or str(raw_val).strip() == "":
			reason = "EMPTY_OCR"
		elif len(matches_val) == 0:
			reason = "NO_REGEX_MATCH"
		elif len(cands_val) > 1:
			reason = "MULTIPLE_REGEX_MATCH"
		_phase2_debug_log(
			"PHASE2_ACCOUNT_FAIL",
			{
				"pdf_path": pdf_path,
				"file_type": file_type,
				"roi": roi_val,
				"dpi": (roi_val or {}).get("dpi") if isinstance(roi_val, dict) or roi_val is None else None,
				"raw_ocr": raw_val,
				"normalized_text": t_val,
				"regex_matches": matches_val,
				"unique_candidates": list(cands_val),
				"candidate_count": len(cands_val),
				"reason": reason,
			},
		)

	# Total
	try:
		roi = section.get("total")
		dpi = int((roi or {}).get("dpi") or 150)
		pix = render_normalized_roi_to_pixmap(pdf_path, 0, dpi=dpi, roi=roi or {})
		raw_tsv = ocr_pixmap_tsv(pix, psm=6, lang="eng")
		sorted_cands = None
		next_line_nums = None
		normalized = None
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
		if total_str == "!":
			anchors_val = locals().get("anchors", None)
			sorted_cands_val = locals().get("sorted_cands", None)
			next_line_nums_val = locals().get("next_line_nums", None)
			chosen_token_val = locals().get("chosen_token", None)
			normalized_val = locals().get("normalized", None)
			candidates_val = locals().get("candidates", None)
			reason = "UNKNOWN_TOTAL_FAIL"
			if not isinstance(anchors_val, list) or len(anchors_val) != 1:
				reason = "NO_UNIQUE_ANCHOR"
			elif chosen_token_val is not None:
				if normalized_val is None or not re.match(r"^\d+(\.\d{2})$", str(normalized_val)):
					reason = "NORMALIZATION_REJECTED"
			else:
				if isinstance(sorted_cands_val, list) and len(sorted_cands_val) == 0 and isinstance(candidates_val, list) and len(candidates_val) > 0:
					reason = "AMBIGUOUS_SAME_LINE_TIE"
				elif isinstance(candidates_val, list) and len(candidates_val) > 0:
					reason = "NO_VALID_SAME_LINE_CANDIDATE"
				else:
					if not isinstance(next_line_nums_val, list) or len(next_line_nums_val) == 0:
						reason = "NO_VALID_CANDIDATE"
					else:
						reason = "NO_VALID_NEXT_LINE_CANDIDATE"
			_phase2_debug_log(
				"PHASE2_TOTAL_FAIL",
				{
					"pdf_path": pdf_path,
					"file_type": file_type,
					"roi": roi,
					"dpi": (roi or {}).get("dpi"),
					"anchors_found": len(anchors_val) if isinstance(anchors_val, list) else None,
					"anchors": anchors_val if isinstance(anchors_val, list) else None,
					"same_line_candidates": sorted_cands_val,
					"next_line_candidates": next_line_nums_val,
					"chosen_token": chosen_token_val,
					"normalized_value": normalized_val,
					"reason": reason,
				},
			)
		if total_str != "!":
			anchors_val = locals().get("anchors", None)
			sorted_cands_val = locals().get("sorted_cands", None)
			next_line_nums_val = locals().get("next_line_nums", None)
			chosen_token_val = locals().get("chosen_token", None)
			normalized_val = locals().get("normalized", None)
			candidates_val = locals().get("candidates", None)
			if isinstance(anchors_val, list) and len(anchors_val) == 1:
				if chosen_token_val is not None and (
					isinstance(sorted_cands_val, list) or isinstance(candidates_val, list)
				):
					suspect_reason = None
					try:
						chosen_conf = float((chosen_token_val or {}).get("conf"))
					except Exception:
						chosen_conf = None
					if isinstance(chosen_conf, (int, float)) and chosen_conf < 70:
						suspect_reason = "LOW_CONFIDENCE_CHOSEN"
					elif isinstance(sorted_cands_val, list) and len(sorted_cands_val) >= 2:
						try:
							top1_conf = float((sorted_cands_val[0] or {}).get("conf"))
							top2_conf = float((sorted_cands_val[1] or {}).get("conf"))
						except Exception:
							top1_conf = None
							top2_conf = None
						if (
							isinstance(top1_conf, (int, float))
							and isinstance(top2_conf, (int, float))
							and top1_conf >= 50
							and top2_conf >= 50
							and (top1_conf - top2_conf) <= 5
						):
							suspect_reason = "CLOSE_SECOND_CANDIDATE"
					elif isinstance(sorted_cands_val, list) and len(sorted_cands_val) >= 3:
						suspect_reason = "MULTI_AMOUNT_TOKENS_SAME_LINE"
					if suspect_reason is not None:
						_phase2_debug_log(
							"PHASE2_TOTAL_SUSPECT_PASS",
							{
								"pdf_path": pdf_path,
								"file_type": file_type,
								"roi": roi,
								"dpi": (roi or {}).get("dpi"),
								"total_str": total_str,
								"anchors_found": len(anchors_val),
								"anchors": anchors_val,
								"chosen_token": chosen_token_val,
								"same_line_candidates": sorted_cands_val,
								"next_line_candidates": next_line_nums_val,
								"normalized_value": normalized_val,
								"reason": suspect_reason,
							},
						)
	except Exception:
		total_str = "!"
		roi_val = locals().get("roi", section.get("total"))
		anchors_val = locals().get("anchors", None)
		sorted_cands_val = locals().get("sorted_cands", None)
		next_line_nums_val = locals().get("next_line_nums", None)
		chosen_token_val = locals().get("chosen_token", None)
		normalized_val = locals().get("normalized", None)
		candidates_val = locals().get("candidates", None)
		reason = "UNKNOWN_TOTAL_FAIL"
		if not isinstance(anchors_val, list) or len(anchors_val) != 1:
			reason = "NO_UNIQUE_ANCHOR"
		elif chosen_token_val is not None:
			if normalized_val is None or not re.match(r"^\d+(\.\d{2})$", str(normalized_val)):
				reason = "NORMALIZATION_REJECTED"
		else:
			if isinstance(sorted_cands_val, list) and len(sorted_cands_val) == 0 and isinstance(candidates_val, list) and len(candidates_val) > 0:
				reason = "AMBIGUOUS_SAME_LINE_TIE"
			elif isinstance(candidates_val, list) and len(candidates_val) > 0:
				reason = "NO_VALID_SAME_LINE_CANDIDATE"
			else:
				if not isinstance(next_line_nums_val, list) or len(next_line_nums_val) == 0:
					reason = "NO_VALID_CANDIDATE"
				else:
					reason = "NO_VALID_NEXT_LINE_CANDIDATE"
		_phase2_debug_log(
			"PHASE2_TOTAL_FAIL",
			{
				"pdf_path": pdf_path,
				"file_type": file_type,
				"roi": roi_val,
				"dpi": (roi_val or {}).get("dpi") if isinstance(roi_val, dict) or roi_val is None else None,
				"anchors_found": len(anchors_val) if isinstance(anchors_val, list) else None,
				"anchors": anchors_val if isinstance(anchors_val, list) else None,
				"same_line_candidates": sorted_cands_val,
				"next_line_candidates": next_line_nums_val,
				"chosen_token": chosen_token_val,
				"normalized_value": normalized_val,
				"reason": reason,
			},
		)

	return (date_str, account_str, total_str)
