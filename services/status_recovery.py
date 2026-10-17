"""One-shot Status Recovery — history cleanup + authoritative recompute.

ARCHITECTURE RULE
-----------------
Runs once per database (``schema_flags.status_recovery_v1``).

Does **not** wipe::

  - user ``conflict_status`` / favorite / invalid / abandoned
  - ``mods.deploy_status`` / deploy_time / deploy_path / deploy_error
  - Deployment Record tables or memory overlays (记录缺失 / 额外部署)

Does::

  1. Move identity pollution off ``content_status`` / ``library_status``
     onto ``identity_status``.
  2. Re-evaluate ``content_status`` via ``content_status_eval`` when a
     library root is available.
  3. Recompute multi-folder ``identity_status``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.status_authority import (
    CONTENT_HEALTHY,
    IDENTITY_STATUS_CONFLICT,
    IDENTITY_STATUS_UNRESOLVED,
    STATUS_MODEL_CLEANUP_V2_FLAG,
    STATUS_RECOVERY_FLAG,
    normalize_content_axis,
    normalize_identity_status,
)

logger = logging.getLogger(__name__)

# ONLY real identity tokens may be peeled from content/library → identity_status.
# Never treat library_status=normal/missing/imported as identity (that polluted all rows).
_LEGACY_IDENTITY_TOKENS = frozenset(
    {
        "identity_conflict",
        "identity_unresolved",
        "IDENTITY_CONFLICT",
        "IDENTITY_UNRESOLVED",
        "unresolved",
    }
)

@dataclass
class StatusRecoveryResult:
    scanned: int = 0
    identity_migrated: int = 0
    content_reevaluated: int = 0
    library_cleared: int = 0
    skipped_user_conflict: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "identity_migrated": self.identity_migrated,
            "content_reevaluated": self.content_reevaluated,
            "library_cleared": self.library_cleared,
            "skipped_user_conflict": self.skipped_user_conflict,
            "notes": list(self.notes),
        }


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _flag_done(db: Any, flag: str) -> bool:
    row = db._conn.execute(  # noqa: SLF001
        "SELECT 1 FROM schema_flags WHERE flag = ?", (flag,)
    ).fetchone()
    return row is not None


def _mark_flag(db: Any, flag: str) -> None:
    db._conn.execute(  # noqa: SLF001
        """
        CREATE TABLE IF NOT EXISTS schema_flags (
            flag TEXT PRIMARY KEY NOT NULL,
            applied_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    db._conn.execute(  # noqa: SLF001
        "INSERT OR REPLACE INTO schema_flags (flag, applied_at) VALUES (?, ?)",
        (flag, _utc_now()),
    )


def _cols(db: Any) -> set[str]:
    return {
        str(row[1])
        for row in db._conn.execute("PRAGMA table_info(mods)").fetchall()  # noqa: SLF001
    }


def migrate_identity_pollution_from_content(db: Any) -> StatusRecoveryResult:
    """
    DB-only phase: peel identity tokens off content/library into identity_status.

    Safe without a library root. Does not touch ``conflict_status``.
    """
    result = StatusRecoveryResult()
    cols = _cols(db)
    if "identity_status" not in cols:
        result.notes.append("identity_status column missing")
        return result

    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            """
            SELECT mod_id, content_status, library_status, identity_status,
                   conflict_status, folder_present
            FROM mods
            """
        ).fetchall()
        result.scanned = len(rows)
        for row in rows:
            mid = int(row["mod_id"])
            cs_raw = str(row["content_status"] or "").strip()
            ls_raw = str(row["library_status"] or "").strip()
            id_raw = str(row["identity_status"] or "").strip()
            user_conflict = str(row["conflict_status"] or "").strip().lower()
            if user_conflict and user_conflict not in {"", "none"}:
                result.skipped_user_conflict += 1

            identity = normalize_identity_status(id_raw)
            content_is_identity = cs_raw.lower() in {
                t.lower() for t in _LEGACY_IDENTITY_TOKENS
            }
            library_is_identity = ls_raw in _LEGACY_IDENTITY_TOKENS or ls_raw.lower() in {
                "identity_conflict",
                "identity_unresolved",
            }
            # library_status=conflict was illegal Mod-status pollution — clear it,
            # but do NOT stamp identity_status from it.
            library_is_illegal_conflict = ls_raw.lower() == "conflict"

            changed = False
            new_identity = identity
            if content_is_identity or library_is_identity:
                if ls_raw in {"IDENTITY_UNRESOLVED", "identity_unresolved", "unresolved"} or (
                    cs_raw.lower() in {"identity_unresolved", "unresolved"}
                ):
                    new_identity = IDENTITY_STATUS_UNRESOLVED
                else:
                    new_identity = IDENTITY_STATUS_CONFLICT
                if new_identity != identity:
                    changed = True
                    result.identity_migrated += 1

            new_content = normalize_content_axis(cs_raw)
            if content_is_identity:
                # Peel identity off content — leave healthy until FS re-eval
                new_content = CONTENT_HEALTHY
                changed = True
            elif cs_raw != new_content:
                # blank / file_missing / unknown → canonical content axis
                changed = True

            new_library = ls_raw
            if library_is_identity or library_is_illegal_conflict:
                new_library = "normal" if new_content == CONTENT_HEALTHY else "missing"
                changed = True
                result.library_cleared += 1

            if not changed and new_identity == identity:
                continue

            # Never SET deploy_status / conflict_status / updated_at here.
            # Library sort authority (updated_at) must not move on status peel.
            db._conn.execute(  # noqa: SLF001
                """
                UPDATE mods SET
                    identity_status = ?,
                    content_status = ?,
                    library_status = ?
                WHERE mod_id = ?
                """,
                (new_identity, new_content, new_library, mid),
            )
        db._conn.commit()  # noqa: SLF001
    return result


