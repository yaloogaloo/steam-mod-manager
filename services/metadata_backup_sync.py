"""Unified metadata backup sync entry (.info → backup only)."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Literal

from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.metadata_backup import backup_root, sync_metadata_backup

logger = logging.getLogger(__name__)

SyncReason = Literal[
    "import",
    "refresh",
    "edit",
    "cover_change",
    "offline_change",
    "rescan",
    "restore",
    "repair",
]

VALID_REASONS: frozenset[str] = frozenset(
    {
        "import",
        "refresh",
        "edit",
        "cover_change",
        "offline_change",
        "rescan",
        "restore",
        "repair",
    }
)

_rebuild_lock = threading.Lock()
_rebuild_running = False


def sync_after_metadata_change(
    mod_id: int | str | None,
    managed_path: str | Path | None,
    reason: str,
) -> bool:
    """
    Sync ``data/mod_backup/<id>`` from ``.info`` after a metadata write.

    Rules:
    - Mod folder must exist; otherwise backup write is forbidden.
    - Never writes back into ``.info``.
    - Updates SQLite backup status after sync + validate.
    """
    reason_key = str(reason or "").strip() or "rescan"
    if reason_key not in VALID_REASONS:
        logger.debug("Unknown backup sync reason %r; treating as rescan", reason_key)
        reason_key = "rescan"

    root = Path(managed_path) if managed_path else None
    mid = str(mod_id or "").strip()

    if root is None or not root.is_dir():
        logger.info(
            "backup sync skipped (folder missing) mod_id=%s path=%s reason=%s",
            mid or "?",
            root,
            reason_key,
        )
        if mid.isdigit():
            try:
                from services.metadata_backup import mark_missing

                mark_missing(mid)
            except Exception:  # noqa: BLE001
                pass
            _record_status(mid, status="missing")
        return False

    if not mid.isdigit():
        mid = root.name if root.name.isdigit() else ""
        if not mid.isdigit():
            try:
                from services.file_ops import read_info_metadata_dict

                data = read_info_metadata_dict(root) or {}
                mid = str(data.get("published_file_id") or "").strip()
            except Exception:  # noqa: BLE001
                mid = ""

    meta_file = root / INFO_DIR_NAME / METADATA_FILENAME
    if not meta_file.is_file():
        logger.info(
            "backup sync skipped (no .info/metadata.json) path=%s reason=%s",
            root,
            reason_key,
        )
        if mid.isdigit():
            _record_status(mid, status="missing")
        return False

    try:
        sync_metadata_backup(root)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "backup sync failed mod_id=%s path=%s reason=%s: %s",
            mid or "?",
            root,
            reason_key,
            exc,
        )
        if mid.isdigit():
            _record_status(mid, status="invalid")
        return False

    if not mid.isdigit():
        try:
            from services.file_ops import read_info_metadata_dict

            data = read_info_metadata_dict(root) or {}
            mid = str(data.get("published_file_id") or "").strip()
        except Exception:  # noqa: BLE001
            mid = root.name if root.name.isdigit() else ""

    if mid.isdigit():
        try:
            from services.metadata_backup_validator import (
                status_from_validation,
                validate_backup,
            )

            result = validate_backup(mid)
            status = status_from_validation(result)
            _record_status(mid, status=status, validate=True)
            logger.info(
                "backup synced mod_id=%s reason=%s status=%s issues=%s",
                mid,
                reason_key,
                status,
                result.get("issues") or [],
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("backup validate after sync failed: %s", exc)
            _record_status(mid, status="partial")
    else:
        logger.info(
            "backup synced path=%s reason=%s (mod_id unresolved)",
            root,
            reason_key,
        )
    return True


def rebuild_metadata_backup(
    mod_id: int | str | None,
    managed_path: str | Path | None,
    reason: str = "repair",
) -> bool:
    """
    Explicit backup repair: read ``.info`` and write ``data/mod_backup/<id>``.

    Use for startup backfill, health checks, and migrations — never from Resolver.
    """
    return sync_after_metadata_change(
        mod_id, managed_path, reason if reason in VALID_REASONS else "repair"
    )


def rebuild_missing_metadata_backup(
    library_root: str | Path | None = None,
) -> int:
    """
    Scan managed Mod folders; create backup when ``.info/metadata.json`` exists
    but ``data/mod_backup/<id>`` is missing.

    Returns number of backups created/synced.
    """
    logger.debug("rebuild_missing_metadata_backup enter")
    from core.paths import default_mod_library

    root = Path(library_root) if library_root else Path(default_mod_library())
    if not root.is_dir():
        logger.info("rebuild_missing_metadata_backup: library missing %s", root)
        return 0

    created = 0
    scanned = 0
    for meta_path in root.glob(f"*/*/{INFO_DIR_NAME}/{METADATA_FILENAME}"):
        scanned += 1
        managed = meta_path.parent.parent
        if not managed.is_dir():
            continue
        mid = ""
        try:
            from services.file_ops import read_info_metadata_dict

            data = read_info_metadata_dict(managed) or {}
            mid = str(data.get("published_file_id") or "").strip()
        except Exception:  # noqa: BLE001
            data = {}
        if not mid.isdigit() and managed.name.isdigit():
            mid = managed.name
        if not mid.isdigit():
            continue
        backup_meta = backup_root(mid) / "metadata.json"
        if backup_meta.is_file():
            continue
        if rebuild_metadata_backup(mid, managed, reason="repair"):
            created += 1

    logger.info(
        "rebuild_missing_metadata_backup done scanned=%s created=%s root=%s",
        scanned,
        created,
        root,
    )
    return created


def start_rebuild_missing_metadata_backup_async(
    library_root: str | Path | None = None,
) -> bool:
    """
    Run :func:`rebuild_missing_metadata_backup` on a daemon thread.

    Returns False if a rebuild is already running.
    """
    logger.debug("start_rebuild_missing_metadata_backup_async enter")
    global _rebuild_running
    with _rebuild_lock:
        if _rebuild_running:
            logger.info("rebuild_missing_metadata_backup already running; skip")
            return False
        _rebuild_running = True

    root = str(library_root) if library_root else None

    def _worker() -> None:
        global _rebuild_running
        try:
            rebuild_missing_metadata_backup(root)
        except Exception:  # noqa: BLE001
            logger.exception("rebuild_missing_metadata_backup crashed")
        finally:
            with _rebuild_lock:
                _rebuild_running = False

    thread = threading.Thread(
        target=_worker,
        name="rebuild-missing-metadata-backup",
        daemon=True,
    )
    thread.start()
    logger.info("rebuild_missing_metadata_backup started in background")
    return True


def _record_status(
    mod_id: str,
    *,
    status: str,
    validate: bool = False,
) -> None:
    try:
        from core.db_manager import get_db

        get_db().update_mod_backup_status(
            mod_id,
            status=status,
            touch_validate_at=validate or status in ("complete", "partial", "invalid"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("update_mod_backup_status failed for %s: %s", mod_id, exc)
