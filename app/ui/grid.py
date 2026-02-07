from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from core.app_state import AppState
from core.mutations import (
	apply_filters,
	set_status_filter,
	set_type_filter,
	toggle_all_eligible,
	toggle_row_checked,
)
from core.row_model import FileType, RowModel, RowStatus
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


_DOC_TYPE_TO_ENUM: dict[str, FileType] = {
	"Tax Invoice": FileType.TaxInvoice,
	"Order": FileType.Order,
	"Proforma": FileType.Proforma,
	"Transfer": FileType.Transfer,
	"Credit": FileType.Credit,
}


_ENUM_TO_DOC_TYPE: dict[FileType, str] = {v: k for k, v in _DOC_TYPE_TO_ENUM.items()}


_STATUS_CYCLE: list[RowStatus | str] = ["All", RowStatus.Ready, RowStatus.Review, RowStatus.Processed]


_TYPE_CYCLE: list[FileType | str] = ["All"] + [_DOC_TYPE_TO_ENUM[t] for t in DOC_TYPES]


class FilesGrid(ttk.Frame):
	def __init__(self, master: tk.Misc, state: AppState):
		super().__init__(master)
		self._state = state
		self.on_visible_count_changed = None

		self._build_ui()
		self.refresh()

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

	def refresh(self) -> None:
		for item in self.tree.get_children(""):
			self.tree.delete(item)

		for row in apply_filters(self._state):
			tags: tuple[str, ...] = ()
			if row.status == RowStatus.Review:
				tags = ("review",)
			elif row.status == RowStatus.Processed:
				tags = ("processed",)

			self.tree.insert(
				"",
				"end",
				iid=row.id,
				values=self._row_values(row),
				tags=tags,
			)

		self._refresh_header_text()
		if self.on_visible_count_changed is not None:
			try:
				self.on_visible_count_changed(self.get_visible_count())
			except TypeError:
				self.on_visible_count_changed()

	def _row_values(self, row: RowModel) -> tuple[str, str, str, str, str, str, str]:
		eligible = row.checkbox_enabled
		checkbox_text = "☑" if (eligible and row.checked) else ("☐" if eligible else "")
		doc_type = _ENUM_TO_DOC_TYPE.get(row.file_type, row.file_type.value)
		return (
			checkbox_text,
			row.file_name,
			doc_type,
			row.date_str,
			row.account_str,
			row.total_str,
			row.status.value,
		)

	def _toggle_header_checkbox(self) -> None:
		eligible_rows = [r for r in self._state.rows if r.checkbox_enabled]
		if not eligible_rows:
			return
		new_value = not all(r.checked for r in eligible_rows)
		toggle_all_eligible(self._state, new_value)
		self.refresh()

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

		row = next((r for r in self._state.rows if r.id == row_id), None)
		if row is None:
			return None
		if not row.checkbox_enabled:
			return None

		new_value = not row.checked
		toggle_row_checked(self._state, row_id, new_value)
		self.tree.item(row_id, values=self._row_values(row))
		return "break"

	def _cycle_status_filter(self) -> None:
		current = self._state.filters.status_filter
		idx = _STATUS_CYCLE.index(current)
		next_value = _STATUS_CYCLE[(idx + 1) % len(_STATUS_CYCLE)]
		set_status_filter(self._state, next_value)  # type: ignore[arg-type]
		self.refresh()

	def _cycle_type_filter(self) -> None:
		current = self._state.filters.type_filter
		idx = _TYPE_CYCLE.index(current)
		next_value = _TYPE_CYCLE[(idx + 1) % len(_TYPE_CYCLE)]
		set_type_filter(self._state, next_value)  # type: ignore[arg-type]
		self.refresh()

	def _refresh_header_text(self) -> None:
		status_text = "All" if self._state.filters.status_filter == "All" else self._state.filters.status_filter.value
		self.tree.heading("status", text=f"Status ({status_text})", command=self._cycle_status_filter)

		if self._state.filters.type_filter == "All":
			type_header = "All"
		else:
			type_str = _ENUM_TO_DOC_TYPE.get(self._state.filters.type_filter, self._state.filters.type_filter.value)
			type_header = TYPE_SHORTHAND.get(type_str, type_str)
		self.tree.heading("type", text=f"Type ({type_header})", command=self._cycle_type_filter)

		eligible_rows = [r for r in self._state.rows if r.checkbox_enabled]
		glyph = "☐"
		if eligible_rows and all(r.checked for r in eligible_rows):
			glyph = "☑"
		self.tree.heading("checked", text=glyph, command=self._toggle_header_checkbox)

