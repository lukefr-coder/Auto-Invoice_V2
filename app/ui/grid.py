from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk
from tkinter import ttk


DEMO_MODE = True


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

		# Filters (None = All)
		self._status_filter: str | None = None
		self._type_filter: str | None = None

		self._build_ui()

		if DEMO_MODE:
			self._inject_demo_rows()

		self.apply_filters()

	def _build_ui(self) -> None:
		columns = (
			"checked",
			"file",
			"type",
			"date",
			"account",
			"total",
			"status",
		)

		self.tree = ttk.Treeview(self, columns=columns, show="headings", selectmode="browse")
		self.tree.pack(side="left", fill="both", expand=True)

		vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
		vsb.pack(side="right", fill="y")
		self.tree.configure(yscrollcommand=vsb.set)

		self.tree.heading("checked", text="✓")
		self.tree.heading("file", text="File")
		self.tree.heading("type", text="Type (All)", command=self._cycle_type_filter)
		self.tree.heading("date", text="Date")
		self.tree.heading("account", text="Account")
		self.tree.heading("total", text="Total")
		self.tree.heading("status", text="Status (All)", command=self._cycle_status_filter)

		self.tree.column("checked", width=45, minwidth=45, stretch=False, anchor="center")
		self.tree.column("file", width=240, minwidth=120, stretch=True, anchor="w")
		self.tree.column("type", width=130, minwidth=110, stretch=False, anchor="w")
		self.tree.column("date", width=100, minwidth=70, stretch=False, anchor="w")
		self.tree.column("account", width=160, minwidth=100, stretch=True, anchor="w")
		self.tree.column("total", width=90, minwidth=70, stretch=False, anchor="e")
		self.tree.column("status", width=110, minwidth=90, stretch=False, anchor="w")

		self.tree.tag_configure("review", background="yellow")
		self.tree.tag_configure("processed", foreground="blue")

		self.tree.bind("<Button-1>", self._on_mouse_down, add=True)

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

	def _inject_demo_rows(self) -> None:
		demo = [
			GridRow("r1", "INV-0001", "Tax Invoice", date="2026-02-01", account="", total="", status="Ready"),
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

