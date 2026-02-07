import json
import os
from pathlib import Path
from typing import Any


_APP_NAME = "Auto-Invoice_V2"
_SETTINGS_FILE_NAME = "settings.json"

_KEYS = (
	"source_folder",
	"dest_folder",
	"export_folder",
)


def _settings_path() -> Path:
	appdata = os.environ.get("APPDATA")
	if appdata:
		base = Path(appdata)
	else:
		# Fallback (should be rare on Windows).
		base = Path.home() / "AppData" / "Roaming"

	return base / _APP_NAME / _SETTINGS_FILE_NAME


def load_settings() -> dict[str, Any]:
	"""Load persisted UI settings.

	Slice 1 persists ONLY three folder path strings:
	- source_folder
	- dest_folder
	- export_folder
	"""

	path = _settings_path()
	if not path.exists():
		return {}

	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except Exception:
		return {}

	if not isinstance(data, dict):
		return {}

	# Keep only known keys and coerce to strings.
	settings: dict[str, Any] = {}
	for key in _KEYS:
		value = data.get(key)
		if isinstance(value, str):
			settings[key] = value
	return settings


def save_settings(settings: dict[str, Any]) -> None:
	"""Persist UI settings to per-user AppData."""

	path = _settings_path()
	path.parent.mkdir(parents=True, exist_ok=True)

	payload: dict[str, Any] = {}
	for key in _KEYS:
		value = settings.get(key)
		if isinstance(value, str):
			payload[key] = value
		elif value is None:
			payload[key] = ""

	path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

