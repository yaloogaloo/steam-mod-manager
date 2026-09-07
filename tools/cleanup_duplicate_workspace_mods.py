#!/usr/bin/env python3
"""One-shot historical cleanup: duplicate (platform, app_id, workspace_id) Mods.

Read-only by default (dry-run). Never touches Identity Service / reconcile /
import / sync / deploy hot paths — only this tool deletes confirmed pollution
rows and folders under the project root.

Usage::

    # dry-run → tools/_audit_out/workspace_duplicate_cleanup_report.json
    python tools/cleanup_duplicate_workspace_mods.py

    # apply only strategy-confirmed items
    python tools/cleanup_duplicate_workspace_mods.py --apply --confirm

Identity model (do not reinterpret)
-----------------------------------
- ``internal_id`` — sole entity identity
- ``workspace_id`` — per-game registration number
- same ``(platform, app_id, workspace_id)`` must not have multiple Mods
- same ``workspace_id`` across different ``app_id`` is allowed
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
DEFAULT_REPORT = OUT_DIR / "workspace_duplicate_cleanup_report.json"

INFO_DIR_NAMES = (".info", "info")
METADATA_NAMES = ("metadata.json", "mod.json")

# Historical failed-import folders: "<Title>_900000000000xxxx"
POLLUTION_DIR_RE = re.compile(r"^.+_9000\d+$")

ACTION_DELETE = "delete_pollution"
ACTION_FIX_INFO = "fix_info_internal_id"
ACTION_MANUAL = "manual_review"
ACTION_KEEP = "keep"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entity_internal_id(row: dict[str, Any]) -> str:
    """Prefer UUID ``internal_id`` column; fall back to PK string."""
    return _text(row.get("internal_id")) or _text(row.get("mod_id"))


def is_pollution_dirname(name: str) -> bool:
    return bool(POLLUTION_DIR_RE.match(_text(name)))


def _path_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _read_info(folder: Path) -> tuple[dict[str, Any] | None, str]:
    if not folder or not folder.is_dir():
        return None, ""
    for info_name in INFO_DIR_NAMES:
        for meta_name in METADATA_NAMES:
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


def _write_info_internal_id(meta_path: Path, internal_id: str) -> None:
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"invalid metadata JSON: {meta_path}")
    data["internal_id"] = _text(internal_id)
    meta_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def load_mod_rows(db_path: Path) -> list[dict[str, Any]]:
    con = _connect(db_path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(mods)")}
        wanted = [
            "mod_id",
            "internal_id",
            "workspace_id",
            "external_id",
            "platform",
            "app_id",
            "title",
            "display_name",
            "source_url",
            "last_known_path",
            "folder_present",
            "deploy_status",
            "deploy_path",
        ]
        select = ", ".join(c for c in wanted if c in cols)
        rows = []
        for r in con.execute(f"SELECT {select} FROM mods"):
            rows.append({k: r[k] for k in r.keys()})
        return rows
    finally:
        con.close()


def _entity_snapshot(
    row: dict[str, Any], *, info: dict[str, Any] | None = None
) -> dict[str, Any]:
    path = _text(row.get("last_known_path"))
    folder = Path(path) if path else None
    pollution = bool(folder and is_pollution_dirname(folder.name))
    info_iid = _text((info or {}).get("internal_id")) if info else ""
    info_wid = _text((info or {}).get("workspace_id")) if info else ""
    eid = _entity_internal_id(row)
    return {
        "mod_id": _text(row.get("mod_id")),
        "internal_id": eid,
        "workspace_id": _text(row.get("workspace_id")),
        "external_id": _text(row.get("external_id")),
        "platform": _text(row.get("platform")),
        "app_id": int(row.get("app_id") or 0),
        "title": _text(row.get("title") or row.get("display_name")),
        "last_known_path": path,
        "folder_present": int(row.get("folder_present") or 0),
        "path_exists": bool(folder and folder.is_dir()),
        "pollution_dirname": pollution,
        "info_internal_id": info_iid,
        "info_workspace_id": info_wid,
        "info_internal_id_matches": bool(
            info_iid and (info_iid == eid or info_iid == _text(row.get("mod_id")))
        ),
    }


def _info_matches_entity(row: dict[str, Any], info: dict[str, Any] | None) -> bool:
    if not info or info.get("_read_error"):
        return False
    info_iid = _text(info.get("internal_id"))
    if not info_iid:
        return False
    eid = _entity_internal_id(row)
    return info_iid == eid or info_iid == _text(row.get("mod_id"))


def _has_valid_path_binding(row: dict[str, Any]) -> bool:
    path = _text(row.get("last_known_path"))
    if not path:
        return False
    return Path(path).is_dir()


def _is_normal_dirname(row: dict[str, Any]) -> bool:
    path = _text(row.get("last_known_path"))
    if not path:
        return False
    return not is_pollution_dirname(Path(path).name)


def select_keeper(
    members: list[dict[str, Any]],
    infos: dict[str, dict[str, Any] | None],
) -> tuple[dict[str, Any] | None, str]:
    """
    Keep strategy (ordered):
    1. Prefer normal directory name (not ``*_9000…``)
    2. Prefer ``.info.internal_id`` matching the DB entity
    3. Prefer a valid on-disk path binding
    4. Otherwise → no keeper (manual_review)
    """
    if not members:
        return None, "empty_group"

    pool = list(members)

    normal = [m for m in pool if _is_normal_dirname(m)]
    if normal:
        pool = normal

    matched = [
        m
        for m in pool
        if _info_matches_entity(m, infos.get(_text(m.get("mod_id"))))
    ]
    if matched:
        pool = matched

    bound = [m for m in pool if _has_valid_path_binding(m)]
    if bound:
        pool = bound

    if len(pool) == 1:
        reason = "keep_strategy"
        if _is_normal_dirname(pool[0]):
            reason = "prefer_normal_dirname"
        elif _info_matches_entity(pool[0], infos.get(_text(pool[0].get("mod_id")))):
            reason = "prefer_info_internal_id_match"
        elif _has_valid_path_binding(pool[0]):
            reason = "prefer_valid_path_binding"
        return pool[0], reason

    return None, "ambiguous_keep_candidates"


def _group_key(row: dict[str, Any]) -> tuple[str, int, str] | None:
    wid = _text(row.get("workspace_id"))
    if not wid:
        return None
    platform = _text(row.get("platform")) or "other"
    app_id = int(row.get("app_id") or 0)
    return (platform, app_id, wid)


def build_cleanup_plan(
    *,
    db_path: Path,
    library_root: Path | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Scan DB + .info + dirs; return dry-run report payload (no mutations)."""
    _ = library_root  # reserved for future sibling-dir scans
    root = Path(project_root) if project_root is not None else ROOT
    rows = load_mod_rows(db_path)

    groups: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = _group_key(row)
        if key is None:
            continue
        groups[key].append(row)

    duplicate_groups = {k: v for k, v in groups.items() if len(v) > 1}

    actions: list[dict[str, Any]] = []
    summary = {
        "duplicate_groups": len(duplicate_groups),
        "delete_pollution": 0,
        "fix_info_internal_id": 0,
        "manual_review": 0,
        "keep": 0,
    }

    for (platform, app_id, workspace_id), members in sorted(
        duplicate_groups.items(),
        key=lambda kv: (kv[0][1], kv[0][0], kv[0][2]),
    ):
        infos: dict[str, dict[str, Any] | None] = {}
        info_paths: dict[str, str] = {}
        for m in members:
            mid = _text(m.get("mod_id"))
            path = _text(m.get("last_known_path"))
            info, ipath = _read_info(Path(path)) if path else (None, "")
            infos[mid] = info
            info_paths[mid] = ipath

        keeper, keep_reason = select_keeper(members, infos)
        if keeper is None:
            for m in members:
                snap = _entity_snapshot(m, info=infos.get(_text(m.get("mod_id"))))
                actions.append(
                    {
                        "before": {
                            "group": {
                                "platform": platform,
                                "app_id": app_id,
                                "workspace_id": workspace_id,
                            },
                            "entity": snap,
                            "group_size": len(members),
                        },
                        "action": ACTION_MANUAL,
                        "after": None,
                        "deleted_path": "",
                        "deleted_internal_id": "",
                        "kept_internal_id": "",
                        "reason": keep_reason,
                        "confirmed": False,
                    }
                )
                summary["manual_review"] += 1
            continue

        kept_id = _entity_internal_id(keeper)
        keep_mid = _text(keeper.get("mod_id"))
        keep_snap = _entity_snapshot(keeper, info=infos.get(keep_mid))
        actions.append(
            {
                "before": {
                    "group": {
                        "platform": platform,
                        "app_id": app_id,
                        "workspace_id": workspace_id,
                    },
                    "entity": keep_snap,
                    "group_size": len(members),
                },
                "action": ACTION_KEEP,
                "after": {"entity": keep_snap},
                "deleted_path": "",
                "deleted_internal_id": "",
                "kept_internal_id": kept_id,
                "reason": keep_reason,
                "confirmed": False,
            }
        )
        summary["keep"] += 1

        keep_info = infos.get(keep_mid)
        keep_info_path = info_paths.get(keep_mid) or ""
        if (
            keep_info
            and not keep_info.get("_read_error")
            and keep_info_path
            and _text(keep_info.get("workspace_id"))
            == _text(keeper.get("workspace_id"))
            and _text(keep_info.get("workspace_id"))
            and not _info_matches_entity(keeper, keep_info)
        ):
            before_info = {
                "path": keep_info_path,
                "internal_id": _text(keep_info.get("internal_id")),
                "workspace_id": _text(keep_info.get("workspace_id")),
            }
            actions.append(
                {
                    "before": {
                        "group": {
                            "platform": platform,
                            "app_id": app_id,
                            "workspace_id": workspace_id,
                        },
                        "entity": keep_snap,
                        "info": before_info,
                    },
                    "action": ACTION_FIX_INFO,
                    "after": {
                        "info": {
                            "path": keep_info_path,
                            "internal_id": kept_id,
                            "workspace_id": _text(keeper.get("workspace_id")),
                        }
                    },
                    "deleted_path": "",
                    "deleted_internal_id": "",
                    "kept_internal_id": kept_id,
                    "reason": (
                        "db_workspace_matches_info_workspace_but_internal_id_differs"
                    ),
                    "confirmed": True,
                    "info_path": keep_info_path,
                    "mod_id": keep_mid,
                }
            )
            summary["fix_info_internal_id"] += 1

        for m in members:
            mid = _text(m.get("mod_id"))
            if mid == keep_mid:
                continue
            snap = _entity_snapshot(m, info=infos.get(mid))
            path = _text(m.get("last_known_path"))
            pollution = bool(path and is_pollution_dirname(Path(path).name))
            if pollution:
                actions.append(
                    {
                        "before": {
                            "group": {
                                "platform": platform,
                                "app_id": app_id,
                                "workspace_id": workspace_id,
                            },
                            "entity": snap,
                            "group_size": len(members),
                        },
                        "action": ACTION_DELETE,
                        "after": {
                            "deleted": True,
                            "kept_internal_id": kept_id,
                        },
                        "deleted_path": path,
                        "deleted_internal_id": _entity_internal_id(m),
                        "kept_internal_id": kept_id,
                        "reason": (
                            f"duplicate_registration; pollution_dirname; "
                            f"kept_via={keep_reason}"
                        ),
                        "confirmed": True,
                        "mod_id": mid,
                        "project_path_ok": bool(
                            path and _path_under_root(Path(path), root)
                        ),
                    }
                )
                summary["delete_pollution"] += 1
            else:
                actions.append(
                    {
                        "before": {
                            "group": {
                                "platform": platform,
                                "app_id": app_id,
                                "workspace_id": workspace_id,
                            },
                            "entity": snap,
                            "group_size": len(members),
                        },
                        "action": ACTION_MANUAL,
                        "after": None,
                        "deleted_path": "",
                        "deleted_internal_id": "",
                        "kept_internal_id": kept_id,
                        "reason": (
                            "duplicate_registration_but_not_pollution_dirname; "
                            "refuse_auto_delete"
                        ),
                        "confirmed": False,
                        "mod_id": mid,
                    }
                )
                summary["manual_review"] += 1

    return {
        "generated_at": _now(),
        "mode": "dry-run",
        "db_path": str(db_path),
        "project_root": str(root),
        "summary": summary,
        "actions": actions,
    }


