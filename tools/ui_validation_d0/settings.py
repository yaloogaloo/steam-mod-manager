"""Explicit INI settings factory; not yet wired to all product aliases."""
from __future__ import annotations

from pathlib import Path
from .safety import Boundary


class SettingsFactory:
    def __init__(self, boundary: Boundary):
        self.path = boundary.target(boundary.root / "settings" / "ui-validation.ini")
        self.requested_namespaces: list[tuple[str, str]] = []

    def create(self, organization: str, application: str):
        # Retain the original type before any future alias injection. Explicit
        # filename avoids the production registry; fallback must also be off.
        from PySide6.QtCore import QSettings
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.requested_namespaces.append((organization, application))
        settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        settings.setFallbacksEnabled(False)
        return settings
