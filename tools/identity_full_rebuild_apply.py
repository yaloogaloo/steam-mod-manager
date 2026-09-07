#!/usr/bin/env python3
"""Identity full rebuild — Phase 4 apply + Phase 5 verify.

One-shot recovery tool (not production runtime).

Apply steps
-----------
1. Backup + create a new database (preserve ``games``)
2. Drop old Mod entity rows / identity pollution
3. Each kept Mod gets a fresh UUID ``internal_id`` (never old mod_id / 9000…)
4. Insert DB rows from disk ``.info`` + path
5. Write back ``.info.internal_id``
6. Set ``last_known_path``
7. Quarantine ``DELETE_CANDIDATE`` directories
8. Emit ``identity_rebuild_report.json`` with verification gates

Usage::

    python tools/identity_full_rebuild_apply.py --dry-run
    python tools/identity_full_rebuild_apply.py --apply --confirm
"""

from __future__ import annotations

import argparse
import json
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

from tools.identity_full_rebuild_common import (  # noqa: E402
    ACTION_DELETE_CANDIDATE,
    ACTION_REBUILD,
    ACTION_SKIP,
    DEFAULT_DB,
    DEFAULT_LIBRARY,
    OUT_DIR,
    is_pollution_id,
    load_json,
    now_utc,
    plan_workspace_actions,
    read_info,
    scan_disk_entries,
    text,
    write_info_internal_id,
    write_json,
)

BG3_APP_ID = 1086940
STARDEW_APP_ID = 413150


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _copy_games(src_db: Path, dst_db: Path) -> int:
    if not src_db.is_file():
        return 0
    src = sqlite3.connect(str(src_db))
    dst = sqlite3.connect(str(dst_db))
    try:
        src.row_factory = sqlite3.Row
        cols = [r[1] for r in src.execute("PRAGMA table_info(games)")]
        if not cols:
            return 0
        rows = src.execute("SELECT * FROM games").fetchall()
        if not rows:
            return 0
        placeholders = ", ".join("?" for _ in cols)
        col_list = ", ".join(cols)
        dst.execute("DELETE FROM games")
        for row in rows:
            dst.execute(
                f"INSERT INTO games ({col_list}) VALUES ({placeholders})",
                [row[c] for c in cols],
            )
        dst.commit()
        return len(rows)
    finally:
        src.close()
        dst.close()


def _clear_mod_tables(con: sqlite3.Connection) -> None:
    for table in (
        "deployment_record_items",
        "deployment_records",
        "mod_relationships",
        "mod_relations",
        "mod_tags",
        "identity_audit_log",
        "mods",
    ):
        try:
            con.execute(f"DELETE FROM {table}")
        except sqlite3.Error:
            pass
    con.commit()


def _info_meta(info: dict[str, Any] | None) -> dict[str, Any]:
    data = info or {}
    platform = text(data.get("platform") or data.get("source_type")).lower() or "other"
    source_url = text(data.get("url") or data.get("source_url"))
    external_id = text(data.get("external_id") or data.get("published_file_id"))
    # external_id is metadata only — strip pollution mirrors of old internal PK.
    if is_pollution_id(external_id):
        external_id = ""
    title = text(data.get("title") or data.get("display_name"))
    description = text(data.get("description") or data.get("custom_description"))
    cover_path = text(data.get("cover_path"))
    custom_deploy = text(data.get("custom_deploy_path"))
    preview_url = text(data.get("preview_url"))
    game_version = text(data.get("game_version")) or None
    mod_files = data.get("mod_files")
    if isinstance(mod_files, (dict, list)):
        mod_files_json = json.dumps(mod_files, ensure_ascii=False)
    else:
        mod_files_json = text(mod_files) or "{}"
    return {
        "platform": platform,
        "source_type": platform,
        "source_url": source_url,
        "external_id": external_id,
        "title": title,
        "description": description,
        "cover_path": cover_path,
        "custom_deploy_path": custom_deploy,
        "preview_url": preview_url,
        "game_version": game_version,
        "mod_files": mod_files_json,
    }