def _backup_dir_for_mod(mod_id: str, *, data_root: Path) -> Path:
    return data_root / "mod_backup" / _text(mod_id)


def _delete_associated_data(
    *,
    mod_id: str,
    folder: Path | None,
    project_root: Path,
    data_root: Path,
) -> dict[str, Any]:
    """Delete pollution folder + backup under project only. Never outside root."""
    deleted: dict[str, Any] = {
        "folder": "",
        "backup": "",
        "skipped_outside_project": [],
    }
    if folder is not None and folder.exists():
        if _path_under_root(folder, project_root):
            shutil.rmtree(folder)
            deleted["folder"] = str(folder)
        else:
            deleted["skipped_outside_project"].append(str(folder))

    bak = _backup_dir_for_mod(mod_id, data_root=data_root)
    if bak.exists():
        if _path_under_root(bak, project_root):
            shutil.rmtree(bak)
            deleted["backup"] = str(bak)
        else:
            deleted["skipped_outside_project"].append(str(bak))
    return deleted


def _delete_db_entity(db_path: Path, mod_id: str) -> bool:
    """Remove mods row + tags / relations / deploy index / audit via DB API."""
    from core.db_manager import DatabaseManager

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(db_path)
    try:
        return bool(db.delete_mod_record(mod_id))
    finally:
        DatabaseManager.reset_instance()


