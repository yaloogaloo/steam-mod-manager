#!/usr/bin/env python3
"""One-shot Stardew Valley workspace_id duplicate folder cleanup.

Scans ``mod/星露谷物语/*`` sidecars only. Never creates/deletes DB entities,
never touches Identity Service / Sync / Reconcile / Import / Deploy.

Usage::

    python tools/cleanup_stardew_workspace_duplicates.py
    python tools/cleanup_stardew_workspace_duplicates.py --dry-run
    python tools/cleanup_stardew_workspace_duplicates.py --apply --confirm
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
DEFAULT_REPORT = OUT_DIR / "stardew_workspace_duplicate_cleanup.json"

STARDEW_APP_ID = 413150
STARDEW_GAME_DIR = "星露谷物语"

INFO_DIR = ".info"
METADATA = "metadata.json"
LEGACY_INFO_DIR = "info"
LEGACY_METADATA = "mod.json"

# Same historical pollution naming used elsewhere.
POLLUTION_DIR_RE = re.compile(r"^(.+)_900000000000\d*$")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_pollution_dirname(name: str) -> bool:
    return bool(POLLUTION_DIR_RE.match(_text(name)))


def _norm_path(path: str | Path) -> str:
    raw = _text(path)
    if not raw:
        return ""
    try:
        return str(Path(raw).resolve()).lower().replace("/", "\\")
    except OSError:
        return raw.lower().replace("/", "\\")


def _path_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _read_info(folder: Path) -> tuple[dict[str, Any] | None, str]:
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
            return {"_read_error": True}, str(meta)
        if isinstance(data, dict):
            return data, str(meta)
        return {"_read_error": True}, str(meta)
    return None, ""


def _has_valid_info(info: dict[str, Any] | None) -> bool:
    if not info or info.get("_read_error"):
        return False
    return bool(_text(info.get("workspace_id")) or _text(info.get("internal_id")))


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def load_stardew_db_rows(db_path: Path, *, app_id: int = STARDEW_APP_ID) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    con = _connect(db_path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(mods)")}
        wanted = [
            "mod_id",
            "internal_id",
            "workspace_id",
            "app_id",
            "platform",
            "title",
            "last_known_path",
            "folder_present",
        ]
        select = ", ".join(c for c in wanted if c in cols)
        rows = []
        for r in con.execute(
            f"SELECT {select} FROM mods WHERE COALESCE(app_id, 0) = ?",
            (int(app_id),),
        ):
            rows.append({k: r[k] for k in r.keys()})
        return rows
    finally:
        con.close()


def _entity_internal_id(row: dict[str, Any]) -> str:
    return _text(row.get("internal_id")) or _text(row.get("mod_id"))


def _db_binding_for_path(
    rows: list[dict[str, Any]], folder: Path
) -> dict[str, Any] | None:
    target = _norm_path(folder)
    for row in rows:
        if _norm_path(row.get("last_known_path")) == target:
            return {
                "mod_id": _text(row.get("mod_id")),
                "internal_id": _entity_internal_id(row),
                "workspace_id": _text(row.get("workspace_id")),
                "last_known_path": _text(row.get("last_known_path")),
                "folder_present": int(row.get("folder_present") or 0),
            }
    return None


def _db_entities_for_workspace(
    rows: list[dict[str, Any]], workspace_id: str
) -> list[dict[str, Any]]:
    wid = _text(workspace_id)
    if not wid:
        return []
    out = []
    for row in rows:
        if _text(row.get("workspace_id")) == wid:
            out.append(row)
    return out


def _folder_entry(
    folder: Path,
    *,
    info: dict[str, Any] | None,
    info_path: str,
    db_binding: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "path": str(folder.resolve()),
        "folder": folder.name,
        "pollution_dirname": is_pollution_dirname(folder.name),
        "internal_id": _text((info or {}).get("internal_id")),
        "workspace_id": _text((info or {}).get("workspace_id")),
        "info_path": info_path,
        "valid_info": _has_valid_info(info),
        "db_binding": db_binding,
    }


def select_keep_candidate(
    entries: list[dict[str, Any]],
    *,
    db_entities: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    """
    Keep rules (ordered):
    1. Directory currently bound by DB ``last_known_path``
    2. Else prefer normal (non-``*_9000…``) directory name
    3. Else prefer directory with valid ``.info``
    """
    if not entries:
        return None, "empty"

    bound_paths = {
        _norm_path(e.get("last_known_path"))
        for e in db_entities
        if _text(e.get("last_known_path"))
    }
    bound = [e for e in entries if _norm_path(e["path"]) in bound_paths]
    if len(bound) == 1:
        return bound[0], "db_bound_path"
    if len(bound) > 1:
        # Prefer bound + normal name, then valid info.
        pool = bound
        normal = [e for e in pool if not e.get("pollution_dirname")]
        if normal:
            pool = normal
        valid = [e for e in pool if e.get("valid_info")]
        if valid:
            pool = valid
        pool = sorted(pool, key=lambda e: e["folder"].lower())
        return pool[0], "db_bound_path_disambiguated"

    # DB not bound to any of these folders.
    pool = list(entries)
    normal = [e for e in pool if not e.get("pollution_dirname")]
    if normal:
        pool = normal
        if len(pool) == 1:
            return pool[0], "normal_dirname"
    valid = [e for e in pool if e.get("valid_info")]
    if valid:
        pool = valid
        if len(pool) == 1:
            return pool[0], "valid_info"
    if len(pool) == 1:
        return pool[0], "sole_remaining"
    pool = sorted(pool, key=lambda e: e["folder"].lower())
    return pool[0], "deterministic_fallback"


def scan_stardew_workspace_duplicates(
    *,
    library_root: Path,
    db_path: Path,
    game_dir_name: str = STARDEW_GAME_DIR,
    app_id: int = STARDEW_APP_ID,
) -> dict[str, Any]:
    """Filesystem-first duplicate scan for Stardew Valley only."""
    game_dir = library_root / game_dir_name
    db_rows = load_stardew_db_rows(db_path, app_id=app_id)

    by_workspace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    scanned = 0

    if game_dir.is_dir():
        for child in sorted(game_dir.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            scanned += 1
            info, info_path = _read_info(child)
            wid = _text((info or {}).get("workspace_id")) if info else ""
            if not wid:
                continue
            binding = _db_binding_for_path(db_rows, child)
            by_workspace[wid].append(
                _folder_entry(
                    child, info=info, info_path=info_path, db_binding=binding
                )
            )

    groups: list[dict[str, Any]] = []
    for wid, entries in sorted(by_workspace.items(), key=lambda kv: kv[0]):
        if len(entries) < 2:
            continue
        db_entities = _db_entities_for_workspace(db_rows, wid)
        keep, reason = select_keep_candidate(entries, db_entities=db_entities)
        if keep is None:
            continue
        delete_candidates = [
            e for e in entries if _norm_path(e["path"]) != _norm_path(keep["path"])
        ]

        # Primary DB entity for rebind / info fix: prefer entity bound to keep,
        # else any entity with this workspace_id.
        db_entity: dict[str, Any] | None = None
        keep_norm = _norm_path(keep["path"])
        for row in db_entities:
            if _norm_path(row.get("last_known_path")) == keep_norm:
                db_entity = {
                    "mod_id": _text(row.get("mod_id")),
                    "internal_id": _entity_internal_id(row),
                    "workspace_id": _text(row.get("workspace_id")),
                    "last_known_path": _text(row.get("last_known_path")),
                    "folder_present": int(row.get("folder_present") or 0),
                }
                break
        if db_entity is None and db_entities:
            row = db_entities[0]
            db_entity = {
                "mod_id": _text(row.get("mod_id")),
                "internal_id": _entity_internal_id(row),
                "workspace_id": _text(row.get("workspace_id")),
                "last_known_path": _text(row.get("last_known_path")),
                "folder_present": int(row.get("folder_present") or 0),
            }

        need_info_fix = False
        if db_entity and keep.get("valid_info"):
            info_wid = _text(keep.get("workspace_id"))
            info_iid = _text(keep.get("internal_id"))
            db_iid = _text(db_entity.get("internal_id"))
            if (
                info_wid
                and info_wid == _text(db_entity.get("workspace_id"))
                and info_iid
                and db_iid
                and info_iid != db_iid
            ):
                need_info_fix = True

        groups.append(
            {
                "app_id": app_id,
                "workspace_id": wid,
                "duplicate_paths": [e["path"] for e in entries],
                "folders": entries,
                "each_internal_id": [
                    {"path": e["path"], "internal_id": e["internal_id"]}
                    for e in entries
                ],
                "db_binding": db_entity,
                "keep_candidate": keep,
                "delete_candidates": delete_candidates,
                "keep_reason": reason,
                "fix_info_internal_id": need_info_fix,
                "confirmed": True,
            }
        )

    return {
        "generated_at": _now(),
        "mode": "dry-run",
        "app_id": app_id,
        "game_dir": str(game_dir),
        "game_dir_name": game_dir_name,
        "library_root": str(library_root.resolve()),
        "db_path": str(db_path),
        "summary": {
            "scanned_folders": scanned,
            "duplicate_groups": len(groups),
            "delete_candidates": sum(len(g["delete_candidates"]) for g in groups),
            "info_fixes": sum(1 for g in groups if g.get("fix_info_internal_id")),
        },
        "groups": groups,
    }


def _update_db_path(
    db_path: Path,
    *,
    mod_id: str,
    last_known_path: str,
    folder_present: bool,
) -> None:
    con = _connect(db_path)
    try:
        con.execute(
            """
            UPDATE mods
            SET last_known_path = ?, folder_present = ?
            WHERE mod_id = ?
            """,
            (
                _text(last_known_path),
                1 if folder_present else 0,
                int(str(mod_id).strip()),
            ),
        )
        con.commit()
    finally:
        con.close()


def _fix_info_internal_id(folder: Path, internal_id: str) -> str:
    info, info_path = _read_info(folder)
    if not info_path:
        # Create canonical sidecar if missing but keep exists.
        meta = folder / INFO_DIR / METADATA
        folder.joinpath(INFO_DIR).mkdir(parents=True, exist_ok=True)
        payload = dict(info or {})
        payload["internal_id"] = _text(internal_id)
        meta.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return str(meta)
    meta = Path(info_path)
    data = json.loads(meta.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid metadata: {meta}")
    data["internal_id"] = _text(internal_id)
    meta.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return str(meta)


def apply_stardew_workspace_cleanup(
    plan: dict[str, Any],
    *,
    apply: bool,
    confirm: bool,
    library_root: Path,
    db_path: Path,
    project_root: Path | None = None,
    game_dir_name: str = STARDEW_GAME_DIR,
) -> dict[str, Any]:
    """
    Delete confirmed duplicate folders, rebind DB path, fix keep ``.info``.

    Never creates or deletes DB entities.
    """
    root = Path(project_root) if project_root is not None else ROOT
    lib = library_root.resolve()
    game_dir = (lib / game_dir_name).resolve()

    result: dict[str, Any] = {
        "generated_at": _now(),
        "applied": False,
        "dry_run": not (apply and confirm),
        "results": [],
    }

    if not (apply and confirm):
        result["reason"] = "apply requires explicit --apply --confirm"
        for g in plan.get("groups") or []:
            result["results"].append(
                {
                    "workspace_id": g.get("workspace_id"),
                    "skipped": True,
                    "reason": "dry-run",
                    "confirmed": bool(g.get("confirmed")),
                }
            )
        return result

    result["applied"] = True
    for group in plan.get("groups") or []:
        if not group.get("confirmed", True):
            result["results"].append(
                {
                    "workspace_id": group.get("workspace_id"),
                    "skipped": True,
                    "reason": "not_confirmed",
                }
            )
            continue

        keep = group.get("keep_candidate") or {}
        keep_path = Path(_text(keep.get("path")))
        deletes = group.get("delete_candidates") or []
        db_binding = group.get("db_binding")
        deleted: list[str] = []
        errors: list[str] = []

        for cand in deletes:
            folder = Path(_text(cand.get("path")))
            try:
                folder_res = folder.resolve()
            except OSError:
                folder_res = folder
            if not _path_under(folder_res, root):
                errors.append(f"outside_project:{folder}")
                continue
            if not _path_under(folder_res, game_dir):
                errors.append(f"outside_stardew_game_dir:{folder}")
                continue
            if folder_res == game_dir:
                errors.append(f"refuse_delete_game_dir:{folder}")
                continue
            if folder_res.is_dir():
                shutil.rmtree(folder_res)
                deleted.append(str(folder_res))

        rebind = None
        info_fix = None
        if isinstance(db_binding, dict) and _text(db_binding.get("mod_id")):
            mid = _text(db_binding.get("mod_id"))
            if keep_path.is_dir():
                _update_db_path(
                    db_path,
                    mod_id=mid,
                    last_known_path=str(keep_path.resolve()),
                    folder_present=True,
                )
                rebind = {
                    "mod_id": mid,
                    "last_known_path": str(keep_path.resolve()),
                    "folder_present": 1,
                }

            if group.get("fix_info_internal_id") and keep_path.is_dir():
                db_iid = _text(db_binding.get("internal_id"))
                if db_iid:
                    meta_path = _fix_info_internal_id(keep_path, db_iid)
                    info_fix = {
                        "info_path": meta_path,
                        "internal_id": db_iid,
                    }

        result["results"].append(
            {
                "workspace_id": group.get("workspace_id"),
                "ok": not errors,
                "keep_path": str(keep_path),
                "deleted_paths": deleted,
                "errors": errors,
                "db_rebind": rebind,
                "info_fix": info_fix,
                "keep_reason": group.get("keep_reason"),
            }
        )

    return result


def write_report(payload: dict[str, Any], path: Path = DEFAULT_REPORT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dry-run (default) or clean duplicate workspace_id folders "
            "under mod/星露谷物语."
        )
    )
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--library", type=Path, default=None)
    parser.add_argument(
        "--game-dir-name",
        default=STARDEW_GAME_DIR,
        help=f"Game folder under mod/ (default: {STARDEW_GAME_DIR})",
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--plan", type=Path, default=None)
    args = parser.parse_args(argv)

    from core.paths import database_path, default_mod_library, project_root

    db_path = Path(args.db) if args.db else database_path()
    library = Path(args.library) if args.library else default_mod_library()
    root = project_root()
    game_name = _text(args.game_dir_name) or STARDEW_GAME_DIR

    if args.plan is not None:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    else:
        plan = scan_stardew_workspace_duplicates(
            library_root=library,
            db_path=db_path,
            game_dir_name=game_name,
        )

    if args.apply:
        apply_result = apply_stardew_workspace_cleanup(
            plan,
            apply=True,
            confirm=bool(args.confirm),
            library_root=library,
            db_path=db_path,
            project_root=root,
            game_dir_name=game_name,
        )
        report = {
            **plan,
            "mode": "apply" if apply_result.get("applied") else "dry-run",
            "apply_result": apply_result,
        }
        if apply_result.get("applied"):
            after = scan_stardew_workspace_duplicates(
                library_root=library,
                db_path=db_path,
                game_dir_name=game_name,
            )
            report["after_scan_summary"] = after.get("summary")
    else:
        report = {**plan, "mode": "dry-run"}
        if args.dry_run:
            report["dry_run_explicit"] = True

    out = write_report(report, Path(args.report))
    print(f"wrote {out}")
    print(json.dumps(report.get("summary") or {}, ensure_ascii=False))
    if args.apply and not args.confirm:
        print(
            "note: --apply without --confirm performs no mutations",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
