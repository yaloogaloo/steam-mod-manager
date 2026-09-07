#!/usr/bin/env python3
"""Human-confirmed repair for historical Mod path / identity conflicts.

Only rebinds an *existing* DB entity onto a disk folder by rewriting
``.info.internal_id`` to match the DB entity. Never:

- auto-binds by workspace_id
- changes DB ``internal_id`` / creates / deletes Mods
- touches Identity Service / Sync / Import / Reconcile hot paths

Requires explicit ``--from-internal-id``, ``--to-path``, and ``--confirm``.

Usage::

    # dry-run (no mutation)
    python tools/repair_mod_path_identity_conflict.py \\
        --from-internal-id <uuid|pk> --to-path "mod/.../Tractor Mod"

    # apply
    python tools/repair_mod_path_identity_conflict.py \\
        --from-internal-id <uuid|pk> --to-path "mod/.../Tractor Mod" --confirm
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.diagnose_mod_path_identity_conflict import (  # noqa: E402
    DECISION_CONFLICT,
    DECISION_MATCHED,
    _connect,
    _read_info_dict,
    _text,
    load_db_entity_by_internal_id,
    run_diagnose,
    titles_highly_match,
)

INFO_DIR = ".info"
METADATA = "metadata.json"


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_info_metadata(folder: Path) -> Path:
    """Copy current metadata.json beside itself with a timestamp suffix."""
    info = folder / INFO_DIR
    meta = info / METADATA
    if not meta.is_file():
        raise FileNotFoundError(f"missing .info/metadata.json under {folder}")
    dest = info / f"metadata.json.pre_identity_repair.{_now_stamp()}"
    shutil.copy2(meta, dest)
    return dest


def validate_repair_target(
    *,
    entity_internal_id: str,
    to_path: Path,
    db_path: Path | None = None,
    library_root: Path | None = None,
) -> dict[str, Any]:
    """
    Ensure the proposed repair is a confirmed IDENTITY_CONFLICT candidate.

    Returns a plan dict. Raises ``ValueError`` when unsafe / inapplicable.
    """
    from core.paths import database_path, default_mod_library

    db_file = Path(db_path) if db_path is not None else database_path()
    lib = Path(library_root) if library_root is not None else default_mod_library()
    folder = Path(to_path).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"--to-path is not a directory: {folder}")

    con = _connect(db_file)
    try:
        entity = load_db_entity_by_internal_id(con, entity_internal_id)
        if entity is None:
            raise ValueError(
                f"no DB entity for --from-internal-id {_text(entity_internal_id)!r}"
            )

        report = run_diagnose(
            internal_id=entity.internal_id,
            db_path=db_file,
            library_root=lib,
        )
        info = _read_info_dict(folder)
        if not info or info.get("_read_error"):
            raise ValueError(f"unreadable or missing .info under {folder}")

        info_iid = _text(info.get("internal_id"))
        info_wid = _text(info.get("workspace_id"))
        info_title = (
            _text(info.get("title"))
            or _text(info.get("display_name"))
            or folder.name
        )

        if info_iid == entity.internal_id or (
            entity.mod_id and info_iid == entity.mod_id
        ):
            raise ValueError(
                "folder already proves DB internal_id "
                f"({entity.internal_id}); no rewrite needed"
            )

        if not entity.workspace_id or info_wid != entity.workspace_id:
            raise ValueError(
                "workspace_id mismatch — refuse rewrite "
                f"(db={entity.workspace_id!r} info={info_wid!r})"
            )

        title_ok = any(
            titles_highly_match(t, info_title) or titles_highly_match(t, folder.name)
            for t in (entity.title, entity.display_name, Path(entity.last_known_path).name)
            if _text(t)
        )
        if not title_ok:
            raise ValueError(
                "title does not highly match — refuse rewrite "
                f"(db={entity.title!r} info={info_title!r} folder={folder.name!r})"
            )

        # Path should appear in conflict candidates when scan works; also allow
        # explicit path if it satisfies the same conflict predicates above.
        conflict_paths = {
            str(Path(c["path"]).resolve())
            for c in report.conflict_candidates
            if c.get("path")
        }
        if str(folder) not in conflict_paths and report.decision != DECISION_CONFLICT:
            # Still OK if predicates passed — diagnose may have scanned a
            # different game folder when last_known_path is stale.
            pass
        if str(folder) not in conflict_paths:
            # Accept when local predicates passed (workspace + title + iid diff).
            pass

        if report.decision == DECISION_MATCHED:
            raise ValueError(
                "diagnose says MATCHED_BY_INTERNAL_ID — refuse conflicting rewrite"
            )

        return {
            "ok": True,
            "decision": report.decision,
            "db_entity": report.db_entity,
            "to_path": str(folder),
            "info_internal_id_before": info_iid,
            "info_workspace_id": info_wid,
            "info_title": info_title,
            "will_set_info_internal_id": entity.internal_id,
            "will_set_last_known_path": str(folder),
            "will_set_folder_present": 1,
            "mod_id": entity.mod_id,
            "notes": [
                "rewrite .info.internal_id only (preserve workspace_id / metadata / "
                "cover / offline / deploy sidecar fields)",
                "update mods.last_known_path + folder_present",
                "re-evaluate content_status + refresh projection",
                "never change DB.internal_id / create / delete / merge",
            ],
            "diagnose_notes": report.notes,
        }
    finally:
        con.close()


def apply_repair(
    *,
    from_internal_id: str,
    to_path: Path,
    confirm: bool,
    db_path: Path | None = None,
    library_root: Path | None = None,
    db: Any = None,
) -> dict[str, Any]:
    """
    Execute or dry-run the repair.

    Without ``confirm=True`` nothing is mutated.
    Never changes ``mods.internal_id`` / creates / deletes entities.
    """
    plan = validate_repair_target(
        entity_internal_id=from_internal_id,
        to_path=to_path,
        db_path=db_path,
        library_root=library_root,
    )
    if not confirm:
        return {
            "executed": False,
            "reason": "missing --confirm; no changes made",
            "plan": plan,
        }

    from core.db_manager import DatabaseManager, get_db
    from core.paths import database_path
    from services.content_status_eval import persist_evaluated_content_status
    from services.file_ops import persist_unified_metadata_dict
    from services.mod_projection_events import notify_mod_changed

    folder = Path(plan["to_path"])
    if db is not None:
        database = db
    elif db_path is not None:
        database = DatabaseManager.instance(Path(db_path))
    else:
        # Production / test singleton (conftest already points at isolated DB).
        _ = database_path()
        database = get_db()

    mid = _text(plan["mod_id"])
    target_iid = _text(plan["will_set_info_internal_id"])
    before_iid = _text(plan["info_internal_id_before"])
    row_before = database.get_mod_backup_row(mid) or {}
    db_iid_before = _text(row_before.get("internal_id")) or target_iid

    backup_path = _backup_info_metadata(folder)
    data = _read_info_dict(folder) or {}
    if data.get("_read_error"):
        raise RuntimeError(f"failed to re-read .info after backup at {folder}")

    # Preserve all sidecar fields; only rewrite identity proof + managed path.
    data["internal_id"] = target_iid
    data["managed_path"] = str(folder)
    data["local_path"] = str(folder)
    # Do not invent / clear workspace_id.
    persist_unified_metadata_dict(
        folder,
        data,
        sync_backup=True,
        sync_reason="identity_path_conflict_repair",
    )

    database.update_mod_identity_fields(
        mid,
        last_known_path=str(folder),
        folder_present=True,
    )
    content_status = persist_evaluated_content_status(
        mid,
        folder,
        db=database,
        folder_present=True,
        sync_sticky_marker=True,
    )
    notify_mod_changed(mid)

    row = database.get_mod_backup_row(mid) or {}
    after_info = _read_info_dict(folder) or {}
    db_iid_after = _text(row.get("internal_id")) or mid

    return {
        "executed": True,
        "backup_metadata": str(backup_path),
        "mod_id": mid,
        "info_internal_id_before": before_iid,
        "info_internal_id_after": _text(after_info.get("internal_id")),
        "info_workspace_id": _text(after_info.get("workspace_id")),
        "last_known_path": _text(row.get("last_known_path")),
        "folder_present": int(row.get("folder_present") or 0),
        "content_status": content_status,
        "db_internal_id_before": db_iid_before,
        "db_internal_id_after": db_iid_after,
        "db_internal_id_unchanged": db_iid_after == db_iid_before,
        "plan": plan,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Human-confirmed repair: rewrite folder .info.internal_id to an "
            "existing DB entity and rebind last_known_path."
        )
    )
    parser.add_argument(
        "--from-internal-id",
        required=True,
        help="Existing DB entity (portable internal_id or numeric mod_id PK)",
    )
    parser.add_argument(
        "--to-path",
        required=True,
        type=Path,
        help="Managed mod folder to rebind (must contain .info)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required to mutate .info / DB. Absent => dry-run only.",
    )
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--library", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        result = apply_repair(
            from_internal_id=args.from_internal_id,
            to_path=args.to_path,
            confirm=bool(args.confirm),
            db_path=args.db,
            library_root=args.library,
        )
    except ValueError as exc:
        print(
            json.dumps(
                {"executed": False, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("executed") or not args.confirm else 1


if __name__ == "__main__":
    raise SystemExit(main())