def _insert_mod(
    con: sqlite3.Connection,
    *,
    mod_id: int,
    internal_id: str,
    app_id: int,
    workspace_id: str,
    path: str,
    meta: dict[str, Any],
) -> None:
    now = _utc_now()
    game_version = meta.get("game_version")
    # Witcher 3 only — leave NULL for every other game.
    if int(app_id) != 292030:
        game_version = None
    con.execute(
        """
        INSERT INTO mods (
            mod_id, app_id, title, preview_url, description,
            display_name, custom_description, user_notes, favorite,
            deploy_status, deploy_time, deploy_path, deploy_error,
            platform, source_url, external_id, workspace_id,
            custom_deploy_path, mod_files, is_invalid, invalid_reason,
            conflict_status, conflict_note, last_check_time,
            mod_version, installed_version, version_source, version_checked_at,
            enabled, offline_status, offline_provider, offline_updated_at,
            cover_path, last_known_path, folder_present,
            internal_id, library_status, source_type, content_status,
            identity_status, official_metadata_synced, user_override_fields,
            game_version, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?,
            '', '', '', 0,
            'not_deployed', '', '', '',
            ?, ?, ?, ?,
            ?, ?, 0, '',
            'none', '', '',
            '', '', '', '',
            1, 'none', '', '',
            ?, ?, 1,
            ?, '', ?, '',
            'ok', 0, '{}',
            ?, ?
        )
        """,
        (
            int(mod_id),
            int(app_id),
            meta["title"] or f"Mod_{workspace_id}",
            meta["preview_url"],
            meta["description"],
            meta["platform"],
            meta["source_url"],
            meta["external_id"],
            workspace_id,
            meta["custom_deploy_path"],
            meta["mod_files"],
            meta["cover_path"],
            path,
            internal_id,
            meta["source_type"],
            game_version,
            now,
        ),
    )


def quarantine_folder(folder: Path, quarantine_root: Path) -> str:
    folder = folder.resolve()
    rel = folder.name
    game = folder.parent.name
    dest = quarantine_root / game / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        dest = quarantine_root / game / f"{rel}__{stamp}"
    shutil.move(str(folder), str(dest))
    return str(dest)


