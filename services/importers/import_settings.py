"""Shared QSettings helpers for Mod Import file dialogs."""

from __future__ import annotations

from pathlib import Path

_SETTINGS_ORG = "SteamModManager"
_SETTINGS_APP = "WorkshopLibrary"
SETTING_LAST_IMPORT_DIRECTORY = "last_import_directory"


def _settings():
    from PySide6.QtCore import QSettings

    return QSettings(_SETTINGS_ORG, _SETTINGS_APP)


def get_last_import_directory(*, fallback: str | Path | None = None) -> str:
    """
    Return the last directory used by an Import file picker.

    Falls back to *fallback*, then the user home directory.
    """
    try:
        raw = str(_settings().value(SETTING_LAST_IMPORT_DIRECTORY, "") or "").strip()
    except Exception:  # noqa: BLE001 — Qt optional in some test contexts
        raw = ""
    if raw:
        path = Path(raw)
        if path.is_dir():
            return str(path)
        parent = path.parent
        if parent.is_dir():
            return str(parent)
    if fallback is not None:
        fb = Path(str(fallback)).expanduser()
        if fb.is_dir():
            return str(fb)
        if fb.parent.is_dir():
            return str(fb.parent)
    return str(Path.home())


def set_last_import_directory(path: str | Path | None) -> None:
    """Remember *path* (file or directory) as the next Import dialog start folder."""
    if path is None:
        return
    text = str(path).strip()
    if not text:
        return
    target = Path(text).expanduser()
    directory = target if target.is_dir() else target.parent
    if not directory.is_dir():
        return
    try:
        _settings().setValue(SETTING_LAST_IMPORT_DIRECTORY, str(directory))
    except Exception:  # noqa: BLE001
        return


def resolve_import_start_directory(*candidates: str | Path | None) -> str:
    """
    Pick a start directory for an Import QFileDialog.

    Prefer the first non-empty existing candidate (file→parent or dir), else
    ``last_import_directory``.
    """
    for raw in candidates:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if path.is_dir():
            return str(path)
        if path.parent.is_dir():
            return str(path.parent)
    return get_last_import_directory()
