"""Library maintenance — scan-only issue reports (Phase 6-A).

Does **not** delete anything. Cleanup of obvious test pollution belongs in
``scripts/cleanup_test_library.py`` (dev-only, dry-run by default).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.paths import data_dir, default_mod_library
from core.sanitize import sanitize_folder_name
from services.library_status import GAME_STATUS_MISSING_FOLDER, compute_game_status
from services.metadata_backup import BACKUP_DIR_NAME

# Exact / prefix heuristics for test pollution (report only).
_TEST_EXACT = frozenset(
    {
        "g",
        "game",
        "gamea",
        "gameb",
        "gamex",
        "gamey",
        "gamez",
        "testgame",
    }
)
_TEST_PREFIX_RE = re.compile(r"^test[_-]?", re.IGNORECASE)
_TEST_SUBSTRING_RE = re.compile(r"test_|sidecar|preserv", re.IGNORECASE)


@dataclass
class LibraryIssueReport:
    orphan_games: list[str] = field(default_factory=list)
    orphan_mods: list[str] = field(default_factory=list)
    orphan_backup: list[str] = field(default_factory=list)
    test_like_entries: list[str] = field(default_factory=list)
    missing_folder_games: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "orphan_games": list(self.orphan_games),
            "orphan_mods": list(self.orphan_mods),
            "orphan_backup": list(self.orphan_backup),
            "test_like_entries": list(self.test_like_entries),
            "missing_folder_games": list(self.missing_folder_games),
            "notes": list(self.notes),
        }


def is_test_like_name(name: str) -> bool:
    key = str(name or "").strip()
    if not key:
        return False
    low = key.casefold()
    if low in _TEST_EXACT:
        return True
    if _TEST_PREFIX_RE.match(key):
        return True
    if _TEST_SUBSTRING_RE.search(key):
        return True
    return False


def scan_library_issues(
    library_root: str | Path | None = None,
    *,
    data_root: str | Path | None = None,
) -> LibraryIssueReport:
    """
    Scan filesystem + SQLite + backup for governance issues.

    Read-only: never deletes rows, folders, or backups.
    """
    from core.db_manager import get_db

    root = Path(library_root) if library_root else Path(default_mod_library())
    data = Path(data_root) if data_root else Path(data_dir())
    report = LibraryIssueReport()
    db = get_db()

    disk_games: set[str] = set()
    if root.is_dir():
        try:
            disk_games = {
                p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
            }
        except OSError as exc:
            report.notes.append(f"disk scan failed: {exc}")

    # Collect mod → game folder from last_known_path / managed rows
    mod_game: dict[str, str] = {}
    mods_with_backup_meta: set[str] = set()
    try:
        for row in db.iter_mod_backup_rows():
            mid = str(row.get("mod_id") or "").strip()
            if not mid:
                continue
            lkp = str(row.get("last_known_path") or "").strip()
            if lkp:
                parent = Path(lkp).parent.name
                if parent:
                    mod_game[mid] = parent
            if str(row.get("backup_metadata_json") or "").strip():
                mods_with_backup_meta.add(mid)
    except Exception as exc:  # noqa: BLE001
        report.notes.append(f"iter_mod_backup_rows failed: {exc}")

    # Also walk all mods for orphans (no path / no backup)
    try:
        with db._lock:
            all_mods = db._conn.execute(
                """
                SELECT mod_id, last_known_path, backup_metadata_json, folder_present
                FROM mods
                """
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        all_mods = []
        report.notes.append(f"mods scan failed: {exc}")

    for row in all_mods:
        mid = str(row["mod_id"])
        lkp = str(row["last_known_path"] or "").strip()
        bmeta = str(row["backup_metadata_json"] or "").strip()
        if lkp and mid not in mod_game:
            parent = Path(lkp).parent.name
            if parent:
                mod_game[mid] = parent
        if bmeta:
            mods_with_backup_meta.add(mid)
        folder_ok = bool(lkp and Path(lkp).is_dir())
        if not folder_ok and not bmeta and not lkp:
            report.orphan_mods.append(mid)

    backup_base = data / BACKUP_DIR_NAME
    backup_ids: set[str] = set()
    if backup_base.is_dir():
        try:
            for child in backup_base.iterdir():
                if child.is_dir() and child.name.isdigit():
                    backup_ids.add(child.name)
        except OSError as exc:
            report.notes.append(f"backup scan failed: {exc}")

    known_mod_ids = {str(r["mod_id"]) for r in all_mods}
    for bid in sorted(backup_ids, key=lambda x: int(x) if x.isdigit() else x):
        if bid not in known_mod_ids:
            report.orphan_backup.append(bid)

    # games table orphans + missing folders
    game_folders_from_mods = set(mod_game.values()) | disk_games
    try:
        db_games = [g for g in db.list_games() if int(g.app_id or 0) > 0]
    except Exception as exc:  # noqa: BLE001
        db_games = []
        report.notes.append(f"list_games failed: {exc}")

    for game in db_games:
        display = (game.display_name or game.name or "").strip() or f"App_{game.app_id}"
        folder = (game.folder_name or "").strip() or sanitize_folder_name(
            display, fallback=f"App_{game.app_id}"
        )
        has_mods = folder in game_folders_from_mods or any(
            v == folder for v in mod_game.values()
        )
        on_disk = folder in disk_games or (root / folder).is_dir()
        has_backup = any(
            mod_game.get(mid) == folder and mid in (backup_ids | mods_with_backup_meta)
            for mid in mod_game
        ) or any(
            mid in backup_ids and mod_game.get(mid) == folder for mid in backup_ids
        )
        # Simpler: mods pointing at this folder with backup or disk
        linked = [
            mid
            for mid, gname in mod_game.items()
            if gname == folder
        ]
        linked_backed = [
            mid
            for mid in linked
            if mid in backup_ids or mid in mods_with_backup_meta
        ]
        if not on_disk and not linked:
            report.orphan_games.append(f"{folder} (app_id={game.app_id})")
        status = compute_game_status(root, folder)
        if status == GAME_STATUS_MISSING_FOLDER and (linked or linked_backed):
            if folder not in report.missing_folder_games:
                report.missing_folder_games.append(folder)

    # Missing folder games from mod paths even without games-table row
    for folder in sorted(game_folders_from_mods):
        if not folder:
            continue
        if compute_game_status(root, folder) == GAME_STATUS_MISSING_FOLDER:
            if folder not in report.missing_folder_games:
                report.missing_folder_games.append(folder)

    # Test-like names across disk / mods / games
    candidates: set[str] = set(disk_games) | set(mod_game.values())
    for game in db_games:
        display = (game.display_name or game.name or "").strip()
        folder = (game.folder_name or "").strip() or sanitize_folder_name(
            display, fallback=f"App_{game.app_id}"
        )
        candidates.add(folder)
        if display:
            candidates.add(display)
    for name in sorted(candidates, key=str.casefold):
        if is_test_like_name(name) and name not in report.test_like_entries:
            report.test_like_entries.append(name)

    report.orphan_games = sorted(set(report.orphan_games), key=str.casefold)
    report.orphan_mods = sorted(set(report.orphan_mods), key=lambda x: int(x) if x.isdigit() else x)
    report.orphan_backup = sorted(
        set(report.orphan_backup), key=lambda x: int(x) if x.isdigit() else x
    )
    report.missing_folder_games = sorted(set(report.missing_folder_games), key=str.casefold)
    report.test_like_entries = sorted(set(report.test_like_entries), key=str.casefold)
    return report