def apply_rebuild(
    *,
    library: Path,
    db_path: Path,
    preview_path: Path | None,
    dry_run: bool,
    confirm: bool,
) -> dict[str, Any]:
    entries = scan_disk_entries(library, db_path=db_path)
    if preview_path and preview_path.is_file():
        preview = load_json(preview_path)
        items = list(preview.get("items") or [])
        # Prefer live re-plan if preview is stale path-wise; else use preview.
        live = {text(i.get("path")): i for i in plan_workspace_actions(entries)}
        merged = []
        for item in items:
            path = text(item.get("path"))
            live_item = live.get(path)
            if live_item is None:
                continue
            # Keep preview UUID so apply is reproducible from preview file.
            if item.get("action") == ACTION_REBUILD and text(item.get("new_internal_id")):
                live_item = dict(live_item)
                live_item["new_internal_id"] = text(item.get("new_internal_id"))
                live_item["action"] = ACTION_REBUILD
            elif item.get("action") == ACTION_DELETE_CANDIDATE:
                live_item = dict(live_item)
                live_item["action"] = ACTION_DELETE_CANDIDATE
                live_item["new_internal_id"] = ""
            merged.append(live_item)
        # Include any new live paths not in preview.
        preview_paths = {text(i.get("path")) for i in items}
        for path, live_item in live.items():
            if path not in preview_paths:
                merged.append(live_item)
        planned = merged
    else:
        planned = plan_workspace_actions(entries)

    rebuild_items = [i for i in planned if i.get("action") == ACTION_REBUILD]
    delete_items = [i for i in planned if i.get("action") == ACTION_DELETE_CANDIDATE]
    skip_items = [i for i in planned if i.get("action") == ACTION_SKIP]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_db = db_path.with_name(f"{db_path.name}.pre_identity_rebuild_{stamp}")
    quarantine_root = OUT_DIR / f"rebuild_quarantine_{stamp}"

    result: dict[str, Any] = {
        "generated_at": now_utc(),
        "tool": "identity_full_rebuild_apply",
        "dry_run": dry_run,
        "confirm": confirm,
        "library_root": str(library.resolve()),
        "db_path": str(db_path.resolve()),
        "backup_db": str(backup_db),
        "quarantine_root": str(quarantine_root),
        "planned_counts": {
            ACTION_REBUILD: len(rebuild_items),
            ACTION_DELETE_CANDIDATE: len(delete_items),
            ACTION_SKIP: len(skip_items),
        },
        "applied": {
            "inserted": 0,
            "info_written": 0,
            "quarantined": 0,
            "games_copied": 0,
        },
        "errors": [],
    }

    if dry_run or not confirm:
        result["status"] = "dry_run"
        result["note"] = "Pass --apply --confirm to execute."
        return result

    # --- mutate ---
    # Prefer an atomic file replace. On Windows the live DB may be locked by the
    # running app — fall back to in-place clear + rebuild on the same path.
    if db_path.is_file():
        shutil.copy2(db_path, backup_db)

    new_db_path = db_path.with_name(f"{db_path.stem}.rebuild_{stamp}{db_path.suffix}")
    if new_db_path.is_file():
        new_db_path.unlink()

    from core.db_manager import DatabaseManager

    DatabaseManager.reset_instance()
    db = DatabaseManager(new_db_path)
    db.close()
    DatabaseManager.reset_instance()

    games_copied = _copy_games(
        backup_db if backup_db.is_file() else db_path, new_db_path
    )
    result["applied"]["games_copied"] = games_copied
    result["new_db_path"] = str(new_db_path)

    con = sqlite3.connect(str(new_db_path))
    try:
        _clear_mod_tables(con)
        next_id = 1
        applied_rows: list[dict[str, Any]] = []
        for item in rebuild_items:
            path = Path(text(item.get("path")))
            if not path.is_dir():
                result["errors"].append({"path": str(path), "error": "missing_folder"})
                continue
            info, info_path = read_info(path)
            if not info_path:
                result["errors"].append({"path": str(path), "error": "missing_info"})
                continue
            meta = _info_meta(info if info and not info.get("_read_error") else item.get("info"))
            # Prefer planned workspace (may include generated token).
            workspace_id = text(item.get("workspace_id"))
            if not workspace_id:
                result["errors"].append({"path": str(path), "error": "empty_workspace"})
                continue
            internal_id = text(item.get("new_internal_id"))
            if not internal_id:
                result["errors"].append({"path": str(path), "error": "empty_new_internal_id"})
                continue
            app_id = int(item.get("app_id") or 0)
            if app_id <= 0:
                result["errors"].append({"path": str(path), "error": "missing_app_id"})
                continue
            try:
                _insert_mod(
                    con,
                    mod_id=next_id,
                    internal_id=internal_id,
                    app_id=app_id,
                    workspace_id=workspace_id,
                    path=str(path.resolve()),
                    meta=meta,
                )
                write_info_internal_id(Path(info_path), internal_id)
                applied_rows.append(
                    {
                        "mod_id": next_id,
                        "internal_id": internal_id,
                        "workspace_id": workspace_id,
                        "app_id": app_id,
                        "path": str(path.resolve()),
                    }
                )
                result["applied"]["inserted"] += 1
                result["applied"]["info_written"] += 1
                next_id += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append({"path": str(path), "error": str(exc)})
        con.commit()
    finally:
        con.close()

    # Activate rebuilt DB as the live database path.
    activated = False
    activate_error = ""
    try:
        for side in (
            db_path,
            Path(str(db_path) + "-wal"),
            Path(str(db_path) + "-shm"),
        ):
            if side.is_file():
                try:
                    side.unlink()
                except OSError:
                    pass
        shutil.copy2(new_db_path, db_path)
        activated = True
        result["db_path"] = str(db_path.resolve())
    except OSError as exc:
        activate_error = str(exc)
        # Leave rebuilt file beside live path; verify uses rebuilt file.
        result["db_path"] = str(new_db_path.resolve())
        result["errors"].append(
            {
                "path": str(db_path),
                "error": f"activate_live_db_failed:{exc}",
            }
        )
    result["activated_live_db"] = activated
    if activate_error:
        result["activate_error"] = activate_error

    quarantine_root.mkdir(parents=True, exist_ok=True)
    for item in delete_items:
        path = Path(text(item.get("path")))
        if not path.is_dir():
            continue
        try:
            dest = quarantine_folder(path, quarantine_root)
            result["applied"]["quarantined"] += 1
            result.setdefault("quarantined_paths", []).append(
                {"from": str(path), "to": dest}
            )
        except Exception as exc:  # noqa: BLE001
            result["errors"].append({"path": str(path), "error": f"quarantine:{exc}"})

    result["status"] = "applied"
    result["sample_rows"] = applied_rows[:5]
    return result