def apply_cleanup_plan(
    plan: dict[str, Any],
    *,
    apply: bool,
    confirm: bool,
    db_path: Path | None = None,
    project_root: Path | None = None,
    data_root: Path | None = None,
) -> dict[str, Any]:
    """
    Execute confirmed plan items.

    Requires ``apply=True`` and ``confirm=True``. Only items with
    ``confirmed=True`` and action in {delete_pollution, fix_info_internal_id}
    are mutated. ``manual_review`` / ``keep`` are never applied.
    """
    root = Path(project_root) if project_root is not None else ROOT
    data = Path(data_root) if data_root is not None else (root / "data")
    db_file = Path(db_path) if db_path is not None else Path(plan.get("db_path") or "")

    result: dict[str, Any] = {
        "generated_at": _now(),
        "applied": False,
        "dry_run": not (apply and confirm),
        "results": [],
        "db_path": str(db_file),
    }

    if not (apply and confirm):
        result["reason"] = "apply requires explicit --apply --confirm"
        result["results"] = [
            {
                "action": a.get("action"),
                "skipped": True,
                "reason": "dry-run",
                "confirmed": bool(a.get("confirmed")),
            }
            for a in plan.get("actions") or []
        ]
        return result

    if not db_file.is_file():
        raise FileNotFoundError(f"database not found: {db_file}")

    result["applied"] = True
    for item in plan.get("actions") or []:
        action = _text(item.get("action"))
        if action in {ACTION_KEEP, ACTION_MANUAL}:
            result["results"].append(
                {
                    "action": action,
                    "skipped": True,
                    "reason": "not_an_apply_action",
                }
            )
            continue
        if not item.get("confirmed"):
            result["results"].append(
                {
                    "action": action,
                    "skipped": True,
                    "reason": "not_confirmed",
                }
            )
            continue

        if action == ACTION_FIX_INFO:
            info_path = Path(_text(item.get("info_path")))
            kept = _text(item.get("kept_internal_id"))
            if not info_path.is_file():
                result["results"].append(
                    {
                        "action": action,
                        "ok": False,
                        "reason": "missing_info_path",
                        "info_path": str(info_path),
                    }
                )
                continue
            if not _path_under_root(info_path, root):
                result["results"].append(
                    {
                        "action": action,
                        "ok": False,
                        "reason": "info_outside_project",
                        "info_path": str(info_path),
                    }
                )
                continue
            before_iid = ""
            try:
                before = json.loads(info_path.read_text(encoding="utf-8"))
                before_iid = _text(before.get("internal_id"))
            except Exception:  # noqa: BLE001
                before = {}
            _write_info_internal_id(info_path, kept)
            result["results"].append(
                {
                    "action": action,
                    "ok": True,
                    "before": {"internal_id": before_iid},
                    "after": {"internal_id": kept},
                    "info_path": str(info_path),
                    "kept_internal_id": kept,
                    "deleted_path": "",
                    "deleted_internal_id": "",
                    "reason": item.get("reason"),
                }
            )
            continue

        if action == ACTION_DELETE:
            mid = _text(item.get("mod_id"))
            del_path = _text(item.get("deleted_path"))
            folder = Path(del_path) if del_path else None
            if (
                folder is not None
                and folder.exists()
                and not _path_under_root(folder, root)
            ):
                result["results"].append(
                    {
                        "action": action,
                        "ok": False,
                        "reason": "path_outside_project",
                        "deleted_path": del_path,
                    }
                )
                continue
            fs = _delete_associated_data(
                mod_id=mid,
                folder=folder if folder and folder.exists() else None,
                project_root=root,
                data_root=data,
            )
            db_ok = _delete_db_entity(db_file, mid)
            result["results"].append(
                {
                    "action": action,
                    "ok": db_ok,
                    "before": item.get("before"),
                    "after": {
                        "deleted": True,
                        "filesystem": fs,
                        "db_deleted": db_ok,
                    },
                    "deleted_path": fs.get("folder") or del_path,
                    "deleted_internal_id": item.get("deleted_internal_id"),
                    "kept_internal_id": item.get("kept_internal_id"),
                    "reason": item.get("reason"),
                }
            )
            continue

        result["results"].append(
            {
                "action": action,
                "skipped": True,
                "reason": "unknown_action",
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
            "Dry-run (default) or apply cleanup for duplicate "
            "(platform, app_id, workspace_id) Mod registrations."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite DB path (default: data/mod_manager.db)",
    )
    parser.add_argument(
        "--library",
        type=Path,
        default=None,
        help="Mod library root (default: <project>/mod)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help=f"Report output path (default: {DEFAULT_REPORT})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply confirmed actions (requires --confirm)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Second gate required with --apply",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=None,
        help="Optional existing plan/report JSON to apply",
    )
    args = parser.parse_args(argv)

    from core.paths import database_path, default_mod_library, project_root

    db_path = Path(args.db) if args.db else database_path()
    library = Path(args.library) if args.library else default_mod_library()
    root = project_root()

    if args.plan is not None:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    else:
        plan = build_cleanup_plan(
            db_path=db_path,
            library_root=library,
            project_root=root,
        )

    if args.apply:
        apply_result = apply_cleanup_plan(
            plan,
            apply=True,
            confirm=bool(args.confirm),
            db_path=db_path,
            project_root=root,
            data_root=root / "data",
        )
        if apply_result.get("applied"):
            after_plan = build_cleanup_plan(
                db_path=db_path,
                library_root=library,
                project_root=root,
            )
            report = {
                **plan,
                "mode": "apply",
                "apply_result": apply_result,
                "after_scan_summary": after_plan.get("summary"),
            }
        else:
            report = {**plan, "mode": "dry-run", "apply_result": apply_result}
    else:
        report = plan

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
