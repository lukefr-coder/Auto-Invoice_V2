from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import tkinter as tk
from tkinter import ttk


DEMO_MODE = True


REVIEW_BG = "#FFF59D"
PROCESSED_FG = "#0000EE"


DOC_TYPES = [
	"Tax Invoice",
	"Order",
	"Proforma",
	"Transfer",
	"Credit",
]

STATUS_VALUES = [
	"Ready",
	"Review",
	"Processed",
]


TYPE_SHORTHAND = {
	"Tax Invoice": "Tx",
	"Order": "Or",
	"Proforma": "Pf",
	"Transfer": "Tr",
	"Credit": "Cr",
}


@dataclass
class GridRow:
	row_id: str
	file_stem: str
	doc_type: str
	date: str = ""
	account: str = ""
	total: str = ""
	status: str = "Ready"
	checked: bool = False


class FilesGrid(ttk.Frame):
	def __init__(self, master: tk.Misc):
		super().__init__(master)

		self._all_rows: list[GridRow] = []
		self._rows_by_id: dict[str, GridRow] = {}
		self.on_visible_count_changed = None

		# Filters (None = All)
		self._status_filter: str | None = None
		self._type_filter: str | None = None

		self._build_ui()

		if DEMO_MODE:
			self._inject_demo_rows()

		self.apply_filters()

	def _build_ui(self) -> None:
		style = ttk.Style(self)
		base_font = style.lookup("Treeview", "font") or style.lookup("TLabel", "font")
		style.configure("FilesGrid.Treeview.Heading", font=base_font)

		columns = (
			"checked",
			"file",
			"type",
			"date",
			"account",
			"total",
			"status",
		)

		self.rowconfigure(0, weight=1)
		self.columnconfigure(0, weight=1)

		table_container = ttk.Frame(self)
		table_container.grid(row=0, column=0, sticky="nsew")
		table_container.rowconfigure(0, weight=1)
		table_container.columnconfigure(0, weight=1)

		self.tree = ttk.Treeview(
			table_container,
			columns=columns,
			show="headings",
			selectmode="browse",
			style="FilesGrid.Treeview",
		)
		self.tree.grid(row=0, column=0, sticky="nsew")

		vsb = ttk.Scrollbar(table_container, orient="vertical", command=self.tree.yview)
		vsb.grid(row=0, column=1, sticky="ns")
		self.tree.configure(yscrollcommand=vsb.set)

		self.tree.heading("checked", text="☐", command=self._toggle_header_checkbox)
		self.tree.heading("file", text="File")
		self.tree.heading("type", text="Type (All)", command=self._cycle_type_filter)
		self.tree.heading("date", text="Date")
		self.tree.heading("account", text="Account")
		self.tree.heading("total", text="Total")
		self.tree.heading("status", text="Status (All)", command=self._cycle_status_filter)

		# Fixed column widths, no stretching.
		self.tree.column("checked", width=40, minwidth=40, stretch=False, anchor="center")
		self.tree.column("file", width=260, minwidth=260, stretch=False, anchor="w")
		self.tree.column("type", width=150, minwidth=150, stretch=False, anchor="center")
		self.tree.column("date", width=150, minwidth=150, stretch=False, anchor="center")
		self.tree.column("account", width=150, minwidth=150, stretch=False, anchor="center")
		self.tree.column("total", width=150, minwidth=150, stretch=False, anchor="center")
		self.tree.column("status", width=150, minwidth=150, stretch=True, anchor="center")

		self.tree.tag_configure("review", background=REVIEW_BG, foreground="black")
		self.tree.tag_configure("processed", foreground=PROCESSED_FG)

		# Disable separator dragging and resize cursor.
		self.tree.bind("<ButtonPress-1>", self._block_heading_separator_drag, add=True)
		self.tree.bind("<Motion>", self._suppress_resize_cursor, add=True)
		self.tree.bind("<Button-1>", self._on_mouse_down, add=True)

	def _block_heading_separator_drag(self, event: tk.Event) -> str | None:
		if self.tree.identify_region(event.x, event.y) == "separator":
			return "break"
		return None

	def _suppress_resize_cursor(self, event: tk.Event) -> None:
		# Prevent the left/right resize cursor on column separators.
		if self.tree.identify_region(event.x, event.y) == "separator":
			try:
				self.tree.configure(cursor="arrow")
			except Exception:
				pass
		else:
			try:
				self.tree.configure(cursor="")
			except Exception:
				pass

	def get_visible_count(self) -> int:
		return len(self.tree.get_children(""))

	def set_rows(self, rows: list[GridRow]) -> None:
		self._all_rows = list(rows)
		self._rows_by_id = {r.row_id: r for r in rows}
		self.apply_filters()

	def apply_filters(self) -> None:
		for item in self.tree.get_children(""):
			self.tree.delete(item)

		for row in self._all_rows:
			if self._status_filter is not None and row.status != self._status_filter:
				continue
			if self._type_filter is not None and row.doc_type != self._type_filter:
				continue

			tags: tuple[str, ...] = ()
			if row.status == "Review":
				tags = ("review",)
			elif row.status == "Processed":
				tags = ("processed",)

			self.tree.insert(
				"",
				"end",
				iid=row.row_id,
				values=self._row_values(row),
				tags=tags,
			)

		self._refresh_header_text()
		if self.on_visible_count_changed is not None:
			try:
				self.on_visible_count_changed(self.get_visible_count())
			except TypeError:
				self.on_visible_count_changed()

	def _row_values(self, row: GridRow) -> tuple[str, str, str, str, str, str, str]:
		eligible = self._checkbox_enabled(row)
		checkbox_text = "☑" if (eligible and row.checked) else ("☐" if eligible else "")
		return (
			checkbox_text,
			row.file_stem,
			row.doc_type,
			row.date,
			row.account,
			row.total,
			row.status,
		)

	def _toggle_header_checkbox(self) -> None:
		eligible_rows = [r for r in self._all_rows if self._checkbox_enabled(r)]
		if not eligible_rows:
			return
		new_value = not all(r.checked for r in eligible_rows)
		for r in eligible_rows:
			r.checked = new_value
		self.apply_filters()

	def _checkbox_enabled(self, row: GridRow) -> bool:
		if row.status == "Review":
			return False
		if row.doc_type not in ("Tax Invoice", "Proforma"):
			return False
		if row.status not in ("Ready", "Processed"):
			return False
		return True

	def _on_mouse_down(self, event: tk.Event) -> str | None:
		region = self.tree.identify("region", event.x, event.y)
		if region != "cell":
			return None

		column = self.tree.identify_column(event.x)
		row_id = self.tree.identify_row(event.y)
		if not row_id:
			return None

		# '#1' is first visible column: checked
		if column != "#1":
			return None

		row = self._rows_by_id.get(row_id)
		if row is None:
			return None

		if not self._checkbox_enabled(row):
			return None

		row.checked = not row.checked
		self.tree.item(row.row_id, values=self._row_values(row))
		return "break"

	def _cycle_status_filter(self) -> None:
		cycle = [None, "Ready", "Review", "Processed"]
		idx = cycle.index(self._status_filter)
		self._status_filter = cycle[(idx + 1) % len(cycle)]
		self.apply_filters()

	def _cycle_type_filter(self) -> None:
		cycle = [None] + DOC_TYPES
		idx = cycle.index(self._type_filter)
		self._type_filter = cycle[(idx + 1) % len(cycle)]
		self.apply_filters()

	def _refresh_header_text(self) -> None:
		status_text = "All" if self._status_filter is None else self._status_filter
		self.tree.heading("status", text=f"Status ({status_text})", command=self._cycle_status_filter)

		if self._type_filter is None:
			type_header = "All"
		else:
			type_header = TYPE_SHORTHAND.get(self._type_filter, self._type_filter)
		self.tree.heading("type", text=f"Type ({type_header})", command=self._cycle_type_filter)

		eligible_rows = [r for r in self._all_rows if self._checkbox_enabled(r)]
		glyph = "☐"
		if eligible_rows and all(r.checked for r in eligible_rows):
			glyph = "☑"
		self.tree.heading("checked", text=glyph, command=self._toggle_header_checkbox)

	@staticmethod
	def _fmt_demo_date(iso_yyyy_mm_dd: str) -> str:
		try:
			y, m, d = (int(p) for p in iso_yyyy_mm_dd.split("-"))
			return date(y, m, d).strftime("%d/%m/%y")
		except Exception:
			return iso_yyyy_mm_dd

	def _inject_demo_rows(self) -> None:
		demo = [
			GridRow(
				"r1",
				"INV-0001",
				"Tax Invoice",
				date=self._fmt_demo_date("2026-02-01"),
				account="",
				total="",
				status="Ready",
			),
			GridRow("r2", "INV-0002", "Tax Invoice", date="", account="", total="", status="Processed", checked=True),
			GridRow("r3", "ORDER-914", "Order", date="", account="", total="", status="Ready"),
			GridRow("r4", "PF-774", "Proforma", date="", account="", total="", status="Ready"),
			GridRow("r5", "TRANSFER-22", "Transfer", date="", account="", total="", status="Ready"),
			GridRow("r6", "CREDIT-51", "Credit", date="", account="", total="", status="Ready"),
			GridRow("r7", "INV-REVIEW-3", "Tax Invoice", date="", account="", total="", status="Review"),
			GridRow("r8", "PF-REVIEW-9", "Proforma", date="", account="", total="", status="Review"),
			GridRow("r9", "ORDER-REVIEW", "Order", date="", account="", total="", status="Review"),
			GridRow("r10", "PF-ARCH-01", "Proforma", date="", account="", total="", status="Processed"),
		]
		self.set_rows(demo)