def reevaluate_content_statuses(
    db: Any,
    library_root: str | Path | None,
) -> StatusRecoveryResult:
    """Filesystem phase: authoritative content_status for every Mod row."""
    from services.content_status_eval import persist_evaluated_content_status
    from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME

    result = StatusRecoveryResult()
    root = Path(library_root) if library_root else None
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            """
            SELECT mod_id, last_known_path, folder_present, backup_status
            FROM mods
            """
        ).fetchall()
    result.scanned = len(rows)
    for row in rows:
        mid = str(row["mod_id"])
        lkp = str(row["last_known_path"] or "").strip()
        path = Path(lkp) if lkp else None
        present = bool(path is not None and path.is_dir())
        if not present and root is not None:
            # best-effort: skip if no path
            pass
        meta_missing = False
        if present and path is not None:
            meta_missing = not (path / INFO_DIR_NAME / METADATA_FILENAME).is_file()
        try:
            persist_evaluated_content_status(
                mid,
                path if present else None,
                db=db,
                folder_present=present,
                backup_status=str(row["backup_status"] or ""),
                metadata_missing=meta_missing,
                sync_sticky_marker=True,
                touch_updated_at=False,
            )
            result.content_reevaluated += 1
        except Exception:  # noqa: BLE001
            logger.debug("content reeval failed for %s", mid, exc_info=True)
    return result


def reevaluate_identity_multi_folder(
    db: Any,
    library_root: str | Path | None,
) -> StatusRecoveryResult:
    """Mark identity_status=identity_conflict when one mod_id has multiple live folders."""
    from services.file_ops import ModFileManager, read_info_metadata_dict
    from services.mod_identity import ensure_mod_identity

    result = StatusRecoveryResult()
    if library_root is None:
        result.notes.append("no library_root for identity reeval")
        return result
    root = Path(library_root)
    if not root.is_dir():
        result.notes.append("library_root missing")
        return result

    id_to_paths: dict[str, list[Path]] = {}
    try:
        manager = ModFileManager(root)
        for folder in manager.list_managed_mods():
            raw = dict(read_info_metadata_dict(folder) or {})
            mid, _payload, _changed = ensure_mod_identity(folder, raw, db=db)
            if not str(mid).isdigit():
                continue
            id_to_paths.setdefault(str(mid), []).append(Path(folder))
    except Exception:  # noqa: BLE001
        logger.debug("list_managed_mods failed during identity recovery", exc_info=True)
        return result

    conflict_ids = {
        mid
        for mid, paths in id_to_paths.items()
        if len([p for p in paths if p.is_dir()]) > 1
    }
    result.scanned = len(id_to_paths)
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT mod_id, identity_status FROM mods"
        ).fetchall()
        for row in rows:
            mid = str(row["mod_id"])
            cur = normalize_identity_status(row["identity_status"])
            if mid in conflict_ids:
                if cur != IDENTITY_STATUS_CONFLICT:
                    db._conn.execute(  # noqa: SLF001
                        """
                        UPDATE mods SET identity_status = ?
                        WHERE mod_id = ?
                        """,
                        (IDENTITY_STATUS_CONFLICT, int(mid)),
                    )
                    result.identity_migrated += 1
            elif cur == IDENTITY_STATUS_CONFLICT:
                # Pollution peel may have set identity_conflict; clear when
                # filesystem identity recompute finds no multi-folder fact.
                from services.status_authority import IDENTITY_STATUS_OK

                db._conn.execute(  # noqa: SLF001
                    """
                    UPDATE mods SET identity_status = ?
                    WHERE mod_id = ?
                    """,
                    (IDENTITY_STATUS_OK, int(mid)),
                )
                result.identity_migrated += 1
        db._conn.commit()  # noqa: SLF001
    return result


