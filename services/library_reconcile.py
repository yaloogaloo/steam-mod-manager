"""Library reconciliation — filesystem ↔ database consistency only.

ARCHITECTURE RULE (lifecycle ownership)
---------------------------------------
Reconcile may:

1. Discover filesystem / backup state
2. Bind and update **existing** Internal Entities (path, folder_present, …)
3. Emit :class:`services.orphan_import.OrphanCandidate` for unknown folders

Reconcile must **never**:

- call ``create_mod_identity`` / allocate Internal Entities
- treat consistency scanning as user-metadata change (no backup enqueue
  when an existing entity is unchanged)

Correct create lifecycle for unknown directories:

    Reconcile → OrphanCandidate → Import/Sync (``import_orphan_candidates``)
        → Identity Service → Internal Entity

ARCHITECTURE RULE (content missing)
-----------------------------------
Content Missing is a **system derived state**. Reconcile must **not** produce
``content_status=content_missing``. Missing evaluation belongs to
``services.content_status_eval`` (via Refresh).

ARCHITECTURE RULE (startup)
---------------------------
Reconcile must **not** make Loading Mods wait. Bind known entities via
indexed resolve; leave unresolved folders as OrphanCandidate / notes.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core.mod_platform import (
    PLATFORM_STEAM,
    is_internal_mod_id,
    normalize_platform,
    normalize_platform_if_known,
)
from core.paths import default_mod_library
from services.file_ops import (
    INFO_DIR_NAME,
    LEGACY_INFO_DIR_NAME,
    METADATA_FILENAME,
    ModFileManager,
    persist_unified_metadata_dict,
    read_info_metadata_dict,
)
from services.library_status import (
    LIBRARY_STATUS_IMPORTED,
    LIBRARY_STATUS_MISSING,
    LIBRARY_STATUS_NORMAL,
    infer_initial_source_type,
    row_source_type,
)
from services.metadata_backup import BACKUP_DIR_NAME, mark_missing
from services.metadata_backup_sync import mark_backup_dirty
from services.mod_identity import (
    ensure_mod_identity,
    extract_workspace_id,
    read_internal_id,
)
from services.orphan_import import OrphanCandidate

logger = logging.getLogger(__name__)

__all__ = [
    "LIBRARY_STATUS_NORMAL",
    "LIBRARY_STATUS_MISSING",
    "LIBRARY_STATUS_IMPORTED",
    "OrphanCandidate",
    "ReconcileResult",
    "ReconcilePacing",
    "reconcile_library",
    "start_reconcile_library_async",
    "schedule_startup_library_reconcile",
    "pause_reconcile",
    "resume_reconcile",
    "resolve_library_games",
    "hold_library_load_until_reconcile_idle",
    "release_startup_library_hold",
    "library_load_must_wait",
    "is_reconcile_running",
    "add_reconcile_idle_listener",
    "take_projection_touch_ids",
    "request_reconcile_shutdown",
    "join_reconcile_thread",
]

_reconcile_lock = threading.Lock()
_reconcile_running = False
_reconcile_pending_root: str | None = None
_reconcile_pending_pacing: "ReconcilePacing | None" = None
# True after MainWindow schedules the startup reconcile QTimer, until that
# run actually starts (or is released). Prevents LibraryLoadWorker from
# racing the first QTimer.singleShot(0) reconcile.
_startup_hold = False
_idle_listeners: list = []
_shutdown_requested = False
_reconcile_thread: threading.Thread | None = None
_projection_touch_ids: list[str] = []
# Cooperative pause: set = running; clear = paused (batch loops wait).
_reconcile_run_gate = threading.Event()
_reconcile_run_gate.set()
_startup_timer: threading.Timer | None = None


@dataclass(frozen=True)
class ReconcilePacing:
    """Optional IO pacing for background / startup reconcile.

    Does not change Identity bind rules — only when the worker sleeps.
    ``max_mods`` > 0 caps the bind pass and skips missing/orphan phases so
    a partial run cannot false-mark unvisited folders as missing.
    """

    batch_size: int = 0
    batch_pause_ms: int = 0
    max_mods: int = 0
    cooperative: bool = False
    low_priority: bool = False


def pause_reconcile() -> None:
    """Pause cooperative reconcile batches (no-op if not cooperative)."""
    _reconcile_run_gate.clear()
    logger.info("reconcile paused")


def resume_reconcile() -> None:
    """Resume after :func:`pause_reconcile`."""
    _reconcile_run_gate.set()
    logger.info("reconcile resumed")


def _set_current_thread_low_priority() -> None:
    try:
        import sys

        if sys.platform != "win32":
            return
        import ctypes

        # THREAD_PRIORITY_BELOW_NORMAL = -1
        ctypes.windll.kernel32.SetThreadPriority(  # type: ignore[attr-defined]
            ctypes.windll.kernel32.GetCurrentThread(),  # type: ignore[attr-defined]
            -1,
        )
    except Exception:  # noqa: BLE001
        pass


def _cooperative_wait(*, pause_ms: int = 0) -> bool:
    """Wait while paused / yield between batches. False if shutdown."""
    while True:
        if _shutdown_requested:
            return False
        if _reconcile_run_gate.wait(0.05):
            break
    if pause_ms <= 0:
        return not _shutdown_requested
    end = time.monotonic() + (max(0, int(pause_ms)) / 1000.0)
    while time.monotonic() < end:
        if _shutdown_requested:
            return False
        if not _reconcile_run_gate.wait(0.02):
            continue
        time.sleep(min(0.02, max(0.0, end - time.monotonic())))
    return not _shutdown_requested


def take_projection_touch_ids() -> list[str]:
    """Consume internal_ids whose Projection path bind changed during reconcile."""
    global _projection_touch_ids
    with _reconcile_lock:
        out = list(_projection_touch_ids)
        _projection_touch_ids = []
        return out


def _store_projection_touch_ids(ids: list[str]) -> None:
    global _projection_touch_ids
    cleaned = [str(x).strip() for x in ids if str(x).strip()]
    with _reconcile_lock:
        _projection_touch_ids = list(dict.fromkeys(cleaned))


def _refresh_projections_for_rebinds(ids: list[str]) -> None:
    """Warm-cache only — UI listeners fire on main thread via idle."""
    try:
        from services.mod_library_cache import get_library_cache
    except Exception:  # noqa: BLE001
        return
    cache = get_library_cache()
    for mid in ids:
        try:
            cache.refresh_projection(mid)
        except Exception:  # noqa: BLE001
            pass


@dataclass
class ReconcileResult:
    scanned: int = 0
    synced: int = 0
    imported: int = 0
    missing: int = 0
    renamed: int = 0
    conflicts: int = 0
    restored: int = 0
    notes: list[str] = field(default_factory=list)
    # Unknown directories / backup dirs awaiting Import/Sync Identity — never created here.
    orphans: list[OrphanCandidate] = field(default_factory=list)
    # internal_id values whose last_known_path was rebound this run.
    rebound_ids: list[str] = field(default_factory=list)


def _folder_has_metadata(folder: Path) -> bool:
    try:
        if (folder / INFO_DIR_NAME / METADATA_FILENAME).is_file():
            return True
        if (folder / LEGACY_INFO_DIR_NAME / METADATA_FILENAME).is_file():
            return True
        if (folder / "metadata.json").is_file():
            return True
    except OSError:
        return False
    return False


def _folder_missing_content(
    folder: Path,
    *,
    internal_id: str | None = None,
    db=None,
) -> bool:
    """Payload-absence fact for callers that need a boolean.

    ARCHITECTURE RULE: Do **not** use this inside ``reconcile_library`` to
    persist ``content_status=content_missing``. Reconcile must not stamp
    missing; Refresh uses ``content_status_eval`` instead.
    """
    from services.local_file_index import has_local_mod_payload

    return not has_local_mod_payload(folder, mod_id=internal_id, db=db)


def reconcile_library(
    library_root: str | Path | None = None,
    *,
    pacing: ReconcilePacing | None = None,
) -> ReconcileResult:
    """
    Unify disk / SQLite path facts for **existing** entities.

    1. Scan ``mod/<game>/<mod>`` → bind via ``ensure_mod_identity``
    2. Update path / folder_present on known entities
    3. Emit :class:`OrphanCandidate` for unknown directories (no Identity create)
    4. Mark DB rows whose folders are gone as ``folder_present=0``

    ARCHITECTURE RULE: Reconcile has **no** Internal Entity create right.
    Unknown directories are OrphanCandidates for Import/Sync Identity only.

    ARCHITECTURE RULE: present-folder content axis is owned by
    ``services.content_status_eval`` (authoritative payload check). Reconcile
    may *invoke* that API; it must not stamp ``content_missing`` from shallow
    probes or literals. Absent folders re-eval to ``content_missing``.
    Multi-folder identity may mark ``identity_status`` (not Mod status).

    ARCHITECTURE RULE: consistency scan ≠ user metadata change. Do not
    ``mark_backup_dirty`` when an existing entity is unchanged.

    *pacing* only affects sleep / early-cap between mods. Identity rules are
    unchanged. A capped run skips missing + orphan phases (safe partial).
    """
    root = Path(library_root) if library_root else Path(default_mod_library())
    result = ReconcileResult()
    pace = pacing or ReconcilePacing()
    if not root.is_dir():
        root.mkdir(parents=True, exist_ok=True)

    try:
        from services.managed_path_cache import invalidate_managed_path_cache

        invalidate_managed_path_cache(library_root=root)
    except Exception:  # noqa: BLE001
        pass

    from core.db_manager import get_db
    from core.paths import data_dir
    from services.identity_service import LIFECYCLE_RECONCILE, current_lifecycle, lifecycle_scope

    if current_lifecycle() != LIFECYCLE_RECONCILE:
        with lifecycle_scope(LIFECYCLE_RECONCILE):
            return reconcile_library(library_root, pacing=pacing)

    try:
        from services.mod_fs_observer import begin_projection_defer

        begin_projection_defer()
    except Exception:  # noqa: BLE001
        pass

    db = get_db()
    try:
        from services.status_recovery import (
            run_status_model_cleanup_v2,
            run_status_recovery,
        )

        run_status_recovery(db, root)
        run_status_model_cleanup_v2(db, root)
    except Exception:  # noqa: BLE001
        logger.debug("status_recovery skipped", exc_info=True)
    manager = ModFileManager(root)
    seen_ids: set[str] = set()
    id_to_paths: dict[str, list[Path]] = {}

    import time as _time

    from services.backup_observability import (
        BackupTimingSession,
        log_backup_result,
        log_backup_start,
        log_backup_stage,
    )

    backup_session = BackupTimingSession(reason="reconcile")
    backup_session.t0 = _time.perf_counter()
    log_backup_start(reason="reconcile", extra=f"root={root}")

    from services.reconcile_observability import (
        ModTimingGuard,
        add_identity_ms,
        add_list_discover_ms,
        add_scan_ms,
        finish_reconcile_session,
        note_mod_id,
        start_reconcile_session,
    )

    start_reconcile_session()
    try:
        from services.startup_io_trace import begin as _io_begin

        _io_begin("reconcile")
    except Exception:  # noqa: BLE001
        pass
    t_discover = _time.perf_counter()
    managed_folders = manager.list_managed_mods()
    add_list_discover_ms((_time.perf_counter() - t_discover) * 1000.0)

    incomplete = False
    # --- Step 1: disk mods (bind existing only) ---
    with ModTimingGuard(reason="reconcile") as _mod_timer:
        for folder_index, folder in enumerate(managed_folders):
            if pace.cooperative and not _cooperative_wait():
                incomplete = True
                result.notes.append("RECONCILE_SHUTDOWN")
                break
            if pace.max_mods > 0 and result.scanned >= pace.max_mods:
                incomplete = True
                result.notes.append(f"STARTUP_RECONCILE_CAP: {pace.max_mods}")
                break
            result.scanned += 1
            _mod_timer.begin(str(folder))
            t_scan = _time.perf_counter()
            raw = dict(read_info_metadata_dict(folder) or {})
            add_scan_ms((_time.perf_counter() - t_scan) * 1000.0)
            if not raw:
                # No .info → not a Mod. Only allowed action: restore .info when
                # DB already owns this path and backup identity matches.
                try:
                    owned = db.find_mod_by_last_known_path(str(folder.resolve()))
                except Exception:  # noqa: BLE001
                    owned = None
                if owned and str(owned).isdigit():
                    from services.metadata_backup import restore_info_sidecar_from_backup

                    if restore_info_sidecar_from_backup(owned, folder, db=db):
                        raw = dict(read_info_metadata_dict(folder) or {})
                        result.notes.append(f"INFO_RESTORED_FROM_BACKUP: {folder}")
                    else:
                        result.notes.append(f"IGNORE_NO_INFO: {folder}")
                        continue
                else:
                    result.notes.append(f"IGNORE_NO_INFO: {folder}")
                    continue
            raw["_managed_path"] = str(folder.resolve())
            raw["_folder_name"] = folder.name
            t_ident = _time.perf_counter()
            # Bind only — never allocate / mint / create here.
            internal_id, payload, changed = ensure_mod_identity(folder, raw, db=db)
            had_row = bool(internal_id.isdigit())
            add_identity_ms((_time.perf_counter() - t_ident) * 1000.0)
            note_mod_id(internal_id)
            early_drift = None
            prev_before_drift = ""
            if internal_id.isdigit():
                prev_before_drift = str(
                    (db.get_mod_backup_row(internal_id) or {}).get("last_known_path") or ""
                ).strip()
                from services.path_lifecycle import detect_path_drift as _drift_before_create

                early_drift = _drift_before_create(internal_id, folder, db=db)
            if not internal_id.isdigit():
                # Forged / unmatched .info — ignore. Never create. Never invent
                # workshop identity from folder names.
                result.notes.append(f"IGNORE_UNBOUND_INFO: {folder}")
                continue

            if changed:
                try:
                    persist_unified_metadata_dict(
                        folder, payload, sync_backup=False, sync_reason="import"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("persist identity failed for %s: %s", folder, exc)

            id_to_paths.setdefault(internal_id, []).append(folder)
            seen_ids.add(internal_id)

            prev = db.get_mod_backup_row(internal_id)
            prev_path = prev_before_drift or str((prev or {}).get("last_known_path") or "").strip()
            renamed = early_drift is not None and early_drift.success
            if not renamed:
                from services.path_lifecycle import detect_path_drift

                drift = detect_path_drift(internal_id, folder, db=db)
                renamed = drift is not None and drift.success
            if renamed:
                result.renamed += 1
                result.rebound_ids.append(internal_id)
            elif prev_path and Path(prev_path) != folder:
                if prev_path and Path(prev_path).is_dir() and Path(prev_path) != folder:
                    pass
                else:
                    result.renamed += 1
                    result.rebound_ids.append(internal_id)

            title = str(payload.get("title") or payload.get("display_name") or folder.name)
            payload_source = str(
                payload.get("source_type") or payload.get("platform") or ""
            )
            store_platform = normalize_platform_if_known(payload_source) or (
                normalize_platform(payload_source) if payload_source else ""
            )
            source_type = infer_initial_source_type(
                mod_id=internal_id,
                had_row=had_row,
                existing_source=str((prev or {}).get("source_type") or ""),
                existing_platform=str((prev or {}).get("platform") or ""),
                payload_source=payload_source,
            )

            from services.identity_service import persist_identity, persist_workspace_id
            from services.mod_identity import source_url_embeds_internal
            from services.mod_identity_authority import sanitize_platform_external_id

            raw_url = str(payload.get("url") or payload.get("source_url") or "").strip()
            if source_url_embeds_internal(raw_url, internal_pk=internal_id):
                raw_url = ""
            ws = persist_workspace_id(
                platform=store_platform,
                mod_id=internal_id,
                workspace_id=str(payload.get("workspace_id") or ""),
                source_url=raw_url,
                external_id=str(payload.get("external_id") or ""),
            )
            ext = sanitize_platform_external_id(
                store_platform, str(payload.get("external_id") or ""), mod_id=internal_id
            )
            # Path / identity on present folders.
            # Content Missing: only via content_status_eval (authoritative payload).
            persist_kw: dict = {
                "internal_id": read_internal_id(payload),
                "source_type": source_type,
                "last_known_path": str(folder.resolve()),
                "folder_present": True,
                "title": title,
                "external_id": ext or None,
                "workspace_id": ws or None,
                "app_id": int(payload.get("app_id") or 0) or None,
                "sticky_source": True,
            }
            if store_platform:
                persist_kw["platform"] = store_platform
            if raw_url:
                persist_kw["source_url"] = raw_url
            try:
                persist_identity(
                    db,
                    internal_id,
                    source="reconcile",
                    reason="bind",
                    **persist_kw,
                )
            except Exception:  # noqa: BLE001
                logger.debug("update identity fields failed for %s", mid, exc_info=True)

            # Content Validator (authority): derive content_status from live payload.
            # Do not use shallow probes or literal content_missing stamps.
            try:
                from services.content_status_eval import persist_evaluated_content_status

                persist_evaluated_content_status(
                    internal_id,
                    folder,
                    db=db,
                    folder_present=True,
                    backup_status=str((prev or {}).get("backup_status") or ""),
                    metadata_missing=not _folder_has_metadata(folder),
                    sync_sticky_marker=True,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "content status validate failed for %s", internal_id, exc_info=True
                )

            # ARCHITECTURE RULE: only real sidecar metadata change dirties backup.
            # Steady-state bind / path reconcile must not enqueue BackupWorker.
            if changed:
                if mark_backup_dirty(internal_id, folder, "restore"):
                    result.synced += 1
                    if had_row and renamed:
                        result.restored += 1

            if (
                pace.batch_size > 0
                and pace.batch_pause_ms > 0
                and (folder_index + 1) % pace.batch_size == 0
            ):
                if not _cooperative_wait(pause_ms=pace.batch_pause_ms):
                    incomplete = True
                    result.notes.append("RECONCILE_SHUTDOWN")
                    break

    # Identity conflicts: same mod_id → multiple live folders
    for mid, paths in id_to_paths.items():
        live = [p for p in paths if p.is_dir()]
        if len(live) > 1:
            result.conflicts += 1
            try:
                from services.status_authority import IDENTITY_STATUS_CONFLICT

                db.update_mod_identity_fields(
                    mid,
                    identity_status=IDENTITY_STATUS_CONFLICT,
                    folder_present=True,
                )
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                from services.status_authority import IDENTITY_STATUS_OK

                row = db.get_mod_backup_row(mid) or {}
                cur = str(row.get("identity_status") or "").strip()
                if cur == "identity_conflict":
                    db.update_mod_identity_fields(
                        mid, identity_status=IDENTITY_STATUS_OK
                    )
            except Exception:  # noqa: BLE001
                pass
            # Sole remaining legal .info path must become the entity's storage bind.
            # Entity / internal_id / workspace_id stay unchanged.
            if len(live) == 1:
                sole = live[0]
                try:
                    from services.identity_service import persist_identity

                    prev_row = db.get_mod_backup_row(mid) or {}
                    prev_lkp = str(prev_row.get("last_known_path") or "").strip()
                    sole_resolved = str(sole.resolve())
                    if prev_lkp != sole_resolved or int(prev_row.get("folder_present") or 0) != 1:
                        persist_identity(
                            db,
                            mid,
                            source="reconcile",
                            reason="rebind_sole_info_path",
                            last_known_path=sole_resolved,
                            folder_present=True,
                        )
                        result.renamed += 1
                        result.rebound_ids.append(mid)
                        result.notes.append(f"PATH_REBOUND: {mid} -> {sole_resolved}")
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "sole-path rebind failed for %s", mid, exc_info=True
                    )

    # --- Step 2: backup / DB rows without disk ---
    # Incomplete (capped / shutdown) runs must not mark unvisited folders missing.
    if incomplete:
        result.notes.append('SKIP_MISSING_AND_ORPHAN_PHASES')

    if not incomplete:
        try:
            from services.mod_presence import RediscoveryIndex, rediscover_entity_path

            rediscovery = RediscoveryIndex()
            for row in db.iter_mod_backup_rows():
                mid = str(row.get('mod_id') or '').strip()
                if not mid.isdigit() or mid in seen_ids:
                    continue
                lkp = str(row.get('last_known_path') or '').strip()
                path = Path(lkp) if lkp else None
                if path is not None and path.is_dir():
                    # Path still present but was not in list_managed_mods — bind path only.
                    # No backup enqueue: consistency scan ≠ metadata change.
                    try:
                        from services.identity_service import persist_identity

                        persist_identity(
                            db,
                            mid,
                            source='reconcile',
                            reason='bind',
                            last_known_path=str(path.resolve()),
                            folder_present=True,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    seen_ids.add(mid)
                    continue

                # Dead last_known_path: rediscover in the game managed root only
                # (never workspace / folder-name invent; Evidence A + B required).
                rebound = False
                try:
                    found = rediscover_entity_path(
                        mid, db=db, library_root=root, index=rediscovery
                    )
                    if found.success and found.path:
                        alt = Path(found.path)
                        if alt.is_dir():
                            try:
                                from services.content_status_eval import (
                                    persist_evaluated_content_status,
                                )
                                from services.identity_service import persist_identity

                                persist_identity(
                                    db,
                                    mid,
                                    source='reconcile',
                                    reason='rebind_info_internal_id',
                                    last_known_path=str(alt.resolve()),
                                    folder_present=True,
                                )
                                persist_evaluated_content_status(
                                    mid,
                                    alt,
                                    db=db,
                                    folder_present=True,
                                    backup_status=str(row.get('backup_status') or ''),
                                    metadata_missing=not _folder_has_metadata(alt),
                                    sync_sticky_marker=True,
                                )
                            except Exception:  # noqa: BLE001
                                pass
                            seen_ids.add(mid)
                            result.renamed += 1
                            result.rebound_ids.append(mid)
                            result.notes.append(f'PATH_REBOUND: {mid} -> {alt}')
                            rebound = True
                except Exception:  # noqa: BLE001
                    logger.debug(
                        'internal_id path rebind failed for %s', mid, exc_info=True
                    )
                if rebound:
                    continue

                mark_missing(mid)
                try:
                    from services.content_status_eval import persist_evaluated_content_status

                    bstatus = str(row.get('backup_status') or '').strip()
                    existing_source = row_source_type(row)
                    if existing_source:
                        db.update_mod_identity_fields(
                            mid,
                            source_type=existing_source,
                            folder_present=False,
                        )
                    else:
                        db.update_mod_identity_fields(mid, folder_present=False)
                    persist_evaluated_content_status(
                        mid,
                        None,
                        db=db,
                        folder_present=False,
                        backup_status=bstatus,
                        sync_sticky_marker=False,
                    )
                except Exception:  # noqa: BLE001
                    pass
                result.missing += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning('reconcile missing-pass failed: %s', exc)

        # Orphan backup dirs on disk not yet in SQLite → OrphanCandidate (no create).
        # Backup folder name is Internal/Steam PK storage — never Steam CREATE proof.
        try:
            from services.metadata_backup import load_backup

            def _under_library(path_text: str) -> bool:
                text = str(path_text or '').strip()
                if not text:
                    return False
                try:
                    resolved = Path(text).expanduser().resolve()
                    return resolved == root.resolve() or root.resolve() in resolved.parents
                except OSError:
                    return False

            backup_base = data_dir() / BACKUP_DIR_NAME
            if backup_base.is_dir():
                for child in backup_base.iterdir():
                    if not child.is_dir() or not child.name.isdigit():
                        continue
                    mid = child.name
                    if mid in seen_ids:
                        continue
                    if is_internal_mod_id(mid) and db.get_mod(mid) is None:
                        result.notes.append(f'IDENTITY_UNRESOLVED backup: {mid}')
                        continue
                    meta_file = child / 'metadata.json'
                    if not meta_file.is_file():
                        continue
                    try:
                        snap = load_backup(mid)
                        if snap is None:
                            continue
                        meta = snap.metadata
                        lkp = str(snap.last_known_path or '').strip()
                        if db.get_mod(mid) is None and lkp and not _under_library(lkp):
                            continue
                        if db.get_mod(mid) is None and not lkp:
                            result.notes.append(f'IDENTITY_UNRESOLVED backup: {mid}')
                            continue
                        if db.get_mod(mid) is not None:
                            # Existing entity — path/presence only; no backup enqueue.
                            present = bool(lkp and Path(lkp).is_dir())
                            from services.content_status_eval import (
                                persist_evaluated_content_status,
                            )
                            from services.identity_service import persist_identity

                            persist_identity(
                                db,
                                mid,
                                source='reconcile',
                                reason='backup_orphan',
                                internal_id=read_internal_id(meta) or None,
                                last_known_path=lkp,
                                folder_present=present,
                                sticky_source=True,
                            )
                            persist_evaluated_content_status(
                                mid,
                                Path(lkp) if present else None,
                                db=db,
                                folder_present=present,
                                sync_sticky_marker=False,
                            )
                            if not present:
                                result.missing += 1
                            seen_ids.add(mid)
                            continue

                        # No mods row — emit OrphanCandidate for Import/Sync.
                        title = str(
                            meta.get('title')
                            or meta.get('display_name')
                            or f'Unknown_Mod_{mid}'
                        )
                        payload_source = str(
                            meta.get('source_type') or meta.get('platform') or ''
                        )
                        store_platform = normalize_platform_if_known(payload_source) or (
                            normalize_platform(payload_source) if payload_source else ''
                        )
                        url = str(meta.get('url') or meta.get('source_url') or '')
                        ext = str(meta.get('external_id') or '')
                        ws_meta = str(meta.get('workspace_id') or '').strip()
                        from services.identity_service import has_official_platform_identity

                        steam_wid = ''
                        if store_platform == PLATFORM_STEAM:
                            if ext.isdigit() and not is_internal_mod_id(ext):
                                steam_wid = ext
                            elif ws_meta.isdigit() and not is_internal_mod_id(ws_meta):
                                steam_wid = ws_meta
                        official = has_official_platform_identity(
                            platform=store_platform,
                            external_id=ext,
                            source_url=url,
                            workshop_id=steam_wid,
                        )
                        if not official or not store_platform:
                            result.notes.append(f'IDENTITY_UNRESOLVED backup: {mid}')
                            continue
                        orphan_path = lkp if lkp else str(child)
                        result.orphans.append(
                            OrphanCandidate(
                                path=orphan_path,
                                platform=store_platform,
                                external_id=ext or steam_wid,
                                workspace_id=ws_meta or steam_wid or ext,
                                source_url=url,
                                title=title,
                                app_id=int(meta.get('app_id') or 0),
                                game_name=str(meta.get('game_name') or ''),
                                origin='backup',
                                payload=dict(meta),
                            )
                        )
                        result.notes.append(f'ORPHAN_CANDIDATE backup: {mid}')
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            'orphan backup scan failed for %s', mid, exc_info=True
                        )
        except Exception as exc:  # noqa: BLE001
            logger.warning('orphan backup scan failed: %s', exc)

    # Recover interrupted deploy transactions (backup_done / prepared leftovers).
    try:
        from services.deploy import ModDeployer

        recovery = ModDeployer(library_root=root, db=db).recover_stale_deploy_transactions()
        if recovery:
            logger.info(
                "reconcile_library deploy-txn recovery count=%s", len(recovery)
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("deploy transaction recovery failed: %s", exc)

    logger.info(
        "reconcile_library done scanned=%s synced=%s orphans=%s missing=%s "
        "renamed=%s conflicts=%s restored=%s root=%s",
        result.scanned,
        result.synced,
        len(result.orphans),
        result.missing,
        result.renamed,
        result.conflicts,
        result.restored,
        root,
    )
    result.rebound_ids = list(
        dict.fromkeys(str(x).strip() for x in result.rebound_ids if str(x).strip())
    )
    try:
        from services.mod_fs_observer import (
            drain_projection_touch_ids,
            end_projection_defer,
            observe_mods_fs_batch,
        )

        end_projection_defer()
        content_touch = drain_projection_touch_ids()
        if content_touch:
            result.rebound_ids.extend(content_touch)
            result.rebound_ids = list(dict.fromkeys(result.rebound_ids))
        # Startup / reconcile follow-up: cheap batch L0 only (never full L2).
        try:
            observe_mods_fs_batch(level=0, db=db, notify_projection=False)
            result.notes.append("fs_observe_l0_batch")
        except Exception:  # noqa: BLE001
            logger.debug("post-reconcile L0 batch failed", exc_info=True)
    except Exception:  # noqa: BLE001
        logger.debug("projection defer end / L0 batch failed", exc_info=True)

    if result.rebound_ids:
        _refresh_projections_for_rebinds(result.rebound_ids)
        _store_projection_touch_ids(result.rebound_ids)
    elapsed_ms = (_time.perf_counter() - backup_session.t0) * 1000.0
    backup_session.mods = result.scanned
    backup_session.add("sync", elapsed_ms, mods=result.synced)
    log_backup_stage(
        "sync",
        elapsed_ms=elapsed_ms,
        mods=result.scanned,
        files=result.synced,
    )
    log_backup_result(backup_session, status="ok")
    finish_reconcile_session()
    try:
        from services.startup_io_trace import end as _io_end

        _io_end("reconcile")
    except Exception:  # noqa: BLE001
        pass
    return result


def _weak_idle_ref(callback: Callable[[], None]) -> weakref.ref:
    if getattr(callback, "__self__", None) is not None:
        return weakref.WeakMethod(callback)  # type: ignore[return-value]
    return weakref.ref(callback)


def add_reconcile_idle_listener(callback: Callable[[], None]) -> None:
    """Invoke *callback* when a reconcile run (including queued follow-up) ends."""
    ref = _weak_idle_ref(callback)
    with _reconcile_lock:
        _idle_listeners.append(ref)


def hold_library_load_until_reconcile_idle() -> None:
    """Defer LibraryLoadWorker until the upcoming startup reconcile is idle."""
    global _startup_hold
    with _reconcile_lock:
        _startup_hold = True


def release_startup_library_hold() -> None:
    """Drop the startup hold; notify listeners if reconcile is not running."""
    global _startup_hold
    fire = False
    with _reconcile_lock:
        if _startup_hold:
            _startup_hold = False
            fire = not _reconcile_running
    if fire:
        _notify_reconcile_idle()


def is_reconcile_running() -> bool:
    with _reconcile_lock:
        return bool(_reconcile_running)


def library_load_must_wait() -> bool:
    """True while startup reconcile is pending or a reconcile thread is active."""
    with _reconcile_lock:
        return bool(_reconcile_running or _startup_hold)


def reset_reconcile_async_state() -> None:
    """Test helper. Does not stop an in-flight reconcile thread."""
    global _reconcile_running, _reconcile_pending_root, _reconcile_pending_pacing
    global _startup_hold, _shutdown_requested, _reconcile_thread, _startup_timer
    with _reconcile_lock:
        _reconcile_running = False
        _reconcile_pending_root = None
        _reconcile_pending_pacing = None
        _startup_hold = False
        _shutdown_requested = False
        _reconcile_thread = None
        _idle_listeners.clear()
    _reconcile_run_gate.set()
    timer = _startup_timer
    _startup_timer = None
    if timer is not None:
        try:
            timer.cancel()
        except Exception:  # noqa: BLE001
            pass


def request_reconcile_shutdown() -> None:
    """Refuse new / queued reconcile runs. Does not abort an in-flight pass."""
    global _shutdown_requested, _reconcile_pending_root, _startup_timer
    with _reconcile_lock:
        _shutdown_requested = True
        _reconcile_pending_root = None
        _reconcile_pending_pacing = None
    _reconcile_run_gate.set()  # unblock paused workers so they can exit
    timer = _startup_timer
    _startup_timer = None
    if timer is not None:
        try:
            timer.cancel()
        except Exception:  # noqa: BLE001
            pass


def join_reconcile_thread(timeout: float) -> bool:
    """Wait for the daemon worker. True when it is not alive."""
    thread = _reconcile_thread
    if thread is None or not thread.is_alive():
        return True
    thread.join(timeout)
    return not thread.is_alive()


def _notify_reconcile_idle() -> None:
    with _reconcile_lock:
        refs = list(_idle_listeners)
    for ref in refs:
        callback = ref()
        if callback is None:
            continue
        try:
            callback()
        except Exception:  # noqa: BLE001
            logger.exception("reconcile idle listener failed")


def schedule_startup_library_reconcile(
    library_root: str | Path | None = None,
) -> str:
    """GUI startup entry for Identity Reconcile.

    Default policy (``startup_reconcile_enabled=false``): skip folder walk /
    Identity Reconcile entirely and release any library-load hold so Library
    can open from DB state immediately.

    When enabled: delay, then run a low-priority batched cooperative reconcile.
    Explicit ``start_reconcile_library_async`` / ``reconcile_library`` callers
    are unaffected.
    """
    global _startup_timer
    from services.startup_reconcile_policy import load_startup_reconcile_policy

    policy = load_startup_reconcile_policy()
    root = str(library_root) if library_root else None
    if not policy.enabled:
        release_startup_library_hold()
        logger.info(
            "startup reconcile skipped "
            "(startup_reconcile_enabled=false; DB projection only)"
        )
        return "skipped"

    pacing = ReconcilePacing(
        batch_size=int(policy.batch_size),
        batch_pause_ms=int(policy.batch_pause_ms),
        max_mods=int(policy.max_mods),
        cooperative=True,
        low_priority=True,
    )
    delay_s = max(0.0, float(policy.delay_ms) / 1000.0)

    def _fire() -> None:
        global _startup_timer
        _startup_timer = None
        if _shutdown_requested:
            release_startup_library_hold()
            return
        start_reconcile_library_async(root, pacing=pacing)
        logger.info(
            "startup reconcile started after delay_ms=%s batch=%s pause_ms=%s "
            "max_mods=%s",
            policy.delay_ms,
            policy.batch_size,
            policy.batch_pause_ms,
            policy.max_mods,
        )

    if delay_s <= 0:
        _fire()
        return "started"

    with _reconcile_lock:
        if _startup_timer is not None:
            try:
                _startup_timer.cancel()
            except Exception:  # noqa: BLE001
                pass
        timer = threading.Timer(delay_s, _fire)
        timer.daemon = True
        _startup_timer = timer
        timer.start()
    # Do not hold Library load for delayed reconcile.
    release_startup_library_hold()
    logger.info(
        "startup reconcile scheduled delay_ms=%s (library load not blocked)",
        policy.delay_ms,
    )
    return "scheduled"


def start_reconcile_library_async(
    library_root: str | Path | None = None,
    *,
    pacing: ReconcilePacing | None = None,
) -> bool:
    """Run :func:`reconcile_library` on a daemon thread (non-blocking).

    Concurrent callers are coalesced: if a run is in progress, the latest
    *library_root* (and pacing) is queued and executed once after the current
    run finishes.

    *pacing* is optional IO throttling for background/startup runs. User /
    Repair / Import callers typically omit it for a full unpaced pass.
    """
    global _reconcile_running, _reconcile_pending_root, _reconcile_pending_pacing
    global _startup_hold, _reconcile_thread
    root = str(library_root) if library_root else None
    with _reconcile_lock:
        if _shutdown_requested:
            logger.info("reconcile_library skipped; shutdown in progress")
            return False
        _startup_hold = False
        if _reconcile_running:
            _reconcile_pending_root = root
            _reconcile_pending_pacing = pacing
            logger.info("reconcile_library already running; queued follow-up")
            try:
                from services.startup_io_trace import note_reconcile_queued

                note_reconcile_queued()
            except Exception:  # noqa: BLE001
                pass
            return False
        _reconcile_running = True
        _reconcile_pending_root = None
        _reconcile_pending_pacing = None

    def _worker() -> None:
        global _reconcile_running, _reconcile_pending_root, _reconcile_pending_pacing
        global _startup_hold
        current = root
        current_pacing = pacing
        if current_pacing is not None and current_pacing.low_priority:
            _set_current_thread_low_priority()
        while True:
            try:
                if current_pacing is None:
                    reconcile_library(current)
                else:
                    reconcile_library(current, pacing=current_pacing)
            except Exception:  # noqa: BLE001
                logger.exception("reconcile_library crashed")
            with _reconcile_lock:
                pending = _reconcile_pending_root
                pending_pacing = _reconcile_pending_pacing
                _reconcile_pending_root = None
                _reconcile_pending_pacing = None
                if pending is None or _shutdown_requested:
                    _reconcile_running = False
                    _startup_hold = False
                    break
                current = pending
                current_pacing = pending_pacing
                if current_pacing is not None and current_pacing.low_priority:
                    _set_current_thread_low_priority()
        _notify_reconcile_idle()

    thread = threading.Thread(
        target=_worker, name="library-reconcile", daemon=True
    )
    _reconcile_thread = thread
    thread.start()
    logger.info("reconcile_library started in background")
    return True


def resolve_library_games(library_root: str | Path) -> list[dict[str, object]]:
    """
    Unified game list for the Library sidebar (Database Read Projection).

    ARCHITECTURE RULE: must not call ``resolve_games`` / filesystem
    ``list_games``. Built from ``games`` + ``mods`` aggregates via
    :func:`services.game_sidebar.build_game_sidebar_view_models`.
    """
    from services.game_sidebar import build_game_sidebar_view_models
    from services.game_status import ModStatusHint
    from services.mod_library_cache import build_library_snapshot

    # Snapshot supplies mod counts / content hints from DB Layer-1 rows only.
    snap = build_library_snapshot(library_root)
    counts: dict[str, int] = {}
    hints: list[ModStatusHint] = []
    for card in snap.cards:
        key = str(card.game_folder or "").strip()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        cat = ""
        tags = str(card.category_tags or "").split()
        if tags:
            cat = str(tags[0]).strip()
        hints.append(
            ModStatusHint(
                game_folder=key,
                content_status=str(card.content_status or "") or "healthy",
                identity_status=str(getattr(card, "identity_status", "") or "ok"),
                category=cat,
                folder_absent=bool(card.folder_absent),
            )
        )
    return [
        e.to_dict()
        for e in build_game_sidebar_view_models(mod_counts=counts, mod_hints=hints)
    ]
