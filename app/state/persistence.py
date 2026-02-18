import json
import os
import shutil
from pathlib import Path
from typing import Any


_APP_NAME = "Auto-Invoice_V2"
_SETTINGS_FILE_NAME = "settings.json"

_HISTORY_FILE_NAME = "history_state.json"
_HISTORY_SCHEMA_VERSION = 1

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


def _history_path() -> Path:
	appdata = os.environ.get("APPDATA")
	if appdata:
		base = Path(appdata)
	else:
		# Fallback (should be rare on Windows).
		base = Path.home() / "AppData" / "Roaming"

	return base / _APP_NAME / _HISTORY_FILE_NAME


def _history_backup_path() -> Path:
	path = _history_path()
	return path.with_suffix(path.suffix + ".bak")


def _load_history_file(path: Path) -> dict[str, Any]:
	if not path.exists():
		return {}
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except Exception:
		return {}
	if not isinstance(data, dict):
		return {}
	try:
		sv = int(data.get("schema_version", 0))
	except Exception:
		sv = 0
	if sv != _HISTORY_SCHEMA_VERSION:
		return {}
	return data


def load_history_state() -> dict[str, Any]:
	"""Load persisted session history state.

	Fail-open: missing/corrupt/unreadable file returns empty state.
	"""
	main = _history_path()
	data = _load_history_file(main)
	if data:
		return data
	backup = _history_backup_path()
	return _load_history_file(backup)


def save_history_state(state: dict[str, Any]) -> None:
	"""Persist session history state to per-user AppData (atomic temp + replace)."""
	path = _history_path()
	path.parent.mkdir(parents=True, exist_ok=True)

	payload = dict(state or {})
	payload["schema_version"] = _HISTORY_SCHEMA_VERSION

	tmp = path.with_suffix(path.suffix + ".tmp")
	try:
		tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
		try:
			if path.exists():
				shutil.copy2(path, _history_backup_path())
		except Exception:
			pass
		os.replace(tmp, path)
	except Exception:
		try:
			tmp.unlink(missing_ok=True)
		except Exception:
			pass
		# Fail-open: persistence errors should not break the app.
		return


def delete_history_state() -> None:
	"""Delete persisted history state file. Best-effort."""
	path = _history_path()
	try:
		path.unlink(missing_ok=True)
	except Exception:
		pass