def verify_rebuild(*, library: Path, db_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r)
            for r in con.execute(
                """
                SELECT mod_id, internal_id, workspace_id, app_id, title,
                       last_known_path, platform, external_id
                FROM mods
                ORDER BY mod_id
                """
            )
        ]
    finally:
        con.close()

    internal_ids = [text(r.get("internal_id")) for r in rows]
    iid_counts: dict[str, int] = defaultdict(int)
    for iid in internal_ids:
        if iid:
            iid_counts[iid] += 1
    dup_internal = sorted(k for k, n in iid_counts.items() if n > 1)

    pollution_mod_ids = [
        str(r.get("mod_id"))
        for r in rows
        if is_pollution_id(r.get("mod_id")) or is_pollution_id(r.get("internal_id"))
    ]

    info_mismatches: list[dict[str, Any]] = []
    missing_info: list[dict[str, Any]] = []
    for row in rows:
        path = text(row.get("last_known_path"))
        iid = text(row.get("internal_id"))
        if not path or not Path(path).is_dir():
            missing_info.append(
                {
                    "mod_id": row.get("mod_id"),
                    "internal_id": iid,
                    "path": path,
                    "reason": "path_missing",
                }
            )
            continue
        info, _ = read_info(Path(path))
        info_iid = text((info or {}).get("internal_id"))
        if info_iid != iid:
            info_mismatches.append(
                {
                    "mod_id": row.get("mod_id"),
                    "db_internal_id": iid,
                    "info_internal_id": info_iid,
                    "path": path,
                }
            )

    # Same-game workspace uniqueness.
    ws_by_app: dict[tuple[int, str], list[int]] = defaultdict(list)
    for row in rows:
        app_id = int(row.get("app_id") or 0)
        ws = text(row.get("workspace_id"))
        if app_id > 0 and ws:
            ws_by_app[(app_id, ws)].append(int(row.get("mod_id") or 0))
    same_game_dups = [
        {"app_id": app, "workspace_id": ws, "mod_ids": mids}
        for (app, ws), mids in sorted(ws_by_app.items())
        if len(mids) > 1
    ]

    # Cross-game coexistence (BG3 / Stardew).
    ws_apps: dict[str, set[int]] = defaultdict(set)
    for row in rows:
        ws = text(row.get("workspace_id"))
        app_id = int(row.get("app_id") or 0)
        if ws and app_id > 0:
            ws_apps[ws].add(app_id)
    bg3_stardew = [
        {"workspace_id": ws, "app_ids": sorted(apps)}
        for ws, apps in sorted(ws_apps.items())
        if BG3_APP_ID in apps and STARDEW_APP_ID in apps
    ]

    # Deploy / Library smoke checks via public APIs.
    deploy_ok = True
    library_ok = True
    deploy_samples: list[dict[str, Any]] = []
    library_error = ""
    library_card_count = 0
    try:
        from core.db_manager import DatabaseManager
        from services.deploy_paths import resolve_deploy_managed_path

        DatabaseManager.reset_instance()
        db = DatabaseManager(db_path)
        sample = rows[: min(5, len(rows))]
        for row in sample:
            mid = str(row.get("mod_id"))
            found = resolve_deploy_managed_path(mid, db=db, library_root=library)
            ok = bool(found and found.is_dir())
            deploy_samples.append(
                {
                    "mod_id": mid,
                    "internal_id": text(row.get("internal_id")),
                    "resolved": str(found) if found else "",
                    "ok": ok,
                }
            )
            if not ok:
                deploy_ok = False
        db.close()
        DatabaseManager.reset_instance()
    except Exception as exc:  # noqa: BLE001
        deploy_ok = False
        deploy_samples.append({"error": str(exc)})

    try:
        from core.db_manager import DatabaseManager
        from services.mod_library_cache import build_library_snapshot
        import inspect

        DatabaseManager.reset_instance()
        db = DatabaseManager(db_path)
        sig = inspect.signature(build_library_snapshot)
        kwargs: dict[str, Any] = {}
        if "force" in sig.parameters:
            kwargs["force"] = True
        if "db" in sig.parameters:
            kwargs["db"] = db
        snap = build_library_snapshot(library, **kwargs)
        cards = getattr(snap, "cards", None) or []
        library_card_count = len(cards)
        library_ok = library_card_count > 0
        db.close()
        DatabaseManager.reset_instance()
    except Exception as exc:  # noqa: BLE001
        library_ok = False
        library_card_count = 0
        library_error = str(exc)

    # Old mod_id leakage: sequential rebuild starts at 1; "old" means pollution /
    # leftover 9000… only (cannot know prior PK set after DB replace).
    old_mod_id_present = bool(pollution_mod_ids)

    checks = {
        "db_internal_id_globally_unique": len(dup_internal) == 0 and all(internal_ids),
        "info_internal_id_matches_db": len(info_mismatches) == 0 and len(missing_info) == 0,
        "same_game_workspace_unique": len(same_game_dups) == 0,
        "bg3_stardew_shared_workspace_allowed": True,  # coexistence is allowed
        "no_900000000000_ids": len(pollution_mod_ids) == 0,
        "no_old_pollution_mod_id": not old_mod_id_present,
        "deploy_resolves_by_internal_id": deploy_ok,
        "library_loads": library_ok,
    }

    return {
        "generated_at": now_utc(),
        "mods_count": len(rows),
        "checks": checks,
        "all_passed": all(checks.values()),
        "duplicate_internal_ids": dup_internal,
        "info_mismatches": info_mismatches[:50],
        "missing_paths": missing_info[:50],
        "same_game_workspace_duplicates": same_game_dups[:50],
        "bg3_stardew_shared_workspaces": bg3_stardew,
        "pollution_ids": pollution_mod_ids[:50],
        "deploy_samples": deploy_samples,
        "library_card_count": library_card_count,
        "library_error": library_error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--preview",
        type=Path,
        default=OUT_DIR / "rebuild_preview.json",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=OUT_DIR / "identity_rebuild_report.json",
    )
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--apply", action="store_true", default=False)
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Required with --apply to mutate DB / .info / quarantine",
    )
    args = parser.parse_args()

    dry_run = args.dry_run or not (args.apply and args.confirm)
    apply_result = apply_rebuild(
        library=args.library,
        db_path=args.db,
        preview_path=args.preview,
        dry_run=dry_run,
        confirm=bool(args.apply and args.confirm),
    )

    report: dict[str, Any] = {
        "generated_at": now_utc(),
        "tool": "identity_full_rebuild",
        "apply": apply_result,
    }

    if apply_result.get("status") == "applied":
        verify_db = Path(
            text(apply_result.get("db_path")) or args.db
        )
        report["verify"] = verify_rebuild(library=args.library, db_path=verify_db)
    else:
        # Still emit a preflight verification against current state when dry-run.
        report["verify"] = {
            "skipped": True,
            "reason": "apply_not_executed",
            "planned": apply_result.get("planned_counts"),
        }

    out = write_json(args.report, report)
    print(f"wrote={out}")
    print(f"apply_status={apply_result.get('status')}")
    print(f"planned={apply_result.get('planned_counts')}")
    if report.get("verify") and not report["verify"].get("skipped"):
        print(f"verify_all_passed={report['verify'].get('all_passed')}")
        print(f"verify_checks={report['verify'].get('checks')}")
    return 0 if apply_result.get("status") in {"applied", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
