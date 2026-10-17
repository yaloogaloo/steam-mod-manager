"""Capability probes — skip environment-dependent tests instead of false failures."""

from __future__ import annotations

from pathlib import Path


def playwright_chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            path = str(pw.chromium.executable_path or "")
            return bool(path and Path(path).is_file())
    except Exception:  # noqa: BLE001
        return False
