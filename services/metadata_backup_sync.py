"""Unified metadata backup sync entry (.info → backup only).

ARCHITECTURE RULE
-----------------
Backup is **not** part of the business critical path. Callers mark dirty and
return; a background worker copies metadata/cover/offline only.

Never backup Mod payloads / Workshop files / hashes / deploy caches.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Literal

from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.metadata_backup import (
    BACKUP_OFFLINE_DIR,
    readable_backup_root,
    sync_metadata_backup,
)
from services.backup_identity import is_frozen_backup_uuid, resolve_backup_mod_pk

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
    "sync",
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
        "sync",
    }
)

# Only explicit repair may run inline. import / restore / edit / refresh / sync /
# reconcile must mark dirty and return (BackupWorker). Use wait=True in tests.
_INLINE_REASONS: frozenset[str] = frozenset({"repair"})

_rebuild_lock = threading.Lock()
_rebuild_running = False
_rebuild_shutdown = False
_rebuild_thread: threading.Thread | None = None

_backup_queue: queue.Queue[tuple[str, str, str]] = queue.Queue()
_backup_worker_lock = threading.Lock()
_backup_worker: threading.Thread | None = None
_backup_worker_stop = False


def _sql_pk(token: str) -> str:
    text = str(token or "").strip()
    if text.isdigit():
        return text
    if is_frozen_backup_uuid(text):
        return resolve_backup_mod_pk(internal_id=text)
    return ""


def _ensure_backup_worker() -> None:
    global _backup_worker, _backup_worker_stop
    with _backup_worker_lock:
        if _backup_worker is not None and _backup_worker.is_alive():
            return
        _backup_worker_stop = False

        def _run() -> None:
            while not _backup_worker_stop:
                try:
                    mid, path, reason = _backup_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                t0 = time.perf_counter()
                try:
                    _sync_backup_now(mid, path, reason)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "backup worker failed mod_id=%s path=%s reason=%s",
                        mid,
                        path,
                        reason,
                    )
                finally:
                    try:
                        from services.library_perf_metrics import (
                            get_library_perf_metrics,
                        )

                        get_library_perf_metrics().record_backup_worker_latency(
                            (time.perf_counter() - t0) * 1000.0
                        )
                        get_library_perf_metrics().record_backup_queue(
                            _backup_queue.qsize()
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    _backup_queue.task_done()

        _backup_worker = threading.Thread(
            target=_run, name="backup-worker", daemon=True
        )
        _backup_worker.start()


def mark_backup_dirty(
    mod_id: int | str | None,
    managed_path: str | Path | None,
    reason: str,
) -> bool:
    """Enqueue backup work and return immediately (user-facing ops)."""
    mid = str(mod_id or "").strip()
    path = str(managed_path or "").strip()
    reason_key = str(reason or "").strip() or "rescan"
    if reason_key not in VALID_REASONS:
        reason_key = "rescan"
    _ensure_backup_worker()
    _backup_queue.put((mid, path, reason_key))
    qsize = _backup_queue.qsize()
    try:
        from services.library_perf_metrics import get_library_perf_metrics

        get_library_perf_metrics().record_backup_queue(qsize)
    except Exception:  # noqa: BLE001
        pass
    logger.debug(
        "backup dirty enqueued mod_id=%s path=%s reason=%s qsize=%s",
        mid or "?",
        path,
        reason_key,
        qsize,
    )
    return True


def backup_queue_size() -> int:
    """Pending + in-flight backup jobs (tests / observability)."""
    return int(_backup_queue.unfinished_tasks)


def metadata_fingerprint(data: dict | None) -> str:
    """Stable fingerprint of user-facing metadata fields for change detection."""
    import json

    src = data or {}
    keys = (
        "title",
        "display_name",
        "description",
        "preview_url",
        "cover",
        "cover_path",
        "url",
        "source_url",
        "workspace_id",
        "external_id",
        "published_file_id",
        "mod_version",
        "game_version",
        "tags",
    )
    payload = {k: src.get(k) for k in keys if k in src}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def drain_backup_queue(*, timeout: float = 5.0) -> None:
    """Test / shutdown helper: wait for queued backups to finish."""
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        if _backup_queue.unfinished_tasks == 0:
            return
        time.sleep(0.05)


def sync_after_metadata_change(
    mod_id: int | str | None,
    managed_path: str | Path | None,
    reason: str,
    *,
    wait: bool = False,
) -> bool:
    """
    Protect user metadata after a write.

    Default lifecycle: mark dirty → BackupWorker (non-blocking).
    Inline only for restore/repair or ``wait=True`` (tests / explicit repair).
    """
    reason_key = str(reason or "").strip() or "rescan"
    if reason_key not in VALID_REASONS:
        reason_key = "rescan"
    if wait or reason_key in _INLINE_REASONS:
        return _sync_backup_now(mod_id, managed_path, reason_key)
    return mark_backup_dirty(mod_id, managed_path, reason_key)


def _sync_backup_now(
    mod_id: int | str | None,
    managed_path: str | Path | None,
    reason: str,
) -> bool:
    """Synchronous .info → backup copy (worker / restore / repair only)."""
    reason_key = str(reason or "").strip() or "rescan"
    if reason_key not in VALID_REASONS:
        logger.debug("Unknown backup sync reason %r; treating as rescan", reason_key)
        reason_key = "rescan"

    root = Path(managed_path) if managed_path else None
    hint = str(mod_id or "").strip()
    from services.metadata_backup import prove_backup_storage_key

    if root is None or not root.is_dir():
        frozen = prove_backup_storage_key(hint, managed_path=root)
        pk = _sql_pk(frozen) or _sql_pk(hint)
        rebound = False
        if pk.isdigit() and root is not None:
            try:
                from services.mod_presence import rediscover_entity_path

                found = rediscover_entity_path(pk)
                if found.success and found.path:
                    candidate = Path(found.path)
                    if candidate.is_dir():
                        root = candidate
                        rebound = True
            except Exception:  # noqa: BLE001
                rebound = False
        if not rebound:
            logger.info(
                "backup sync skipped (folder missing) mod_id=%s path=%s reason=%s",
                frozen or hint or "?",
                root,
                reason_key,
            )
            if pk.isdigit():
                try:
                    from services.metadata_backup import mark_missing

                    mark_missing(pk)
                except Exception:  # noqa: BLE001
                    pass
                _record_status(pk, status="missing")
            try:
                from services.reconcile_observability import note_backup_skipped

                note_backup_skipped()
            except Exception:  # noqa: BLE001
                pass
            return False

    frozen = prove_backup_storage_key(hint, managed_path=root)
    if not is_frozen_backup_uuid(frozen):
        logger.warning(
            "backup sync skipped (entity unresolved) hint=%s path=%s reason=%s",
            hint or "?",
            root,
            reason_key,
        )
        try:
            from services.reconcile_observability import note_backup_skipped

            note_backup_skipped()
        except Exception:  # noqa: BLE001
            pass
        return False
    mid = frozen
    pk = _sql_pk(frozen)

    meta_file = root / INFO_DIR_NAME / METADATA_FILENAME
    if not meta_file.is_file():
        logger.info(
            "backup sync skipped (no .info/metadata.json) path=%s reason=%s",
            root,
            reason_key,
        )
        if pk.isdigit():
            _record_status(pk, status="missing")
        try:
            from services.reconcile_observability import note_backup_skipped

            note_backup_skipped()
        except Exception:  # noqa: BLE001
            pass
        return False

    from services.backup_observability import BackupTimingSession
    from services.reconcile_observability import (
        add_persist_ms,
        note_backup_skipped,
        note_backup_started,
        note_mod_id,
        pop_mod_timing,
        push_mod_timing,
    )

    push_mod_timing(folder=str(root), reason=reason_key, internal_id=mid)
    session = BackupTimingSession(reason=reason_key, mods=1)
    session.t0 = time.perf_counter()
    t_sync = time.perf_counter()
    try:
        try:
            note_backup_started()
            note_mod_id(mid)
            sync_metadata_backup(root, mod_id=mid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "backup sync failed mod_id=%s path=%s reason=%s: %s",
                mid or "?",
                root,
                reason_key,
                exc,
            )
            if pk.isdigit():
                _record_status(pk, status="invalid")
            return False
        copy_ms = (time.perf_counter() - t_sync) * 1000.0
        session.copied = True
        session.add("backup", copy_ms, files=1)

        if not pk.isdigit():
            try:
                from services.file_ops import read_info_metadata_dict
                from services.metadata_owner_guard import resolve_owner_mod_id_from_info

                data = read_info_metadata_dict(root) or {}
                pk = _sql_pk(resolve_owner_mod_id_from_info(data))
            except Exception:  # noqa: BLE001
                pk = ""

        if pk.isdigit():
            try:
                from services.metadata_backup_validator import (
                    status_from_validation,
                    validate_backup,
                )

                t_persist = time.perf_counter()
                result = validate_backup(mid)
                status = status_from_validation(result)
                _record_status(pk, status=status, validate=True)
                persist_ms = (time.perf_counter() - t_persist) * 1000.0
                add_persist_ms(persist_ms)
                session.add("persist", persist_ms, files=1)
                total_ms = (time.perf_counter() - session.t0) * 1000.0
                logger.info(
                    "backup synced mod_id=%s reason=%s status=%s issues=%s "
                    "elapsed_ms=%.1f copy_ms=%.1f persist_ms=%.1f",
                    mid,
                    reason_key,
                    status,
                    result.get("issues") or [],
                    total_ms,
                    copy_ms,
                    persist_ms,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("backup validate after sync failed: %s", exc)
                _record_status(pk, status="partial")
                logger.info(
                    "backup synced mod_id=%s reason=%s status=partial elapsed_ms=%.1f",
                    mid,
                    reason_key,
                    (time.perf_counter() - session.t0) * 1000.0,
                )
        else:
            logger.info(
                "backup synced path=%s reason=%s (mod_id unresolved) elapsed_ms=%.1f",
                root,
                reason_key,
                (time.perf_counter() - session.t0) * 1000.0,
            )
        return True
    finally:
        pop_mod_timing()


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
        mod_id,
        managed_path,
        reason if reason in VALID_REASONS else "repair",
        wait=True,
    )


def rebuild_missing_metadata_backup(
    library_root: str | Path | None = None,
) -> int:
    """
    Scan managed Mod folders; create or repair backup when ``.info/metadata.json``
    exists but ``data/mod_backup/<id>`` is missing, or when the live folder has
    an offline page and the Backup Offline Snapshot is missing or not a usable
    local-dependency closure.

    Returns number of backups created/synced.
    """
    logger.debug("rebuild_missing_metadata_backup enter")
    from core.paths import default_mod_library
    from services.backup_observability import (
        BackupTimingSession,
        log_backup_result,
        log_backup_start,
        log_backup_stage,
    )

    root = Path(library_root) if library_root else Path(default_mod_library())
    session = BackupTimingSession(reason="repair", mods=0)
    session.t0 = time.perf_counter()
    log_backup_start(reason="repair", extra=f"root={root}")
    if not root.is_dir():
        logger.info("rebuild_missing_metadata_backup: library missing %s", root)
        log_backup_result(session, status="skipped")
        return 0

    created = 0
    scanned = 0
    t_discover = time.perf_counter()
    try:
        from services.offline.backup_offline_repair import repair_live_offline_backups

        created += int(
            repair_live_offline_backups(library_root=root).get("backup_repaired") or 0
        )
    except Exception:  # noqa: BLE001
        logger.exception("rebuild_missing_metadata_backup: live offline repair failed")
    for meta_path in root.glob(f"*/*/{INFO_DIR_NAME}/{METADATA_FILENAME}"):
        scanned += 1
        managed = meta_path.parent.parent
        if not managed.is_dir():
            continue
        mid = ""
        try:
            from services.file_ops import read_info_metadata_dict
            from services.metadata_owner_guard import resolve_owner_mod_id_from_info

            data = read_info_metadata_dict(managed) or {}
            # Never use folder.name / published_file_id as backup owner key.
            mid = resolve_owner_mod_id_from_info(data)
        except Exception:  # noqa: BLE001
            data = {}
        if not mid.isdigit():
            continue
        dest = readable_backup_root(mid)
        backup_meta = (dest / "metadata.json") if dest is not None else None
        live_index = None
        try:
            from services.offline.paths import resolve_offline_page

            live_index = resolve_offline_page(managed)
        except Exception:  # noqa: BLE001
            live_index = None
        missing_bucket = dest is None or backup_meta is None or not backup_meta.is_file()
        invalid_offline = False
        if dest is not None and live_index is not None and live_index.is_file():
            from services.offline.backup_closure import backup_offline_snapshot_valid

            invalid_offline = not backup_offline_snapshot_valid(
                dest / BACKUP_OFFLINE_DIR, source_index=live_index
            )
        if not missing_bucket and not invalid_offline:
            continue
        if missing_bucket:
            if rebuild_metadata_backup(mid, managed, reason="repair"):
                created += 1
            continue
        from services.offline.backup_offline_repair import snapshot_live_offline_only

        if snapshot_live_offline_only(mid, managed):
            created += 1

    discover_ms = (time.perf_counter() - t_discover) * 1000.0
    session.mods = created
    session.add("discover", discover_ms, mods=scanned)
    log_backup_stage("discover", elapsed_ms=discover_ms, mods=scanned)
    session.add("backup", (time.perf_counter() - session.t0) * 1000.0, mods=created)
    logger.info(
        "rebuild_missing_metadata_backup done scanned=%s created=%s root=%s",
        scanned,
        created,
        root,
    )
    log_backup_result(session, status="ok")
    return created


def request_backup_rebuild_shutdown() -> None:
    """Refuse new backup rebuilds. Does not abort an in-flight pass."""
    global _rebuild_shutdown
    with _rebuild_lock:
        _rebuild_shutdown = True


def join_backup_rebuild_thread(timeout: float) -> bool:
    thread = _rebuild_thread
    if thread is None or not thread.is_alive():
        return True
    thread.join(timeout)
    return not thread.is_alive()


def reset_backup_rebuild_async_state() -> None:
    """Test helper. Does not stop an in-flight rebuild thread."""
    global _rebuild_running, _rebuild_shutdown, _rebuild_thread
    with _rebuild_lock:
        _rebuild_running = False
        _rebuild_shutdown = False
        _rebuild_thread = None


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
