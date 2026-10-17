"""Identity Collision Recovery — Phase 3 apply (APPROVE only).

Mutates DB + .info only for plan items with ``manual_decision=APPROVE``.

Hard rules
----------
- Never process empty / REJECT / missing decisions.
- Never guess keep/split from folder names.
- Never call Sync / Import / Reconcile as part of apply.
- SPLIT: keep path retains old internal_id; each split path gets a new entity.

Usage::

    python tools/identity_collision_apply.py --plan PATH --dry-run
    python tools/identity_collision_apply.py --plan PATH --apply --confirm
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.identity_collision_audit import run_audit  # noqa: E402
from tools.identity_collision_common import (  # noqa: E402
    CODE_INTERNAL_ID_COLLISION,
    DECISION_APPROVE,
    app_id_for_nexus_slug,
    default_paths,
    dump_json,
    load_json,
    nexus_mod_id_from_url,
    nexus_url_game_slug,
    norm_path,
    now_utc,
    read_info,
    text,
    write_info_patch,
)


def _fields_from_info(folder: Path) -> dict[str, Any] | None:
    """Extract registration fields from .info evidence only."""
    payload, info_path = read_info(folder)
    if payload is None or payload.get("_read_error"):
        return None
    url = text(payload.get("url") or payload.get("source_url"))
    platform = text(payload.get("platform") or payload.get("source_type")) or "nexus"
    try:
        app_id = int(payload.get("app_id") or 0)
    except (TypeError, ValueError):
        app_id = 0
    if app_id <= 0:
        app_id = app_id_for_nexus_slug(nexus_url_game_slug(url))
    external_id = text(payload.get("external_id") or payload.get("published_file_id"))
    if not external_id:
        external_id = nexus_mod_id_from_url(url)
    workspace_id = text(payload.get("workspace_id")) or external_id
    title = text(payload.get("title") or payload.get("display_name"))
    if app_id <= 0 or not external_id or not url:
        return None
    return {
        "info_path": info_path,
        "platform": platform,
        "app_id": app_id,
        "external_id": external_id,
        "workspace_id": workspace_id,
        "source_url": url,
        "title": title,
    }


def _resolve_keep_split(item: dict[str, Any]) -> tuple[str, list[str], str]:
    keep = text(item.get("keep_path"))
    splits = [text(p) for p in (item.get("split_paths") or []) if text(p)]
    if keep and splits:
        return keep, splits, ""
    # Allow using explicit suggestions only when human already APPROVED and
    # filled keep_path; never invent from folder names.
    return keep, splits, "keep_path and split_paths must be set explicitly"


def apply_plan(
    plan: dict[str, Any],
    *,
    db_path: Path,
    library: Path,
    apply: bool,
    confirm: bool,
    rollback_dir: Path,
    db=None,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    if not apply:
        for item in plan.get("items") or []:
            decision = text(item.get("manual_decision")).upper()
            results.append(
                {
                    "finding_code": item.get("finding_code"),
                    "old_internal_id": item.get("old_internal_id"),
                    "would_apply": (
                        decision == DECISION_APPROVE
                        and item.get("finding_code") == CODE_INTERNAL_ID_COLLISION
                    ),
                    "manual_decision": decision,
                    "dry_run": True,
                }
            )
        return {
            "phase": "collision_apply",
            "applied": False,
            "reason": "dry-run (pass --apply --confirm to mutate)",
            "generated_at": now_utc(),
            "results": results,
        }
    if not confirm:
        raise SystemExit("refusing apply without --confirm")

    from core.db_manager import DatabaseManager
    from services.identity_service import identity_create_scope

    owns_db = db is None
    if owns_db:
        DatabaseManager.reset_instance()
        db = DatabaseManager.instance(db_path)
    rollback_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    case_dir = rollback_dir / f"collision_split_{stamp}"
    case_dir.mkdir(parents=True, exist_ok=True)
    if Path(db_path).is_file():
        shutil.copy2(db_path, case_dir / "mod_manager.db.bak")

    for item in plan.get("items") or []:
        code = text(item.get("finding_code"))
        decision = text(item.get("manual_decision")).upper()
        if decision != DECISION_APPROVE:
            results.append(
                {
                    "finding_code": code,
                    "applied": False,
                    "skipped": True,
                    "reason": f"manual_decision={decision!r} (need APPROVE)",
                    "old_internal_id": item.get("old_internal_id"),
                }
            )
            continue
        if code != CODE_INTERNAL_ID_COLLISION:
            results.append(
                {
                    "finding_code": code,
                    "applied": False,
                    "skipped": True,
                    "reason": "only INTERNAL_ID_COLLISION splits are applied",
                    "old_internal_id": item.get("old_internal_id"),
                }
            )
            continue

        keep_path_s, split_paths, err = _resolve_keep_split(item)
        if err or not keep_path_s or not split_paths:
            results.append(
                {
                    "finding_code": code,
                    "applied": False,
                    "error": err or "missing keep_path/split_paths",
                    "old_internal_id": item.get("old_internal_id"),
                }
            )
            continue

        old_iid = text(item.get("old_internal_id"))
        db_rec = dict(item.get("db_record") or {})
        mid = text(db_rec.get("mod_id"))
        if not mid.isdigit():
            # Resolve by internal_id column.
            found = db.find_mod_by_internal_id(old_iid)
            mid = text(found) if found else ""
        if not mid.isdigit() and old_iid.isdigit() and db.get_mod(old_iid) is not None:
            mid = old_iid
        if not mid.isdigit():
            results.append(
                {
                    "finding_code": code,
                    "applied": False,
                    "error": "cannot resolve DB mod_id for keep entity",
                    "old_internal_id": old_iid,
                }
            )
            continue

        keep_folder = Path(keep_path_s)
        if not keep_folder.is_dir():
            results.append(
                {
                    "finding_code": code,
                    "applied": False,
                    "error": f"keep_path missing: {keep_path_s}",
                    "old_internal_id": old_iid,
                }
            )
            continue

        # Snapshot keep .info
        snap_keep = case_dir / "keep" / keep_folder.name
        snap_keep.mkdir(parents=True, exist_ok=True)
        if (keep_folder / ".info").is_dir():
            shutil.copytree(
                keep_folder / ".info", snap_keep / ".info", dirs_exist_ok=True
            )

        keep_fields = _fields_from_info(keep_folder) or {}
        proof_iid = old_iid or mid
        db.update_mod_identity_fields(
            mid,
            internal_id=proof_iid,
            last_known_path=str(keep_folder.resolve()),
            folder_present=True,
            app_id=int(keep_fields.get("app_id") or db_rec.get("app_id") or 0) or None,
            platform=text(keep_fields.get("platform") or db_rec.get("platform")) or None,
            external_id=text(keep_fields.get("external_id") or db_rec.get("external_id"))
            or None,
            source_url=text(keep_fields.get("source_url") or db_rec.get("source_url"))
            or None,
        )
        keep_title = text(keep_fields.get("title") or db_rec.get("title"))
        if keep_title:
            with db._lock:
                db._conn.execute(
                    "UPDATE mods SET title = ?, display_name = ? WHERE mod_id = ?",
                    (keep_title, keep_title, int(mid)),
                )
                db._conn.commit()

        keep_info = Path(
            text(keep_fields.get("info_path"))
            or str(keep_folder / ".info" / "metadata.json")
        )
        write_info_patch(
            keep_info,
            {
                "internal_id": proof_iid,
                "workspace_id": text(
                    keep_fields.get("workspace_id") or db_rec.get("workspace_id")
                )
                or None,
                "external_id": text(
                    keep_fields.get("external_id") or db_rec.get("external_id")
                )
                or None,
                "platform": text(keep_fields.get("platform") or db_rec.get("platform"))
                or None,
                "app_id": int(keep_fields.get("app_id") or db_rec.get("app_id") or 0)
                or None,
                "title": keep_title or None,
                "url": text(keep_fields.get("source_url") or db_rec.get("source_url"))
                or None,
            },
        )

        created: list[dict[str, Any]] = []
        for split_s in split_paths:
            folder = Path(split_s)
            if norm_path(folder) == norm_path(keep_folder):
                created.append({"skipped": True, "reason": "same as keep_path", "path": split_s})
                continue
            if not folder.is_dir():
                created.append({"error": "split path missing", "path": split_s})
                continue
            fields = _fields_from_info(folder)
            if fields is None:
                created.append(
                    {
                        "error": "insufficient .info evidence (need url+app_id+external_id)",
                        "path": split_s,
                    }
                )
                continue

            # Skip if an entity already exists for this platform scope.
            existing = db.find_mod_by_external(
                fields["platform"], fields["external_id"], app_id=int(fields["app_id"])
            )
            if existing is not None:
                created.append(
                    {
                        "skipped": True,
                        "reason": "entity already exists for platform scope",
                        "mod_id": str(existing.mod_id),
                        "path": split_s,
                    }
                )
                continue

            with identity_create_scope():
                new_mid = int(db.allocate_mod_id())
            new_iid = str(new_mid)
            with db._lock:
                db._conn.execute(
                    """
                    UPDATE mods SET
                        app_id = ?,
                        platform = ?,
                        external_id = ?,
                        workspace_id = ?,
                        internal_id = ?,
                        title = ?,
                        display_name = ?,
                        source_url = ?,
                        last_known_path = ?,
                        folder_present = 1,
                        source_type = ?
                    WHERE mod_id = ?
                    """,
                    (
                        int(fields["app_id"]),
                        fields["platform"],
                        fields["external_id"],
                        fields["workspace_id"],
                        new_iid,
                        fields["title"] or f"Mod {fields['external_id']}",
                        fields["title"] or f"Mod {fields['external_id']}",
                        fields["source_url"],
                        str(folder.resolve()),
                        fields["platform"],
                        new_mid,
                    ),
                )
                db._conn.commit()

            snap = case_dir / "split" / f"{new_mid}_{folder.name}"
            snap.mkdir(parents=True, exist_ok=True)
            if (folder / ".info").is_dir():
                shutil.copytree(folder / ".info", snap / ".info", dirs_exist_ok=True)

            meta = Path(fields["info_path"])
            write_info_patch(
                meta,
                {
                    "internal_id": new_iid,
                    "workspace_id": fields["workspace_id"],
                    "external_id": fields["external_id"],
                    "platform": fields["platform"],
                    "app_id": int(fields["app_id"]),
                    "title": fields["title"] or None,
                    "url": fields["source_url"],
                },
            )
            created.append(
                {
                    "mod_id": str(new_mid),
                    "internal_id": new_iid,
                    "path": str(folder.resolve()),
                    "app_id": int(fields["app_id"]),
                    "external_id": fields["external_id"],
                    "title": fields["title"],
                }
            )

        results.append(
            {
                "finding_code": code,
                "applied": True,
                "old_internal_id": proof_iid,
                "keep": {
                    "mod_id": mid,
                    "internal_id": proof_iid,
                    "path": str(keep_folder.resolve()),
                    "title": keep_title,
                },
                "created": created,
                "rollback": str(case_dir),
            }
        )

    # Re-audit after mutations (read-only verification).
    post = run_audit(db_path=db_path, library=library)
    return {
        "phase": "collision_apply",
        "applied": True,
        "generated_at": now_utc(),
        "db_path": str(db_path.resolve()),
        "library": str(library.resolve()),
        "rollback_dir": str(case_dir),
        "results": results,
        "post_audit_counts": post.get("counts"),
        "post_audit_internal_collisions": sum(
            1
            for f in post.get("findings") or []
            if text(f.get("code")) == CODE_INTERNAL_ID_COLLISION
        ),
    }


def main(argv: list[str] | None = None) -> int:
    db_default, lib_default, out_dir = default_paths(ROOT)
    parser = argparse.ArgumentParser(description="Identity collision recovery apply")
    parser.add_argument(
        "--plan",
        type=Path,
        default=out_dir / "identity_collision_recovery_plan.json",
    )
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--library", type=Path, default=None)
    parser.add_argument(
        "--rollback-dir",
        type=Path,
        default=out_dir / "collision_rollback",
    )
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--confirm", action="store_true", default=False)
    parser.add_argument(
        "--out",
        type=Path,
        default=out_dir / "identity_collision_apply_result.json",
    )
    args = parser.parse_args(argv)
    plan = load_json(args.plan)
    db_path = Path(args.db or plan.get("db_path") or db_default)
    library = Path(args.library or plan.get("library") or lib_default)
    do_apply = bool(args.apply) and not bool(args.dry_run)
    result = apply_plan(
        plan,
        db_path=db_path,
        library=library,
        apply=do_apply,
        confirm=bool(args.confirm),
        rollback_dir=args.rollback_dir,
    )
    dump_json(args.out, result)
    print(f"wrote {args.out}")
    print(f"applied={result.get('applied')} results={len(result.get('results') or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
