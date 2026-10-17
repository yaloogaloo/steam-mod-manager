"""Governance: LIVE/Backup leftover ``assets/`` trees must not exist."""

from __future__ import annotations

from pathlib import Path

import core.paths as paths
from core.paths import project_root


def _scan_live_asset_dirs(root: Path) -> list[str]:
    found: list[str] = []
    if not root.is_dir():
        return found
    try:
        games = list(root.iterdir())
    except OSError:
        return found
    for game in games:
        if not game.is_dir() or game.name.startswith("."):
            continue
        try:
            children = list(game.iterdir())
        except OSError:
            continue
        for folder in children:
            if not folder.is_dir():
                continue
            info = folder / ".info"
            if not info.is_dir():
                continue
            for cand in (info / "assets", info / "offline" / "assets"):
                if cand.is_dir():
                    found.append(str(cand))
    return found


def _scan_backup_asset_dirs(root: Path) -> list[str]:
    found: list[str] = []
    if not root.is_dir():
        return found
    try:
        buckets = list(root.iterdir())
    except OSError:
        return found
    for bucket in buckets:
        if not bucket.is_dir():
            continue
        cand = bucket / "offline" / "assets"
        if cand.is_dir():
            found.append(str(cand))
    return found


def test_no_live_asset_leftover() -> None:
    leftover = _scan_live_asset_dirs(paths.default_mod_library())
    assert leftover == []

    prod = project_root() / "mod"
    if prod.is_dir() and prod.resolve() != paths.default_mod_library().resolve():
        leftover_prod = _scan_live_asset_dirs(prod)
        assert leftover_prod == [], leftover_prod


def test_no_backup_offline_asset_leftover() -> None:
    leftover = _scan_backup_asset_dirs(paths.data_dir() / "mod_backup")
    assert leftover == []

    prod = project_root() / "data" / "mod_backup"
    if prod.is_dir() and prod.resolve() != (paths.data_dir() / "mod_backup").resolve():
        leftover_prod = _scan_backup_asset_dirs(prod)
        assert leftover_prod == [], leftover_prod
