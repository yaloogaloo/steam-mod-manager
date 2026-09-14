#!/usr/bin/env python3
"""Wrapper: one-shot Backup asset migrate lives under tools/archive/legacy_asset_tools/."""

from __future__ import annotations

import runpy
from pathlib import Path

_TARGET = (
    Path(__file__).resolve().parent
    / "archive"
    / "legacy_asset_tools"
    / "migrate_backup_assets_to_store.py"
)

if __name__ == "__main__":
    runpy.run_path(str(_TARGET), run_name="__main__")
