from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RowStatus(str, Enum):
	Ready = "Ready"
	Review = "Review"
	Processed = "Processed"


class FileType(str, Enum):
	TaxInvoice = "Tax Invoice"
	Order = "Order"
	Proforma = "Proforma"
	Transfer = "Transfer"
	Credit = "Credit"
	Unknown = "Unknown"


@dataclass
class RowModel:
	id: str
	file_name: str
	file_type: FileType
	date_str: str
	account_str: str
	total_str: str
	status: RowStatus
	checked: bool
	checkbox_enabled: bool
	source_path: str = ""
