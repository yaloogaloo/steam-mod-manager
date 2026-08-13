#!/usr/bin/env python3
"""Dev-only cleanup for obvious test pollution in the Game Library.

Default mode is dry-run. Never integrated into the product UI.

Examples::

    python scripts/cleanup_test_library.py
    python scripts/cleanup_test_library.py --apply
    python scripts/cleanup_test_library.py --library E:/path/to/mod --apply

Only touches names / paths that match clear test heuristics
(``test_*``, ``GameA``, ``pytest-of-``, orphan backups under ``GameX`` / ``TestGame``, …).
Type-B production data and Type-C ambiguous rows are never selected.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# Allow running from repo root without install
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.paths import data_dir, default_mod_library  # noqa: E402
from services.library_maintenance import (  # noqa: E402
    is_test_like_name,
    scan_library_issues,
)
from services.metadata_backup import BACKUP_DIR_NAME  # noqa: E402


def _is_pytest_temp_path(path: str) -> bool:
    low = str(path or "").replace("/", "\\").casefold()
    return "pytest-of-" in low or "\\pytest\\" in low


def _backup_game_name(backup_dir: Path) -> str:
    meta = backup_dir / "metadata.json"
    if not meta.is_file():
        return ""
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("game_name") or "").strip()


def _collect_targets(
    library_root: Path,
    data_root: Path,
) -> dict[str, list[str]]:
    report = scan_library_issues(library_root, data_root=data_root)
    disk_dirs: list[str] = []
    if library_root.is_dir():
        for child in library_root.iterdir():
            if child.is_dir() and is_test_like_name(child.name):
                disk_dirs.append(str(child))

    from core.db_manager import DatabaseManager, get_db

    DatabaseManager.instance()
    db = get_db()
    mod_ids: list[str] = []
    backup_dirs: list[str] = []
    try:
        for row in db.iter_mod_backup_rows():
            mid = str(row.get("mod_id") or "").strip()
            lkp = str(row.get("last_known_path") or "").strip()
            parent = Path(lkp).parent.name if lkp else ""
            clear_a = False
            if mid and is_test_like_name(parent):
                clear_a = True
            elif mid and _is_pytest_temp_path(lkp):
                clear_a = True
            if clear_a:
                mod_ids.append(mid)
                bpath = data_root / BACKUP_DIR_NAME / mid
                if bpath.is_dir():
                    backup_dirs.append(str(bpath))
        # Empty-path stubs (no backup row) left by tests on the production singleton.
        with db._lock:
            stub_rows = db._conn.execute(
                """
                SELECT mod_id, title, last_known_path
                FROM mods
                WHERE TRIM(COALESCE(last_known_path, '')) = ''
                """
            ).fetchall()
        for row in stub_rows:
            mid = str(row["mod_id"] or "").strip()
            title = str(row["title"] or "").strip()
            if mid and (
                is_test_like_name(title) or _is_synthetic_orphan_title(title)
            ):
                mod_ids.append(mid)
                bpath = data_root / BACKUP_DIR_NAME / mid
                if bpath.is_dir():
                    backup_dirs.append(str(bpath))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] mod scan failed: {exc}")

    # Orphan backup dirs with test game names and no / stale DB row.
    backup_root = data_root / BACKUP_DIR_NAME
    if backup_root.is_dir():
        for child in backup_root.iterdir():
            if not child.is_dir():
                continue
            game = _backup_game_name(child)
            if is_test_like_name(game):
                backup_dirs.append(str(child))
                mid = child.name
                if mid.isdigit():
                    mod_ids.append(mid)

    game_rows: list[str] = []
    try:
        for game in db.list_games():
            if int(game.app_id or 0) <= 0:
                continue
            name = (game.display_name or game.name or "").strip()
            folder = (game.folder_name or "").strip() or name
            if is_test_like_name(folder) or is_test_like_name(name):
                game_rows.append(
                    f"app_id={game.app_id} name={name!r} folder={folder!r}"
                )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] games scan failed: {exc}")

    return {
        "disk_dirs": sorted(set(disk_dirs)),
        "backup_dirs": sorted(set(backup_dirs)),
        "mod_ids": sorted(set(mod_ids), key=lambda x: int(x) if x.isdigit() else x),
        "game_rows": sorted(set(game_rows)),
        "test_like_reported": list(report.test_like_entries),
    }


def _is_synthetic_orphan_title(title: str) -> bool:
    """Titles that only appear in unit fixtures (never real Workshop names)."""
    t = str(title or "").strip()
    if not t:
        return False
    low = t.casefold()
    exact = {
        "cover mod",
        "cool mod",
        "mod",
        "legacy",
        "x",
        "t",
        "single",
        "multi",
        "remap",
        "similar",
        "nexusmulti",
        "nexusctx",
        "nexusbadge",
        "mainunlock",
        "detailopen",
        "missing",
        "moda",
        "test mod",
        "folderstay",
        "biggerharbour",
        "sectionmod",
        "okmod",
        "failmod",
        "persistfail",
        "steamtitle",
        "keep",
        "gone",
        "localmod",
        "fastmod",
        "a",
        "steam2",
        "steamonly",
        "autocover",
        "mod a",
        "mod b",
        "mod c",
        "bravo",
        "deployme",
    }
    if low in exact:
        return True
    if low.startswith("refresh mod ") or low.startswith("menu mod "):
        return True
    return False


def _confirm(prompt: str) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--library",
        type=Path,
        default=None,
        help="Mod library root (default: project mod/)",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=None,
        help="Data root for backups / DB helpers (default: project data/)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete matched test artefacts (default: dry-run)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation when --apply is set",
    )
    args = parser.parse_args(argv)

    library_root = Path(args.library) if args.library else Path(default_mod_library())
    data_root = Path(args.data) if args.data else Path(data_dir())

    targets = _collect_targets(library_root, data_root)
    print(
        "=== cleanup_test_library (dry-run)"
        if not args.apply
        else "=== cleanup_test_library (APPLY)"
    )
    print(f"library: {library_root}")
    print(f"data:    {data_root}")
    print()
    print("test_like (scan report):", targets["test_like_reported"] or "(none)")
    print("disk_dirs to remove:")
    for p in targets["disk_dirs"] or ["(none)"]:
        print(f"  - {p}")
    print("backup_dirs to remove:")
    for p in targets["backup_dirs"] or ["(none)"]:
        print(f"  - {p}")
    print("mod_ids to delete from SQLite:")
    for mid in targets["mod_ids"] or ["(none)"]:
        print(f"  - {mid}")
    print("games table rows to delete:")
    for row in targets["game_rows"] or ["(none)"]:
        print(f"  - {row}")

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to execute.")
        return 0

    if not any(
        targets[k] for k in ("disk_dirs", "backup_dirs", "mod_ids", "game_rows")
    ):
        print("\nNothing to delete.")
        return 0

    if not args.yes and not _confirm(
        "\nType yes to permanently delete the items above: "
    ):
        print("Aborted.")
        return 1

    from core.db_manager import get_db

    db = get_db()
    for path in targets["disk_dirs"]:
        shutil.rmtree(path, ignore_errors=True)
        print(f"removed disk {path}")
    for path in targets["backup_dirs"]:
        shutil.rmtree(path, ignore_errors=True)
        print(f"removed backup {path}")
    for mid in targets["mod_ids"]:
        try:
            if hasattr(db, "delete_mod_record"):
                db.delete_mod_record(mid)
            else:
                with db._lock:
                    db._conn.execute("DELETE FROM mods WHERE mod_id = ?", (int(mid),))
                    db._conn.commit()
            print(f"deleted mod_id {mid}")
        except Exception as exc:  # noqa: BLE001
            print(f"failed mod_id {mid}: {exc}")
    for row in targets["game_rows"]:
        try:
            app_id = int(row.split("app_id=", 1)[1].split()[0])
            with db._lock:
                db._conn.execute("DELETE FROM games WHERE app_id = ?", (app_id,))
                db._conn.commit()
            print(f"deleted game {app_id}")
        except Exception as exc:  # noqa: BLE001
            print(f"failed game row {row}: {exc}")

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
