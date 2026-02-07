from __future__ import annotations

from datetime import date

from core.app_state import AppState
from core.filters import FilterModel
from core.row_model import FileType, RowModel, RowStatus


def _fmt_demo_date(iso_yyyy_mm_dd: str) -> str:
	try:
		y, m, d = (int(p) for p in iso_yyyy_mm_dd.split("-"))
		return date(y, m, d).strftime("%d/%m/%y")
	except Exception:
		return iso_yyyy_mm_dd


def _checkbox_enabled(file_type: FileType, status: RowStatus) -> bool:
	# Slice 02 rule: enabled only for Tax Invoice / Proforma AND Ready/Processed.
	if file_type not in (FileType.TaxInvoice, FileType.Proforma):
		return False
	if status not in (RowStatus.Ready, RowStatus.Processed):
		return False
	return True


def make_initial_state() -> AppState:
	rows: list[RowModel] = [
		RowModel(
			id="r1",
			file_name="INV-0001",
			file_type=FileType.TaxInvoice,
			date_str=_fmt_demo_date("2026-02-01"),
			account_str="",
			total_str="",
			status=RowStatus.Ready,
			checked=False,
			checkbox_enabled=_checkbox_enabled(FileType.TaxInvoice, RowStatus.Ready),
		),
		RowModel(
			id="r2",
			file_name="INV-0002",
			file_type=FileType.TaxInvoice,
			date_str="",
			account_str="",
			total_str="",
			status=RowStatus.Processed,
			checked=True,
			checkbox_enabled=_checkbox_enabled(FileType.TaxInvoice, RowStatus.Processed),
		),
		RowModel(
			id="r3",
			file_name="ORDER-914",
			file_type=FileType.Order,
			date_str="",
			account_str="",
			total_str="",
			status=RowStatus.Ready,
			checked=False,
			checkbox_enabled=_checkbox_enabled(FileType.Order, RowStatus.Ready),
		),
		RowModel(
			id="r4",
			file_name="PF-774",
			file_type=FileType.Proforma,
			date_str="",
			account_str="",
			total_str="",
			status=RowStatus.Ready,
			checked=False,
			checkbox_enabled=_checkbox_enabled(FileType.Proforma, RowStatus.Ready),
		),
		RowModel(
			id="r5",
			file_name="TRANSFER-22",
			file_type=FileType.Transfer,
			date_str="",
			account_str="",
			total_str="",
			status=RowStatus.Ready,
			checked=False,
			checkbox_enabled=_checkbox_enabled(FileType.Transfer, RowStatus.Ready),
		),
		RowModel(
			id="r6",
			file_name="CREDIT-51",
			file_type=FileType.Credit,
			date_str="",
			account_str="",
			total_str="",
			status=RowStatus.Ready,
			checked=False,
			checkbox_enabled=_checkbox_enabled(FileType.Credit, RowStatus.Ready),
		),
		RowModel(
			id="r7",
			file_name="INV-REVIEW-3",
			file_type=FileType.TaxInvoice,
			date_str="",
			account_str="",
			total_str="",
			status=RowStatus.Review,
			checked=False,
			checkbox_enabled=_checkbox_enabled(FileType.TaxInvoice, RowStatus.Review),
		),
		RowModel(
			id="r8",
			file_name="PF-REVIEW-9",
			file_type=FileType.Proforma,
			date_str="",
			account_str="",
			total_str="",
			status=RowStatus.Review,
			checked=False,
			checkbox_enabled=_checkbox_enabled(FileType.Proforma, RowStatus.Review),
		),
		RowModel(
			id="r9",
			file_name="ORDER-REVIEW",
			file_type=FileType.Order,
			date_str="",
			account_str="",
			total_str="",
			status=RowStatus.Review,
			checked=False,
			checkbox_enabled=_checkbox_enabled(FileType.Order, RowStatus.Review),
		),
		RowModel(
			id="r10",
			file_name="PF-ARCH-01",
			file_type=FileType.Proforma,
			date_str="",
			account_str="",
			total_str="",
			status=RowStatus.Processed,
			checked=False,
			checkbox_enabled=_checkbox_enabled(FileType.Proforma, RowStatus.Processed),
		),
	]

	return AppState(
		source_path="",
		dest_path="",
		filters=FilterModel(type_filter="All", status_filter="All"),
		rows=rows,
	)
