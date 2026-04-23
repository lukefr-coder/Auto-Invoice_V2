from __future__ import annotations

import re

from core.row_model import FileType
from services.ocr_runtime import (
	is_profile_complete,
	load_ocr_profile,
	ocr_pil_image,
	ocr_pil_image_tsv,
	ocr_pixmap,
	ocr_pixmap_tsv,
	pixmap_to_pil_gray,
	render_normalized_roi_to_pixmap,
	tighten_text_crop,
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


def _tsv_preview(raw_tsv: str | None, max_tokens: int = 12) -> str:
	"""Return a compact preview of TSV text content (non-header tokens)."""
	if not raw_tsv or not raw_tsv.strip():
		return ""
	tokens = []
	for line in raw_tsv.splitlines()[1:]:
		parts = line.split("\t")
		if len(parts) >= 12:
			txt = parts[11].strip()
			if txt:
				tokens.append(txt)
	preview = " ".join(tokens[:max_tokens])
	if len(tokens) > max_tokens:
		preview += f" ... (+{len(tokens) - max_tokens} more)"
	return preview


def _is_single_gst_like_preview(preview: str | None) -> bool:
	"""Return True when preview collapses to a single GST amount line like 'GST 11.85'."""
	p = re.sub(r"\s+", " ", str(preview or "").upper()).strip()
	if not p or "..." in p:
		return False
	return bool(re.match(r"^GST\s+\d[\d,]*(?:\.\d{2})$", p))


def _normalize_money_token_text(token_text: str) -> str | None:
	"""Normalize OCR money token text using the same strict rules as total parser."""
	raw = str(token_text or "").strip().replace("$", "").replace(" ", "")
	if re.match(r"^\d+,\d{2}$", raw):
		normalized = raw.replace(",", ".")
	elif re.match(r"^\d{1,3}(?:\.\d{3})+,\d{2}$", raw):
		normalized = raw.replace(".", "").replace(",", ".")
	elif re.match(r"^\d{1,3}(?:,\d{3})+(?:\.\d{2})$", raw):
		normalized = raw.replace(",", "")
	elif re.match(r"^\d+(\.\d{2})$", raw):
		normalized = raw
	else:
		return None
	if re.match(r"^\d+(\.\d{2})$", normalized):
		return f"{float(normalized):.2f}"
	return None


def _parse_tsv_tokens(raw_tsv: str) -> list[dict]:
	"""Parse non-empty word tokens from Tesseract TSV text."""
	tokens: list[dict] = []
	for line in (raw_tsv or "").splitlines():
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
			tokens.append(
				{
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
			)
		except Exception:
			continue
	return tokens


def _alt_psm11_secondary_total_rescue(raw_tsv: str, anchor_tok: dict) -> tuple[str | None, dict | None, int]:
	"""Secondary strict candidate search for alt psm=11 layout fragmentation cases."""
	tokens = _parse_tsv_tokens(raw_tsv)
	try:
		anchor_left = int(anchor_tok.get("left"))
		anchor_top = int(anchor_tok.get("top"))
		anchor_w = int(anchor_tok.get("width"))
		anchor_h = int(anchor_tok.get("height"))
	except Exception:
		return (None, None, 0)

	anchor_right = anchor_left + anchor_w
	anchor_cy = anchor_top + (anchor_h / 2.0)
	min_right_gap = max(3, int(anchor_h * 0.1))
	qualifying: list[dict] = []
	for tok in tokens:
		cand_text = str(tok.get("text") or "")
		if not re.match(r"^[\$\s]*\d[\d,]*(?:\.\d{2})?\s*$", cand_text):
			continue
		try:
			cand_left = int(tok.get("left"))
			cand_top = int(tok.get("top"))
			cand_h = int(tok.get("height"))
		except Exception:
			continue
		if cand_left < anchor_right + min_right_gap:
			continue
		cand_cy = cand_top + (cand_h / 2.0)
		max_y_delta = max(anchor_h, cand_h) * 1.25
		if abs(cand_cy - anchor_cy) > max_y_delta:
			continue
		qualifying.append(tok)

	if len(qualifying) != 1:
		return (None, None, len(qualifying))

	chosen = qualifying[0]
	normalized = _normalize_money_token_text(str(chosen.get("text") or ""))
	if normalized is None:
		return (None, None, len(qualifying))
	return (normalized, chosen, len(qualifying))


def _check_total_suspect(result: dict) -> str | None:
	"""Return a suspect reason if the parse result looks suspicious, else None.

	Reuses the same suspicion rules as the existing PHASE2_TOTAL_SUSPECT_PASS block.
	"""
	if result is None or result.get("total_str") == "!":
		return None
	anchors = result.get("anchors")
	if not isinstance(anchors, list) or len(anchors) != 1:
		return None
	chosen_token = result.get("chosen_token")
	sorted_cands = result.get("sorted_cands")
	candidates = result.get("candidates")
	if chosen_token is None or not (isinstance(sorted_cands, list) or isinstance(candidates, list)):
		return None
	try:
		chosen_conf = float((chosen_token or {}).get("conf"))
	except Exception:
		chosen_conf = None
	if isinstance(chosen_conf, (int, float)) and chosen_conf < 70:
		return "LOW_CONFIDENCE_CHOSEN"
	if isinstance(sorted_cands, list) and len(sorted_cands) >= 2:
		try:
			top1_conf = float((sorted_cands[0] or {}).get("conf"))
			top2_conf = float((sorted_cands[1] or {}).get("conf"))
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
			return "CLOSE_SECOND_CANDIDATE"
	return None


def _normalize_money_raw(raw_text: str) -> str | None:
	"""Normalize a raw OCR money string to 'DDDD.CC' format, or None."""
	raw = (raw_text or "").strip().replace("$", "").replace(" ", "").replace(",", "")
	if not raw:
		return None
	if re.match(r"^\d+(\.\d{2})$", raw):
		v = float(raw)
		return f"{v:.2f}"
	return None


def _check_single_digit_14_ambiguity(cleaned_norm: str, reread_norm: str) -> tuple[bool, str]:
	"""Check strict normalized-money disagreement shape for 1<->4 ambiguity.

	Returns (is_match, reason). The reason is suitable for debug logs.
	"""
	if not re.match(r"^\d+(\.\d{2})$", str(cleaned_norm or "")):
		return (False, "CLEANED_NOT_NORMALIZED_MONEY")
	if not re.match(r"^\d+(\.\d{2})$", str(reread_norm or "")):
		return (False, "REREAD_NOT_NORMALIZED_MONEY")
	if len(cleaned_norm) != len(reread_norm):
		return (False, "LENGTH_MISMATCH")
	if cleaned_norm.find(".") != reread_norm.find("."):
		return (False, "FORMAT_MISMATCH")

	diff_pairs: list[tuple[str, str]] = []
	for left_c, right_c in zip(cleaned_norm, reread_norm):
		if left_c == right_c:
			continue
		if not (left_c.isdigit() and right_c.isdigit()):
			return (False, "NON_DIGIT_DISAGREEMENT")
		diff_pairs.append((left_c, right_c))

	if len(diff_pairs) != 1:
		return (False, "NOT_SINGLE_DIGIT_DISAGREEMENT")
	left_d, right_d = diff_pairs[0]
	if {left_d, right_d} != {"1", "4"}:
		return (False, "DISAGREEMENT_NOT_IN_1_4_CLASS")
	return (True, "AMBIGUITY_1_4_SINGLE_DIGIT")


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


# ── Account-no pilot helpers (Phase 2 doubtful-read recovery) ──────────


def _account_per_char_ocr(tight_img) -> str | None:
	"""Split a tightened text image into character cells and OCR each.

	Returns a 5-char account string ``[A-Z][0-9]{4}`` or ``None``.
	"""
	w, h = tight_img.size
	if w < 15 or h < 8:
		return None

	_INK = 160
	px = tight_img.load()

	# Detect per-column ink presence
	col_has_ink = [
		any(px[x, y] < _INK for y in range(h))
		for x in range(w)
	]

	# Group contiguous ink columns
	groups: list[tuple[int, int]] = []
	in_grp = False
	start = 0
	for i, has in enumerate(col_has_ink):
		if has and not in_grp:
			start = i
			in_grp = True
		elif not has and in_grp:
			groups.append((start, i))
			in_grp = False
	if in_grp:
		groups.append((start, w))

	# Merge groups separated by tiny gap (≤ 2 px)
	if len(groups) > 1:
		merged: list[tuple[int, int]] = [groups[0]]
		for g in groups[1:]:
			prev = merged[-1]
			if g[0] - prev[1] <= 2:
				merged[-1] = (prev[0], g[1])
			else:
				merged.append(g)
		groups = merged

	# Require exactly 5 character groups; fall back to equal split
	if len(groups) != 5:
		cell_w = w / 5
		groups = [(int(i * cell_w), int((i + 1) * cell_w)) for i in range(5)]

	chars: list[str] = []
	for idx, (x0, x1) in enumerate(groups):
		cx0 = max(0, x0 - 1)
		cx1 = min(w, x1 + 1)
		cell = tight_img.crop((cx0, 0, cx1, h))
		wl = "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if idx == 0 else "0123456789"
		ch = ocr_pil_image(cell, psm=10, lang="eng", whitelist=wl).strip()
		if not ch or len(ch) != 1:
			return None
		chars.append(ch.upper())

	result = "".join(chars)
	if re.match(r"^[A-Z][0-9]{4}$", result):
		return result
	return None


def _account_no_pilot(pix) -> str | None:
	"""Doubtful-read pilot: refine ROI and attempt rebuild for account_no.

	Converts the existing pixmap to a cleaned grayscale crop, runs a
	whole-string OCR retry, then a per-character verify/rebuild.
	Returns a validated 5-char account string or ``None``.
	"""
	try:
		gray = pixmap_to_pil_gray(pix)
	except Exception:
		return None

	tight = tighten_text_crop(gray, pad_px=4)
	if tight is None:
		return None

	tw, th = tight.size
	if tw < 10 or th < 8:
		return None

	# Whole-string OCR retry on cleaned crop
	whole_text = ocr_pil_image(
		tight,
		psm=7,
		lang="eng",
		whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
	).strip().upper().replace(" ", "")

	whole_candidate = None
	if whole_text and len(whole_text) == 5 and re.match(r"^[A-Z][0-9]{4}$", whole_text):
		whole_candidate = whole_text

	# Per-character verify/rebuild
	char_candidate = _account_per_char_ocr(tight)

	# Decision: require both paths to agree (corroboration).
	# A single-path result is not accepted to avoid uncorroborated wrong-valid rescues.
	if char_candidate is not None and whole_candidate is not None:
		if char_candidate == whole_candidate:
			return char_candidate
		# Paths disagree: cannot determine which is correct → fail-closed.
		return None
	# Only one path succeeded: insufficient corroboration → fail-closed.
	return None


def _extract_account_candidates(raw_text: str | None) -> tuple[list[str], set[str], str]:
	"""Extract strict account candidates using narrow account-specific normalization."""
	t = (raw_text or "").upper()
	t = re.sub(r"\b([A-Z$])\s+([0-9OIL]{4})\b", r"\1\2", t)
	t = re.sub(r"(?<=\d)O(?=\d)", "0", t)
	t = re.sub(r"(?<=\d)[IL](?=\d)", "1", t)
	t = re.sub(r"(?<=\d)[IL](?=\b)", "1", t)
	t = re.sub(r"(?<=\d)O(?=\b)", "0", t)

	matches: list[str] = []
	cands: set[str] = set()
	for token in re.findall(r"[A-Z0-9$]+", t):
		# Narrow noisy-prefix handling: only trim to a 5-char suffix token.
		cand_token = token[-5:] if len(token) > 5 else token
		if len(cand_token) != 5:
			continue
		lead = cand_token[0]
		tail = cand_token[1:].replace("O", "0").replace("I", "1").replace("L", "1")
		if not tail.isdigit():
			continue
		if lead == "$":
			lead = "S"
		candidate = f"{lead}{tail}"
		if re.match(r"^[A-Z][0-9]{4}$", candidate):
			matches.append(candidate)
			cands.add(candidate)

	return (matches, cands, t)


def _detect_account_anchor(raw_tsv: str) -> dict:
	"""Detect the Account label anchor in original ACCOUNT ROI TSV.

	Matches TSV tokens to the label "ACCOUNT" using narrow, deterministic
	rules only. No fuzzy or contains matching.

	Returns a dict with:
	  anchor_found (bool)
	  anchors (list of matched anchor dicts; expected 0 or 1)
	  reason (str): EMPTY_TSV | NO_ANCHOR | MULTIPLE_ANCHORS | OK
	"""

	def _norm_token(s: str) -> str:
		"""Narrow OCR-confusion normalization: only visually close chars in ACCOUNT."""
		u = s.upper()
		# 0 -> O, 1 -> I, | -> I  (characters likely confused in ACCOUNT glyphs)
		u = u.replace("0", "O").replace("1", "I").replace("|", "I")
		return u

	if not raw_tsv or not raw_tsv.strip():
		return {"anchor_found": False, "anchors": [], "reason": "EMPTY_TSV"}

	# Parse TSV tokens.
	tokens: list[dict] = []
	for line in raw_tsv.splitlines():
		if not line or not line.strip():
			continue
		cols = line.split("\t")
		if len(cols) < 12:
			continue
		if cols[0].strip().lower() == "level":
			continue
		text = cols[11].strip()
		if not text:
			continue
		try:
			tok = {
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
		tokens.append(tok)

	# Group tokens by (block_num, par_num, line_num) for adjacent-join check.
	lines_map: dict[tuple, list[dict]] = {}
	for tok in tokens:
		key = (tok["block_num"], tok["par_num"], tok["line_num"])
		lines_map.setdefault(key, []).append(tok)
	for key in lines_map:
		lines_map[key].sort(key=lambda t: t["word_num"])

	anchors: list[dict] = []
	seen_line_keys: set[tuple] = set()

	for tok in tokens:
		text_raw = tok["text"]
		text_u = text_raw.upper()
		text_norm = _norm_token(text_raw)
		match_mode: str | None = None

		# Rule 1: exact single-token match.
		if text_u == "ACCOUNT":
			match_mode = "exact_token"

		# Rule 2: single-token match after narrow OCR-confusion normalization.
		elif text_norm == "ACCOUNT":
			match_mode = "normalized_token"

		# Rule 3: adjacent two-token join on same line concatenates to ACCOUNT.
		else:
			line_key = (tok["block_num"], tok["par_num"], tok["line_num"])
			if line_key not in seen_line_keys:
				line_toks = lines_map.get(line_key, [])
				for i, lt in enumerate(line_toks[:-1]):
					next_lt = line_toks[i + 1]
					joined = _norm_token(lt["text"]) + _norm_token(next_lt["text"])
					if joined == "ACCOUNT":
						match_mode = "adjacent_join"
						# Use first token as anchor representative.
						tok = lt
						text_raw = lt["text"]
						text_norm = joined
						break

		if match_mode is not None:
			line_key = (tok["block_num"], tok["par_num"], tok["line_num"])
			if line_key not in seen_line_keys:
				seen_line_keys.add(line_key)
				anchors.append({
					"text_raw": text_raw,
					"text_normalized": text_norm,
					"block_num": tok["block_num"],
					"par_num": tok["par_num"],
					"line_num": tok["line_num"],
					"word_num": tok["word_num"],
					"left": tok["left"],
					"top": tok["top"],
					"width": tok["width"],
					"height": tok["height"],
					"conf": tok["conf"],
					"match_mode": match_mode,
				})

	if not anchors:
		return {"anchor_found": False, "anchors": [], "reason": "NO_ANCHOR"}
	if len(anchors) > 1:
		return {"anchor_found": False, "anchors": anchors, "reason": "MULTIPLE_ANCHORS"}
	return {"anchor_found": True, "anchors": anchors, "reason": "OK"}


def _try_parse_total_from_tsv(raw_tsv: str) -> dict:
	"""Parse total from Tesseract TSV output using strict anchor/candidate logic.

	Returns a dict with parsing results and intermediate state for debug logging.
	Keys: total_str, anchors, sorted_cands, next_line_nums, chosen_token,
	      normalized, candidates.
	"""
	result: dict = {
		"total_str": "!",
		"anchors": None,
		"sorted_cands": None,
		"next_line_nums": None,
		"chosen_token": None,
		"normalized": None,
		"candidates": None,
		"low_conf_same_line_fallback_used": False,
	}

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
		text_u_raw = str(tok.get("text") or "").strip().upper()
		# Keep exact anchor match primary; allow only tiny OCR confusion normalization.
		text_u_norm = text_u_raw.replace("!", "L").replace("|", "L")
		if "TOTALEX" in text_u_norm:
			continue
		if text_u_raw != "TOTAL" and text_u_norm != "TOTAL":
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
			next_text_u_raw = str(next_tok.get("text") or "").strip().upper()
			next_text_u_norm = next_text_u_raw.replace("!", "L").replace("|", "L")
			if next_text_u_raw == "EX" or next_text_u_norm == "EX":
				continue
		anchors.append(tok)

	result["anchors"] = anchors

	chosen_token = None
	candidates = None
	sorted_cands = None
	next_line_nums = None
	low_conf_same_line_fallback_used = False

	if len(anchors) == 1:
		anchor = anchors[0]
		anchor_line_key = (anchor.get("block_num"), anchor.get("par_num"), anchor.get("line_num"))
		total_right = int(anchor.get("left")) + int(anchor.get("width"))
		height = int(anchor.get("height"))
		small_gap_px = max(5, int(height * 0.2))
		candidates = []
		same_line_strict_amount_tokens = []
		for tok in tokens:
			if (tok.get("block_num"), tok.get("par_num"), tok.get("line_num")) != anchor_line_key:
				continue
			cand_left = int(tok.get("left"))
			if cand_left < total_right + small_gap_px:
				continue
			cand_text = str(tok.get("text") or "")
			if not re.match(r"^[\$\s]*\d[\d,]*(?:\.\d{2})?\s*$", cand_text):
				continue
			same_line_strict_amount_tokens.append(tok)
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
			# Narrow fail-closed rescue: one strict same-line amount token rejected only by confidence gate.
			if len(same_line_strict_amount_tokens) == 1:
				chosen_token = same_line_strict_amount_tokens[0]
				low_conf_same_line_fallback_used = True
			else:
				anchor_block_par = (anchor.get("block_num"), anchor.get("par_num"))
				anchor_line_num = int(anchor.get("line_num"))
				next_line_num = anchor_line_num + 1
				next_line_nums = []
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

	result["candidates"] = candidates
	result["sorted_cands"] = sorted_cands
	result["next_line_nums"] = next_line_nums
	result["chosen_token"] = chosen_token
	result["low_conf_same_line_fallback_used"] = low_conf_same_line_fallback_used

	normalized = None
	if chosen_token is not None:
		raw = str(chosen_token.get("text") or "").strip()
		raw = raw.replace("$", "").replace(" ", "")
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
			result["total_str"] = f"{v:.2f}"

	result["normalized"] = normalized
	return result


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
		# ── Slice 1+2: Account anchor detection on original ROI TSV ─────────
		# Primary: original ROI TSV at psm=6.
		_acct_tsv_psm6 = ocr_pixmap_tsv(pix, psm=6, lang="eng")
		_acct_tsv_psm6_preview = _tsv_preview(_acct_tsv_psm6)
		_anchor_result_psm6 = _detect_account_anchor(_acct_tsv_psm6)

		# Slice 2 companion: original ROI TSV at psm=11, consulted only when
		# primary psm=6 does not find an anchor.
		_acct_tsv_psm11 = None
		_acct_tsv_psm11_preview = ""
		_anchor_result_psm11: dict = {"anchor_found": False, "anchors": [], "reason": "NOT_RUN"}
		if not _anchor_result_psm6["anchor_found"]:
			_acct_tsv_psm11 = ocr_pixmap_tsv(pix, psm=11, lang="eng")
			_acct_tsv_psm11_preview = _tsv_preview(_acct_tsv_psm11)
			_anchor_result_psm11 = _detect_account_anchor(_acct_tsv_psm11)

		# Resolve final anchor: psm=6 wins; psm=11 used only on primary miss.
		if _anchor_result_psm6["anchor_found"]:
			_anchor_result = _anchor_result_psm6
			_anchor_source_used = "original_psm6"
		elif _anchor_result_psm11["anchor_found"]:
			_anchor_result = _anchor_result_psm11
			_anchor_source_used = "original_psm11"
		else:
			_anchor_result = _anchor_result_psm6  # carry primary non-found for reason
			_anchor_source_used = "none"

		_anchor_meta = _anchor_result["anchors"][0] if _anchor_result["anchor_found"] else None
		_phase2_debug_log("PHASE2_ACCOUNT_ANCHOR", {
			"pdf_path": pdf_path,
			"file_type": file_type,
			"roi": roi,
			"dpi": (roi or {}).get("dpi"),
			"primary": {
				"source": "original_psm6",
				"existed": _acct_tsv_psm6 is not None,
				"non_empty": bool(_acct_tsv_psm6 and _acct_tsv_psm6.strip()),
				"preview": _acct_tsv_psm6_preview,
				"anchor_found": _anchor_result_psm6["anchor_found"],
				"anchor_count": len(_anchor_result_psm6["anchors"]),
				"reason": _anchor_result_psm6["reason"],
			},
			"companion": {
				"source": "original_psm11",
				"existed": _acct_tsv_psm11 is not None,
				"non_empty": bool(_acct_tsv_psm11 and _acct_tsv_psm11.strip()),
				"preview": _acct_tsv_psm11_preview,
				"anchor_found": _anchor_result_psm11["anchor_found"],
				"anchor_count": len(_anchor_result_psm11["anchors"]),
				"reason": _anchor_result_psm11["reason"],
			},
			"anchor_source_used": _anchor_source_used,
			"anchor_found": _anchor_result["anchor_found"],
			"anchor": {
				"text_raw": _anchor_meta["text_raw"],
				"text_normalized": _anchor_meta["text_normalized"],
				"line": (_anchor_meta["block_num"], _anchor_meta["par_num"], _anchor_meta["line_num"]),
				"bbox": (_anchor_meta["left"], _anchor_meta["top"], _anchor_meta["width"], _anchor_meta["height"]),
				"conf": _anchor_meta["conf"],
				"match_mode": _anchor_meta["match_mode"],
			} if _anchor_meta is not None else None,
		})
		# Cleaned/tightened OCR is the primary account read.
		cleaned_raw = ""
		raw = ""
		primary_source = "cleaned"
		try:
			gray = pixmap_to_pil_gray(pix)
			tight = tighten_text_crop(gray, pad_px=4)
			if tight is not None:
				tw, th = tight.size
				if tw >= 10 and th >= 8:
					cleaned_raw = ocr_pil_image(
						tight,
						psm=7,
						lang="eng",
						whitelist="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$",
					).strip()
		except Exception:
			cleaned_raw = ""

		matches, cands, t = _extract_account_candidates(cleaned_raw)

		# Raw OCR remains fallback only when cleaned-primary is not uniquely valid.
		if len(cands) != 1:
			primary_source = "raw"
			raw = ocr_pixmap(pix, psm=6, lang="eng")
			matches, cands, t = _extract_account_candidates(raw)

		if len(cands) == 1:
			account_str = next(iter(cands))
		if account_str == "!":
			reason = "UNKNOWN_ACCOUNT_FAIL"
			if primary_source == "cleaned" and (not cleaned_raw or cleaned_raw.strip() == ""):
				reason = "EMPTY_CLEANED_OCR"
			elif primary_source == "raw" and (not raw or raw.strip() == ""):
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
					"raw_ocr": cleaned_raw if primary_source == "cleaned" else raw,
					"raw_ocr_cleaned": cleaned_raw,
					"raw_ocr_raw": raw,
					"primary_source": primary_source,
					"normalized_text": t,
					"regex_matches": matches,
					"unique_candidates": list(cands),
					"candidate_count": len(cands),
					"reason": reason,
				},
			)
		if account_str != "!":
			account_str_val = account_str
			raw_val = cleaned_raw if primary_source == "cleaned" else raw
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
						"primary_source": primary_source,
						"normalized_text": t_val,
						"account_str": account_str_val,
						"matches": matches,
						"unique_candidates": list(cands),
						"reason": "SUSPECT_LEADING_LETTER",
					},
				)
		# ── Account pilot: doubtful-only read recovery ──
		_acct_doubtful = (account_str == "!")
		if _acct_doubtful:
			_pilot = _account_no_pilot(pix)
			if _pilot is not None and re.match(r"^[A-Z][0-9]{4}$", _pilot):
				_phase2_debug_log("PHASE2_ACCOUNT_PILOT_ACCEPT", {
					"pdf_path": pdf_path,
					"original": account_str,
					"pilot_result": _pilot,
					"primary_source": primary_source,
				})
				account_str = _pilot
	except Exception:
		account_str = "!"
		raw_val = locals().get("raw", None)
		cleaned_raw_val = locals().get("cleaned_raw", None)
		primary_source_val = locals().get("primary_source", "cleaned")
		roi_val = locals().get("roi", section.get("account_no"))
		t_val = locals().get("t", "")
		matches_val = locals().get("matches", [])
		cands_val = locals().get("cands", set())
		reason = "UNKNOWN_ACCOUNT_FAIL"
		if primary_source_val == "cleaned" and (not cleaned_raw_val or str(cleaned_raw_val).strip() == ""):
			reason = "EMPTY_CLEANED_OCR"
		elif primary_source_val == "raw" and (not raw_val or str(raw_val).strip() == ""):
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
				"raw_ocr": cleaned_raw_val if primary_source_val == "cleaned" else raw_val,
				"raw_ocr_cleaned": cleaned_raw_val,
				"raw_ocr_raw": raw_val,
				"primary_source": primary_source_val,
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

		# ── Phase 2 total pilot: cleaned/tightened crop as default TSV input ──
		_cleaned_tsv = None
		try:
			_gray = pixmap_to_pil_gray(pix)
			_tight = tighten_text_crop(_gray, pad_px=4)
			if _tight is not None:
				_tw, _th = _tight.size
				if _tw >= 15 and _th >= 8:
					_cleaned_tsv = ocr_pil_image_tsv(_tight, psm=6, lang="eng")
		except Exception:
			_cleaned_tsv = None

		# Try cleaned TSV first (default path)
		_total_result = None
		_total_winner = None
		_cleaned_result_failed = None
		_cleaned_preview = _tsv_preview(_cleaned_tsv)
		if _cleaned_tsv and _cleaned_tsv.strip():
			_cleaned_try = _try_parse_total_from_tsv(_cleaned_tsv)
			_total_result = _cleaned_try
			if _cleaned_try["total_str"] == "!":
				_cleaned_result_failed = _cleaned_try
				_total_result = None  # structural failure, fall back
			else:
				_total_winner = "cleaned"
		_phase2_debug_log(
			"PHASE2_TOTAL_CLEANED_TSV",
			{
				"pdf_path": pdf_path,
				"file_type": file_type,
				"source": "cleaned",
				"existed": _cleaned_tsv is not None,
				"non_empty": bool(_cleaned_tsv and _cleaned_tsv.strip()),
				"preview": _cleaned_preview,
				"parsed_total": _total_result["total_str"] if _total_result else None,
			},
		)

		# ── Token-level alternate OCR re-read (all valid cleaned winners) ──
		_token_reread_flipped = False
		if _total_result is not None and _total_winner == "cleaned":
			_ct = _total_result.get("chosen_token")
			if _ct is not None and _tight is not None:
				try:
					# Strong cleaned reads are hard-kept and never overridden by token reread.
					try:
						_cleaned_chosen_conf = float((_ct or {}).get("conf"))
					except Exception:
						_cleaned_chosen_conf = None
					_STRONG_CLEANED_KEEP_CONF = 90.0
					_hard_keep_cleaned = (
						isinstance(_cleaned_chosen_conf, (int, float))
						and _cleaned_chosen_conf >= _STRONG_CLEANED_KEEP_CONF
					)
					_cleaned_total_str = _total_result["total_str"]
					_cleaned_suspect_reason = _check_total_suspect(_total_result)
					_reread_override_eligible = False
					_reread_override_applied = False
					_reject_reason = "HARD_KEEP_STRONG_CLEANED_CONF" if _hard_keep_cleaned else None

					_tl = int(_ct["left"])
					_tt_top = int(_ct["top"])
					_tr = _tl + int(_ct["width"])
					_tb = _tt_top + int(_ct["height"])
					_iw, _ih = _tight.size
					_crop_box = (
						max(0, _tl - 2),
						max(0, _tt_top - 1),
						min(_iw, _tr + 2),
						min(_ih, _tb + 1),
					)
					if _crop_box[2] > _crop_box[0] and _crop_box[3] > _crop_box[1]:
						_token_crop = _tight.crop(_crop_box)
						_token_raw = ocr_pil_image(
							_token_crop, psm=8, lang="eng",
							whitelist="0123456789.,$",
						)
						_token_norm = _normalize_money_raw(_token_raw)
						_token_flip = False
						_flip_source = None
						_alt_raw = None
						_alt_norm = None
						_primary_ambiguity_reason = None
						_alt_ambiguity_reason = None
						if not _hard_keep_cleaned:
							if _token_norm is None:
								_reject_reason = "PRIMARY_NOT_NORMALIZED_MONEY"
							elif _token_norm == _cleaned_total_str:
								_reject_reason = "PRIMARY_NO_DISAGREEMENT"
							else:
								_is_ambiguous, _primary_ambiguity_reason = _check_single_digit_14_ambiguity(
									_cleaned_total_str,
									_token_norm,
								)
								if _is_ambiguous:
									_reread_override_eligible = True
									_token_flip = True
									_flip_source = "primary"
									_total_result = dict(_total_result)
									_total_result["total_str"] = _token_norm
									_total_result["normalized"] = _token_norm
									_total_winner = "cleaned_token_reread"
									_token_reread_flipped = True
									_reread_override_applied = True
								else:
									_reject_reason = f"PRIMARY_{_primary_ambiguity_reason}"

						# ── Alternate token reread when primary returned same as cleaned ──
						if (
							not _token_flip
							and not _hard_keep_cleaned
							and _token_norm is not None
							and _token_norm == _cleaned_total_str
						):
							_alt_crop_box = (
								max(0, _tl - 4),
								max(0, _tt_top - 1),
								min(_iw, _tr + 2),
								min(_ih, _tb + 1),
							)
							if _alt_crop_box[2] > _alt_crop_box[0] and _alt_crop_box[3] > _alt_crop_box[1]:
								_alt_crop = _tight.crop(_alt_crop_box)
								_alt_raw = ocr_pil_image(
									_alt_crop, psm=7, lang="eng",
									whitelist="0123456789.,$",
								)
								_alt_norm = _normalize_money_raw(_alt_raw)
								if _alt_norm is None:
									if _reject_reason in {None, "PRIMARY_NO_DISAGREEMENT"}:
										_reject_reason = "ALT_NOT_NORMALIZED_MONEY"
								elif _alt_norm == _cleaned_total_str:
									if _reject_reason in {None, "PRIMARY_NO_DISAGREEMENT"}:
										_reject_reason = "ALT_NO_DISAGREEMENT"
								else:
									_is_ambiguous, _alt_ambiguity_reason = _check_single_digit_14_ambiguity(
										_cleaned_total_str,
										_alt_norm,
									)
									if _is_ambiguous:
										_reread_override_eligible = True
										_token_flip = True
										_flip_source = "alternate"
										_total_result = dict(_total_result)
										_total_result["total_str"] = _alt_norm
										_total_result["normalized"] = _alt_norm
										_total_winner = "cleaned_token_reread_alt"
										_token_reread_flipped = True
										_reread_override_applied = True
									else:
										_reject_reason = f"ALT_{_alt_ambiguity_reason}"

						if not _reread_override_applied and _reject_reason is None:
							_reject_reason = "REREAD_OVERRIDE_NOT_ELIGIBLE"

						_phase2_debug_log(
							"PHASE2_TOTAL_TOKEN_REREAD",
							{
								"pdf_path": pdf_path,
								"file_type": file_type,
								"cleaned_total": _cleaned_total_str,
								"cleaned_suspect_reason": _cleaned_suspect_reason,
								"cleaned_chosen_token_conf": _cleaned_chosen_conf,
								"cleaned_hard_keep": _hard_keep_cleaned,
								"token_reread_raw": (_token_raw or "").strip(),
								"token_reread_normalized": _token_norm,
								"alt_reread_raw": (_alt_raw or "").strip() if _alt_raw is not None else None,
								"alt_reread_normalized": _alt_norm,
								"reread_override_eligible": _reread_override_eligible,
								"reread_override_applied": _reread_override_applied,
								"reread_override_reject_reason": _reject_reason,
								"flip_to_token_reread": _token_flip,
								"flip_source": _flip_source,
								"chosen_token_text": str((_ct or {}).get("text", "")),
								"chosen_token_conf": (_ct or {}).get("conf"),
								"primary_ambiguity_reason": _primary_ambiguity_reason,
								"alt_ambiguity_reason": _alt_ambiguity_reason,
							},
						)
				except Exception:
					pass

		# ── Suspect cleaned-win second look (on-demand original ROI) ──
		if _total_result is not None and _total_winner == "cleaned" and not _token_reread_flipped:
			_cleaned_suspect = _check_total_suspect(_total_result)
			if _cleaned_suspect is not None:
				_orig_tsv_2nd = ocr_pixmap_tsv(pix, psm=6, lang="eng")
				_orig_result_2nd = _try_parse_total_from_tsv(_orig_tsv_2nd)
				_orig_valid = _orig_result_2nd["total_str"] != "!"
				_orig_suspect = _check_total_suspect(_orig_result_2nd) if _orig_valid else None
				_orig_agrees = _orig_valid and _orig_result_2nd["total_str"] == _total_result["total_str"]
				_flip = _orig_valid and not _orig_agrees and _orig_suspect is None
				_phase2_debug_log(
					"PHASE2_TOTAL_SUSPECT_SECOND_LOOK",
					{
						"pdf_path": pdf_path,
						"file_type": file_type,
						"cleaned_total": _total_result["total_str"],
						"cleaned_suspect_reason": _cleaned_suspect,
						"orig_total": _orig_result_2nd["total_str"],
						"orig_valid": _orig_valid,
						"orig_agrees": _orig_agrees,
						"orig_suspect_reason": _orig_suspect,
						"flip_to_original": _flip,
						"preview_orig": _tsv_preview(_orig_tsv_2nd),
					},
				)
				if _flip:
					_total_result = _orig_result_2nd
					_total_winner = "cleaned_suspect_flipped"

		# Fallback: original ROI TSV
		_orig_tsv = None
		_orig_preview = ""
		_orig_result_failed = None
		if _total_result is None:
			_orig_tsv = ocr_pixmap_tsv(pix, psm=6, lang="eng")
			_orig_preview = _tsv_preview(_orig_tsv)
			_total_result = _try_parse_total_from_tsv(_orig_tsv)
			_total_winner = "fallback"
			if _total_result["total_str"] == "!":
				_orig_result_failed = _total_result
			_phase2_debug_log(
				"PHASE2_TOTAL_FALLBACK_TSV",
				{
					"pdf_path": pdf_path,
					"file_type": file_type,
					"source": "original_fallback",
					"existed": _orig_tsv is not None,
					"non_empty": bool(_orig_tsv and _orig_tsv.strip()),
					"preview": _orig_preview,
					"parsed_total": _total_result["total_str"] if _total_result else None,
				},
			)

		# Narrow rescue: when both normal views miss anchor and collapse to same GST-only preview,
		# try one alternate sparse-text OCR pass on the same ROI.
		if (
			_total_result is not None
			and _total_result.get("total_str") == "!"
			and _cleaned_result_failed is not None
			and _orig_result_failed is not None
		):
			_cleaned_anchors = _cleaned_result_failed.get("anchors")
			_orig_anchors = _orig_result_failed.get("anchors")
			_no_anchor_both = (
				isinstance(_cleaned_anchors, list)
				and len(_cleaned_anchors) == 0
				and isinstance(_orig_anchors, list)
				and len(_orig_anchors) == 0
			)
			_cleaned_preview_norm = re.sub(r"\s+", " ", str(_cleaned_preview or "").upper()).strip()
			_orig_preview_norm = re.sub(r"\s+", " ", str(_orig_preview or "").upper()).strip()
			_same_single_gst_preview = (
				_is_single_gst_like_preview(_cleaned_preview)
				and _is_single_gst_like_preview(_orig_preview)
				and _cleaned_preview_norm == _orig_preview_norm
			)
			_should_run_psm11 = _no_anchor_both and _same_single_gst_preview
			_alt_preview = ""
			_alt_usable = False
			_alt_secondary_ran = False
			_alt_secondary_qualifying_count = 0
			_alt_secondary_used = False
			if _should_run_psm11:
				_alt_tsv = ocr_pixmap_tsv(pix, psm=11, lang="eng")
				_alt_preview = _tsv_preview(_alt_tsv)
				_alt_result = _try_parse_total_from_tsv(_alt_tsv)
				_alt_usable = _alt_result.get("total_str") != "!"
				if (
					not _alt_usable
					and isinstance(_alt_result.get("anchors"), list)
					and len(_alt_result.get("anchors")) == 1
				):
					_alt_secondary_ran = True
					_alt_norm, _alt_tok, _alt_secondary_qualifying_count = _alt_psm11_secondary_total_rescue(
						_alt_tsv,
						_alt_result["anchors"][0],
					)
					if _alt_norm is not None and _alt_tok is not None:
						_alt_result = dict(_alt_result)
						_alt_result["chosen_token"] = _alt_tok
						_alt_result["normalized"] = _alt_norm
						_alt_result["total_str"] = _alt_norm
						_alt_usable = True
						_alt_secondary_used = True
				if _alt_usable:
					_total_result = _alt_result
					_total_winner = "fallback_psm11_anchor_rescue"
			_phase2_debug_log(
				"PHASE2_TOTAL_ALT_PSM11",
				{
					"pdf_path": pdf_path,
					"file_type": file_type,
					"ran": _should_run_psm11,
					"cleaned_preview": _cleaned_preview,
					"fallback_preview": _orig_preview,
					"alt_preview": _alt_preview,
					"alt_secondary_ran": _alt_secondary_ran,
					"alt_secondary_qualifying_count": _alt_secondary_qualifying_count,
					"alt_secondary_used": _alt_secondary_used,
					"alt_usable": _alt_usable,
				},
			)

		total_str = _total_result["total_str"]
		anchors = _total_result["anchors"]
		sorted_cands = _total_result["sorted_cands"]
		next_line_nums = _total_result["next_line_nums"]
		chosen_token = _total_result["chosen_token"]
		normalized = _total_result["normalized"]
		candidates = _total_result["candidates"]
		low_conf_same_line_fallback_used = _total_result.get("low_conf_same_line_fallback_used", False)

		_phase2_debug_log(
			"PHASE2_TOTAL_FINAL",
			{
				"pdf_path": pdf_path,
				"file_type": file_type,
				"winner": _total_winner,
				"final_total_str": total_str,
				"chosen_token": chosen_token,
				"normalized": normalized,
				"low_conf_same_line_fallback_used": low_conf_same_line_fallback_used,
			},
		)

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