def run_status_model_cleanup_v2(
    db: Any,
    library_root: str | Path | None = None,
    *,
    force: bool = False,
) -> StatusRecoveryResult:
    """
    One-shot deletion of the old Mod status model.

    Re-evaluates every row's ``content_status`` via ``content_status_eval``
    onto ``healthy`` | ``content_missing``. Does **not** map
    ``folder_missing`` → a new token.

    Never touches ``conflict_status`` or ``deploy_status``.
    """
    merged = StatusRecoveryResult()
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            """
            CREATE TABLE IF NOT EXISTS schema_flags (
                flag TEXT PRIMARY KEY NOT NULL,
                applied_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        if not force and _flag_done(db, STATUS_MODEL_CLEANUP_V2_FLAG):
            merged.notes.append("cleanup_v2_already_applied")
            return merged
        deploy_before = {
            int(r["mod_id"]): (
                str(r["deploy_status"] or ""),
                str(r["deploy_path"] or ""),
                str(r["deploy_error"] or ""),
            )
            for r in db._conn.execute(  # noqa: SLF001
                "SELECT mod_id, deploy_status, deploy_path, deploy_error FROM mods"
            ).fetchall()
        }
        conflict_before = {
            int(r["mod_id"]): str(r["conflict_status"] or "")
            for r in db._conn.execute(  # noqa: SLF001
                "SELECT mod_id, conflict_status FROM mods"
            ).fetchall()
        }

    # Peel any remaining identity pollution first.
    phase1 = migrate_identity_pollution_from_content(db)
    merged.identity_migrated += phase1.identity_migrated
    merged.library_cleared += phase1.library_cleared
    merged.notes.extend(phase1.notes)

    # Force-clear deleted content_status tokens to healthy before FS re-eval
    # so writers only see legal values after cleanup.
    with db._lock:  # noqa: SLF001
        deleted = sorted(
            {
                "folder_missing",
                "metadata_missing",
                "backup_invalid",
                "file_missing",
                "identity_conflict",
                "conflict",
                "identity_unresolved",
                "unknown",
                "missing",
                "normal",
                "imported",
            }
        )
        placeholders = ",".join("?" * len(deleted))
        cur = db._conn.execute(  # noqa: SLF001
            f"""
            UPDATE mods SET
                content_status = 'healthy',
                library_status = CASE
                    WHEN LOWER(TRIM(COALESCE(library_status, ''))) IN (
                        'missing', 'backup_invalid', 'content_missing',
                        'folder_missing', 'conflict', 'identity_conflict'
                    ) THEN 'normal'
                    ELSE library_status
                END
            WHERE LOWER(TRIM(COALESCE(content_status, ''))) IN ({placeholders})
            """,
            tuple(deleted),
        )
        cleared = int(cur.rowcount or 0)
        db._conn.commit()  # noqa: SLF001
        if cleared:
            merged.notes.append(f"cleared_deleted_content_tokens={cleared}")

    if library_root is not None:
        phase2 = reevaluate_content_statuses(db, library_root)
        merged.scanned = phase2.scanned
        merged.content_reevaluated += phase2.content_reevaluated
        merged.notes.extend(phase2.notes)

    with db._lock:  # noqa: SLF001
        deploy_after = {
            int(r["mod_id"]): (
                str(r["deploy_status"] or ""),
                str(r["deploy_path"] or ""),
                str(r["deploy_error"] or ""),
            )
            for r in db._conn.execute(  # noqa: SLF001
                "SELECT mod_id, deploy_status, deploy_path, deploy_error FROM mods"
            ).fetchall()
        }
        conflict_after = {
            int(r["mod_id"]): str(r["conflict_status"] or "")
            for r in db._conn.execute(  # noqa: SLF001
                "SELECT mod_id, conflict_status FROM mods"
            ).fetchall()
        }
        if deploy_before != deploy_after:
            raise RuntimeError(
                "status_model_cleanup_v2 mutated deploy_status — forbidden"
            )
        if conflict_before != conflict_after:
            raise RuntimeError(
                "status_model_cleanup_v2 mutated conflict_status — forbidden"
            )
        _mark_flag(db, STATUS_MODEL_CLEANUP_V2_FLAG)
        db._conn.commit()  # noqa: SLF001
    logger.info(
        "status_model_cleanup_v2 done content_reevaluated=%s notes=%s",
        merged.content_reevaluated,
        merged.notes,
    )
    return merged


def run_status_recovery(
    db: Any,
    library_root: str | Path | None = None,
    *,
    force: bool = False,
) -> StatusRecoveryResult:
    """
    Full recovery. Idempotent unless ``force=True``.

    ``conflict_status`` and ``deploy_status`` are never modified.
    Also applies ``status_model_cleanup_v2`` when not yet flagged.
    """
    merged = StatusRecoveryResult()
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            """
            CREATE TABLE IF NOT EXISTS schema_flags (
                flag TEXT PRIMARY KEY NOT NULL,
                applied_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        if not force and _flag_done(db, STATUS_RECOVERY_FLAG):
            # Still run v2 cleanup once if pending.
            cleanup = run_status_model_cleanup_v2(
                db, library_root, force=False
            )
            merged.notes.extend(cleanup.notes)
            merged.content_reevaluated += cleanup.content_reevaluated
            merged.notes.append("already_applied")
            return merged
        # Snapshot deploy outcomes — recovery must leave them byte-identical.
        deploy_before = {
            int(r["mod_id"]): (
                str(r["deploy_status"] or ""),
                str(r["deploy_path"] or ""),
                str(r["deploy_error"] or ""),
            )
            for r in db._conn.execute(  # noqa: SLF001
                "SELECT mod_id, deploy_status, deploy_path, deploy_error FROM mods"
            ).fetchall()
        }

    phase1 = migrate_identity_pollution_from_content(db)
    merged.scanned = phase1.scanned
    merged.identity_migrated += phase1.identity_migrated
    merged.library_cleared += phase1.library_cleared
    merged.skipped_user_conflict += phase1.skipped_user_conflict
    merged.notes.extend(phase1.notes)

    if library_root is not None:
        phase2 = reevaluate_content_statuses(db, library_root)
        merged.content_reevaluated += phase2.content_reevaluated
        merged.notes.extend(phase2.notes)
        phase3 = reevaluate_identity_multi_folder(db, library_root)
        merged.identity_migrated += phase3.identity_migrated
        merged.notes.extend(phase3.notes)

    with db._lock:  # noqa: SLF001
        deploy_after = {
            int(r["mod_id"]): (
                str(r["deploy_status"] or ""),
                str(r["deploy_path"] or ""),
                str(r["deploy_error"] or ""),
            )
            for r in db._conn.execute(  # noqa: SLF001
                "SELECT mod_id, deploy_status, deploy_path, deploy_error FROM mods"
            ).fetchall()
        }
        if deploy_before != deploy_after:
            raise RuntimeError(
                "status_recovery mutated deploy_status — forbidden"
            )
        _mark_flag(db, STATUS_RECOVERY_FLAG)
        db._conn.commit()  # noqa: SLF001

    cleanup = run_status_model_cleanup_v2(db, library_root, force=force)
    merged.content_reevaluated += cleanup.content_reevaluated
    merged.notes.extend(cleanup.notes)

    logger.info(
        "status_recovery done scanned=%s identity=%s content=%s library_cleared=%s",
        merged.scanned,
        merged.identity_migrated,
        merged.content_reevaluated,
        merged.library_cleared,
    )
    return merged
