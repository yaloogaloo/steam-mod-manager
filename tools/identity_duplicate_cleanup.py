#!/usr/bin/env python3
"""Identity post-rebuild duplicate cleanup — quarantine pollution purge.

One-shot tool (not production runtime).

After Identity Full Rebuild, duplicate same-game workspace folders were moved
into ``tools/_audit_out/rebuild_quarantine_*``. This script permanently deletes
those pollution directories and verifies the live library remains clean.

Hard rules
----------
- Never mutate the rebuilt DB (2401 Mods)
- Never change live ``internal_id`` / ``workspace_id``
- Never restore old ``mod_id`` / ``900000000000…``
- Never create new entities

Usage::

    python tools/identity_duplicate_cleanup.py --dry-run
    python tools/identity_duplicate_cleanup.py --apply --confirm
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "tools" / "_audit_out"
DEFAULT_LIBRARY = ROOT / "mod"
DEFAULT_DB = ROOT / "data" / "mod_manager.db"
DEFAULT_REPORT = OUT_DIR / "identity_duplicate_cleanup_report.json"
DEFAULT_PREVIEW = OUT_DIR / "rebuild_preview.json"

INFO_DIR = ".info"
METADATA = "metadata.json"
LEGACY_INFO_DIR = "info"
LEGACY_METADATA = "mod.json"
POLLUTION_ID_RE = re.compile(r"^900000000000\d+$")
POLLUTION_DIR_RE = re.compile(r"^(.+)_900000000000\d*$")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _read_info(folder: Path) -> dict[str, Any] | None:
    for info_name, meta_name in (
        (INFO_DIR, METADATA),
        (LEGACY_INFO_DIR, LEGACY_METADATA),
        (INFO_DIR, LEGACY_METADATA),
        (LEGACY_INFO_DIR, METADATA),
    ):
        meta = folder / info_name / meta_name
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"_read_error": True}
        return data if isinstance(data, dict) else {"_read_error": True}
    return None


def _iter_quarantine_mod_dirs(audit_out: Path) -> list[Path]:
    out: list[Path] = []
    for qroot in sorted(audit_out.glob("rebuild_quarantine_*")):
        if not qroot.is_dir():
            continue
        for game_dir in sorted(qroot.iterdir()):
            if not game_dir.is_dir() or game_dir.name.startswith("."):
                continue
            for child in sorted(game_dir.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    out.append(child)
    return out


def _iter_live_mod_dirs(library: Path) -> list[Path]:
    if not library.is_dir():
        return []
    out: list[Path] = []
    for game_dir in sorted(library.iterdir()):
        if not game_dir.is_dir() or game_dir.name.startswith(".") or game_dir.name.startswith("_"):
            continue
        for child in sorted(game_dir.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                out.append(child)
    return out


def _load_preview_conflict_groups(preview_path: Path) -> list[dict[str, Any]]:
    if not preview_path.is_file():
        return []
    try:
        data = json.loads(preview_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    items = list(data.get("items") or [])
    groups: dict[tuple[int, str], dict[str, Any]] = {}
    for item in items:
        app_id = int(item.get("app_id") or 0)
        ws = _text(item.get("workspace_id"))
        if app_id <= 0 or not ws:
            continue
        if not (
            item.get("action") == "DELETE_CANDIDATE"
            or (item.get("action") == "REBUILD" and item.get("duplicate_workspace"))
        ):
            continue
        key = (app_id, ws)
        slot = groups.setdefault(
            key,
            {
                "app_id": app_id,
                "workspace_id": ws,
                "game": _text(item.get("game")),
                "kept": [],
                "deleted_candidates": [],
            },
        )
        path = _text(item.get("path"))
        if item.get("action") == "REBUILD":
            if path and path not in slot["kept"]:
                slot["kept"].append(path)
        else:
            if path and path not in slot["deleted_candidates"]:
                slot["deleted_candidates"].append(path)
    return [groups[k] for k in sorted(groups.keys(), key=lambda x: (x[0], x[1]))]


def _quarantine_entry(folder: Path) -> dict[str, Any]:
    info = _read_info(folder)
    try:
        app_id = int((info or {}).get("app_id") or 0)
    except (TypeError, ValueError):
        app_id = 0
    from services.mod_identity import read_entity_key

    return {
        "path": str(folder.resolve()),
        "quarantine_root": str(folder.parents[1].resolve())
        if len(folder.parts) >= 2
        else str(folder.parent.resolve()),
        "game": folder.parent.name,
        "folder": folder.name,
        "app_id": app_id,
        "workspace_id": _text((info or {}).get("workspace_id")),
        "title": _text((info or {}).get("title") or (info or {}).get("display_name")),
        # Report: filesystem binding (entity_key / legacy sidecar key).
        "internal_id": read_entity_key(info or {}),
        "pollution_dirname": bool(POLLUTION_DIR_RE.match(folder.name)),
    }


def plan_cleanup(
    *,
    library: Path,
    audit_out: Path,
    preview_path: Path,
) -> dict[str, Any]:
    quarantine_dirs = _iter_quarantine_mod_dirs(audit_out)
    entries = [_quarantine_entry(p) for p in quarantine_dirs]
    conflict_groups = _load_preview_conflict_groups(preview_path)

    # Quarantine folders are already the non-keepers from rebuild.
    # Keep list = live paths from preview conflict groups (still under mod/).
    keep_paths: list[str] = []
    for group in conflict_groups:
        for path in group.get("kept") or []:
            if path not in keep_paths and Path(path).is_dir():
                keep_paths.append(path)

    delete_paths = [e["path"] for e in entries]

    return {
        "generated_at": _now(),
        "library_root": str(library.resolve()),
        "audit_out": str(audit_out.resolve()),
        "preview_ref": str(preview_path.resolve()) if preview_path.is_file() else "",
        "workspace_conflict_groups": conflict_groups,
        "kept_directories": keep_paths,
        "delete_directories": delete_paths,
        "delete_entries": entries,
        "counts": {
            "conflict_groups": len(conflict_groups),
            "kept": len(keep_paths),
            "to_delete": len(delete_paths),
            "quarantine_roots": len(list(audit_out.glob("rebuild_quarantine_*"))),
        },
    }


def apply_deletes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    deleted: list[dict[str, Any]] = []
    for entry in plan.get("delete_entries") or []:
        path = Path(_text(entry.get("path")))
        if not path.exists():
            deleted.append({**entry, "status": "already_gone"})
            continue
        shutil.rmtree(path, ignore_errors=False)
        deleted.append({**entry, "status": "deleted"})

    # Remove empty game dirs and quarantine roots.
    for qroot in sorted(OUT_DIR.glob("rebuild_quarantine_*")):
        if not qroot.is_dir():
            continue
        for game_dir in list(qroot.iterdir()):
            if game_dir.is_dir():
                try:
                    next(game_dir.iterdir())
                except StopIteration:
                    game_dir.rmdir()
        try:
            next(qroot.iterdir())
        except StopIteration:
            qroot.rmdir()
        except OSError:
            # Non-empty or locked — leave; verify will flag residue.
            pass
    return deleted


def _game_name_to_app_id(db_path: Path) -> dict[str, int]:
    if not db_path.is_file():
        return {}
    con = sqlite3.connect(str(db_path))
    try:
        return {
            _text(name): int(app_id)
            for app_id, name in con.execute("SELECT app_id, name FROM games")
            if int(app_id or 0) > 0 and _text(name)
        }
    finally:
        con.close()


def verify_cleanup(*, library: Path, db_path: Path, audit_out: Path) -> dict[str, Any]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r)
            for r in con.execute(
                """
                SELECT mod_id, internal_id, workspace_id, app_id, last_known_path
                FROM mods
                ORDER BY mod_id
                """
            )
        ]
    finally:
        con.close()

    name_to_app = _game_name_to_app_id(db_path)
    db_count = len(rows)
    live_dirs = _iter_live_mod_dirs(library)
    live_count = len(live_dirs)

    # Same-app workspace uniqueness on disk (.info).
    by_key: dict[tuple[int, str], list[str]] = defaultdict(list)
    info_mismatches: list[dict[str, Any]] = []
    pollution_hits: list[str] = []

    db_by_path = {
        _text(r.get("last_known_path")).lower().replace("/", "\\"): r for r in rows
    }
    db_by_iid = {_text(r.get("internal_id")): r for r in rows if _text(r.get("internal_id"))}

    for folder in live_dirs:
        info = _read_info(folder)
        try:
            app_id = int((info or {}).get("app_id") or 0)
        except (TypeError, ValueError):
            app_id = 0
        # Prefer DB app_id when path is bound; else games.name for game folder.
        key_path = str(folder.resolve()).lower().replace("/", "\\")
        row = db_by_path.get(key_path)
        if row is not None:
            app_id = int(row.get("app_id") or app_id or 0)
        if app_id <= 0:
            app_id = int(name_to_app.get(folder.parent.name, 0) or 0)
        ws = _text((info or {}).get("workspace_id"))
        if row is not None and _text(row.get("workspace_id")):
            ws = _text(row.get("workspace_id"))
        if app_id > 0 and ws:
            by_key[(app_id, ws)].append(str(folder.resolve()))

        from services.mod_identity import read_entity_key

        info_iid = read_entity_key(info or {})
        # Identity pollution only — folder-name suffixes are cosmetic, not IDs.
        if POLLUTION_ID_RE.match(info_iid):
            pollution_hits.append(f"info:{folder}:{info_iid}")
        if row is not None:
            db_iid = _text(row.get("internal_id"))
            if info_iid != db_iid:
                info_mismatches.append(
                    {
                        "path": str(folder),
                        "info_internal_id": info_iid,
                        "db_internal_id": db_iid,
                    }
                )
        elif info_iid and info_iid not in db_by_iid:
            info_mismatches.append(
                {
                    "path": str(folder),
                    "info_internal_id": info_iid,
                    "db_internal_id": "",
                    "reason": "info_not_in_db",
                }
            )

    for row in rows:
        if POLLUTION_ID_RE.match(_text(row.get("mod_id"))) or POLLUTION_ID_RE.match(
            _text(row.get("internal_id"))
        ):
            pollution_hits.append(f"db:{row.get('mod_id')}:{row.get('internal_id')}")

    same_game_dups = [
        {"app_id": app, "workspace_id": ws, "paths": paths}
        for (app, ws), paths in sorted(by_key.items())
        if len(paths) > 1
    ]

    quarantine_residue = [
        str(p.resolve())
        for p in sorted(audit_out.glob("rebuild_quarantine_*"))
        if p.exists()
    ]
    # Also flag any remaining mod dirs inside quarantine roots.
    residue_mods = [str(p) for p in _iter_quarantine_mod_dirs(audit_out)]

    checks = {
        "same_app_workspace_unique": len(same_game_dups) == 0,
        "db_mod_count_2401": db_count == 2401,
        "disk_valid_mod_count_2401": live_count == 2401,
        "no_quarantine_residue": len(quarantine_residue) == 0 and len(residue_mods) == 0,
        "no_900000000000_ids": len(pollution_hits) == 0,
        "info_internal_id_matches_db": len(info_mismatches) == 0,
    }
    return {
        "generated_at": _now(),
        "checks": checks,
        "all_passed": all(checks.values()),
        "db_mod_count": db_count,
        "disk_mod_count": live_count,
        "same_game_workspace_duplicates": same_game_dups,
        "quarantine_residue": quarantine_residue,
        "quarantine_residue_mods": residue_mods,
        "pollution_hits": pollution_hits[:50],
        "info_mismatches": info_mismatches[:50],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--audit-out", type=Path, default=OUT_DIR)
    parser.add_argument("--preview", type=Path, default=DEFAULT_PREVIEW)
    parser.add_argument("--out", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--confirm", action="store_true", default=False)
    args = parser.parse_args()

    plan = plan_cleanup(
        library=args.library,
        audit_out=args.audit_out,
        preview_path=args.preview,
    )

    dry_run = args.dry_run or not (args.apply and args.confirm)
    deleted: list[dict[str, Any]] = []
    if not dry_run:
        deleted = apply_deletes(plan)

    verify = verify_cleanup(
        library=args.library, db_path=args.db, audit_out=args.audit_out
    )

    report = {
        "generated_at": _now(),
        "tool": "identity_duplicate_cleanup",
        "dry_run": dry_run,
        "workspace_conflict_groups": plan["workspace_conflict_groups"],
        "kept_directories": plan["kept_directories"],
        "deleted_directories": [d.get("path") for d in deleted]
        if deleted
        else plan["delete_directories"],
        "deleted_entries": deleted if deleted else plan["delete_entries"],
        "delete_count": len(deleted) if deleted else plan["counts"]["to_delete"],
        "counts": {
            **plan["counts"],
            "deleted": len([d for d in deleted if d.get("status") == "deleted"]),
        },
        "verify": verify,
        "note": (
            "dry-run only; pass --apply --confirm to permanently delete quarantine duplicates"
            if dry_run
            else "quarantine duplicates permanently deleted; DB untouched"
        ),
    }
    out = _write_json(args.out, report)
    print(f"wrote={out}")
    print(f"dry_run={dry_run}")
    print(f"to_delete={plan['counts']['to_delete']} kept={plan['counts']['kept']}")
    print(f"verify_all_passed={verify.get('all_passed')}")
    print(f"verify_checks={verify.get('checks')}")
    return 0 if (dry_run or verify.get("all_passed")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
