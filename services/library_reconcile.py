"""Library reconciliation — disk + SQLite + backup into one consistent view.

Identity: bind existing Workspace ID, or create from official platform identity
(Steam Workshop ID / Nexus Mod ID → external_id → workspace_id).
Never mint Workspace ID from Internal ID. Unrecognized folders stay unresolved.
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
    CONTENT_IDENTITY_CONFLICT,
    LIBRARY_STATUS_BACKUP_INVALID,
    LIBRARY_STATUS_CONFLICT,
    LIBRARY_STATUS_IMPORTED,
    LIBRARY_STATUS_MISSING,
    LIBRARY_STATUS_NORMAL,
    SOURCE_EXTERNAL,
    compute_content_status,
    content_status_to_library_status,
    infer_initial_source_type,
    normalize_library_source,
    row_source_type,
)
from services.metadata_backup import BACKUP_DIR_NAME, mark_missing
from services.metadata_backup_sync import sync_after_metadata_change
from services.mod_identity import (
    ensure_mod_identity,
    read_internal_id,
    resolve_existing_mod_id,
)

logger = logging.getLogger(__name__)

# Re-export legacy constants for older tests / callers
__all__ = [
    "LIBRARY_STATUS_NORMAL",
    "LIBRARY_STATUS_MISSING",
    "LIBRARY_STATUS_IMPORTED",
    "LIBRARY_STATUS_CONFLICT",
    "LIBRARY_STATUS_BACKUP_INVALID",
    "ReconcileResult",
    "reconcile_library",
    "start_reconcile_library_async",
    "resolve_library_games",
    "hold_library_load_until_reconcile_idle",
    "release_startup_library_hold",
    "library_load_must_wait",
    "is_reconcile_running",
    "add_reconcile_idle_listener",
    "request_reconcile_shutdown",
    "join_reconcile_thread",
]

_reconcile_lock = threading.Lock()
_reconcile_running = False
_reconcile_pending_root: str | None = None
# True after MainWindow schedules the startup reconcile QTimer, until that
# run actually starts (or is released). Prevents LibraryLoadWorker from
# racing the first QTimer.singleShot(0) reconcile.
_startup_hold = False
_idle_listeners: list = []
_shutdown_requested = False
_reconcile_thread: threading.Thread | None = None


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
    mod_id: str | None = None,
    db=None,
) -> bool:
    from services.local_file_index import has_local_mod_payload

    return not has_local_mod_payload(folder, mod_id=mod_id, db=db)


def reconcile_library(library_root: str | Path | None = None) -> ReconcileResult:
    """
    Unify disk / SQLite / backup state.

    1. Scan ``mod/<game>/<mod>`` with ``.info`` → ensure identity → sync backup
    2. Mark backup-only mods as ``folder_present=0``
    3. Import brand-new external folders into SQLite stubs

    Sticky ``source_type`` is preserved across refreshes; ``content_status``
    is recomputed every pass.
    """
    root = Path(library_root) if library_root else Path(default_mod_library())
    result = ReconcileResult()
    if not root.is_dir():
        root.mkdir(parents=True, exist_ok=True)

    from core.db_manager import get_db
    from core.paths import data_dir
    from services.identity_service import LIFECYCLE_RECONCILE, current_lifecycle, lifecycle_scope

    if current_lifecycle() != LIFECYCLE_RECONCILE:
        with lifecycle_scope(LIFECYCLE_RECONCILE):
            return reconcile_library(library_root)

    db = get_db()
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

    # --- Step 1 + 3: disk mods ---
    with ModTimingGuard(reason="reconcile") as _mod_timer:
        for folder in managed_folders:
            result.scanned += 1
            _mod_timer.begin(str(folder))
            t_scan = _time.perf_counter()
            raw = dict(read_info_metadata_dict(folder) or {})
            add_scan_ms((_time.perf_counter() - t_scan) * 1000.0)
            raw["_managed_path"] = str(folder.resolve())
            raw["_folder_name"] = folder.name
            t_ident = _time.perf_counter()
            had_row = bool(resolve_existing_mod_id(raw, db=db))
            mod_id, payload, changed = ensure_mod_identity(folder, raw, db=db)
            add_identity_ms((_time.perf_counter() - t_ident) * 1000.0)
            note_mod_id(mod_id)
            early_drift = None
            prev_before_drift = ""
            if mod_id.isdigit():
                prev_before_drift = str(
                    (db.get_mod_backup_row(mod_id) or {}).get("last_known_path") or ""
                ).strip()
                from services.path_lifecycle import detect_path_drift as _drift_before_create

                early_drift = _drift_before_create(mod_id, folder, db=db)
            if not mod_id.isdigit():
                from services.identity_service import (
                    create_mod_identity,
                    has_official_platform_identity,
                    is_empty_mod_placeholder,
                    sidecar_published_file_id,
                )

                plat = normalize_platform_if_known(
                    str(payload.get("source_type") or payload.get("platform") or "")
                )
                url = str(payload.get("url") or payload.get("source_url") or "").strip()
                ext = str(payload.get("external_id") or "").strip()
                ws_hint = str(payload.get("workspace_id") or "").strip()
                folder_title = str(payload.get("title") or folder.name)
                placeholder = is_empty_mod_placeholder(folder.name) or is_empty_mod_placeholder(
                    folder_title
                )
                official = has_official_platform_identity(
                    platform=plat,
                    external_id=ext,
                    source_url=url,
                    workshop_id=ws_hint if plat == PLATFORM_STEAM else "",
                )
                if placeholder and not official:
                    result.notes.append(f"IDENTITY_UNRESOLVED_PLACEHOLDER: {folder}")
                    if changed:
                        try:
                            persist_unified_metadata_dict(
                                folder, payload, sync_backup=False, sync_reason="unresolved"
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "persist unresolved placeholder failed for %s: %s", folder, exc
                            )
                    continue
                try:
                    app_id = int(payload.get("app_id") or 0)
                except (TypeError, ValueError):
                    app_id = 0
                game_name = str(payload.get("game_name") or folder.parent.name)
                if app_id <= 0 and game_name:
                    try:
                        for game in db.list_games():
                            names = {
                                str(game.name or "").strip(),
                                str(getattr(game, "folder_name", "") or "").strip(),
                            }
                            if game_name in names:
                                app_id = int(game.app_id or 0)
                                break
                    except Exception:  # noqa: BLE001
                        pass
                if app_id > 0 and game_name:
                    try:
                        from core.game_info import GameInfo

                        db.upsert_game(
                            GameInfo(app_id=app_id, name=game_name, folder_name=game_name)
                        )
                    except Exception:  # noqa: BLE001
                        pass
                created_id = ""
                rebound = False
                if ws_hint and not is_internal_mod_id(ws_hint):
                    occupied = db.find_mod_by_workspace_id(
                        ws_hint, platform=plat or None, app_id=app_id
                    )
                    if occupied:
                        created_id = occupied
                        rebound = True
                try:
                    if created_id.isdigit():
                        pass
                    elif plat == PLATFORM_STEAM:
                        from core.mod_platform import steam_workshop_url as _swu
                        from urllib.parse import parse_qs, urlparse

                        wid = ext if ext.isdigit() and not is_internal_mod_id(ext) else ""
                        if not wid and ws_hint.isdigit() and not is_internal_mod_id(ws_hint):
                            wid = ws_hint
                        if not wid and "id=" in url:
                            parsed_id = parse_qs(urlparse(url).query).get("id", [""])[0]
                            if parsed_id.isdigit() and not is_internal_mod_id(parsed_id):
                                wid = parsed_id
                        if wid.isdigit() and not is_internal_mod_id(wid) and official:
                            created = create_mod_identity(
                                db,
                                platform=PLATFORM_STEAM,
                                workshop_id=wid,
                                external_id=wid,
                                source_url=url or _swu(wid),
                                title=str(payload.get("title") or folder.name),
                                app_id=app_id,
                                game_name=game_name,
                                operation="reconcile",
                            )
                            created_id = created.mod_id
                    elif plat and official and (url or ext):
                        created = create_mod_identity(
                            db,
                            platform=plat,
                            external_id=ext,
                            source_url=url,
                            title=str(payload.get("title") or folder.name),
                            app_id=app_id,
                            game_name=game_name,
                            operation="reconcile",
                        )
                        created_id = created.mod_id
                except Exception as exc:  # noqa: BLE001
                    logger.info("reconcile identity create skipped for %s: %s", folder, exc)
                if created_id.isdigit():
                    mod_id = created_id
                    payload["published_file_id"] = sidecar_published_file_id(
                        mod_id=mod_id,
                        platform=plat,
                        external_id=ext or (created_id if plat == PLATFORM_STEAM else ""),
                    )
                    payload["identity_status"] = "complete"
                    changed = True
                    had_row = rebound
                else:
                    result.notes.append(f"IDENTITY_UNRESOLVED: {folder}")
                    if changed:
                        try:
                            persist_unified_metadata_dict(
                                folder, payload, sync_backup=False, sync_reason="unresolved"
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "persist unresolved identity failed for %s: %s", folder, exc
                            )
                    continue

            if changed:
                try:
                    persist_unified_metadata_dict(
                        folder, payload, sync_backup=False, sync_reason="import"
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("persist identity failed for %s: %s", folder, exc)

            id_to_paths.setdefault(mod_id, []).append(folder)
            seen_ids.add(mod_id)

            prev = db.get_mod_backup_row(mod_id)
            prev_path = prev_before_drift or str((prev or {}).get("last_known_path") or "").strip()
            renamed = early_drift is not None and early_drift.success
            if not renamed:
                from services.path_lifecycle import detect_path_drift

                drift = detect_path_drift(mod_id, folder, db=db)
                renamed = drift is not None and drift.success
            if renamed:
                result.renamed += 1
            elif prev_path and Path(prev_path) != folder:
                if prev_path and Path(prev_path).is_dir() and Path(prev_path) != folder:
                    pass
                else:
                    result.renamed += 1

            title = str(payload.get("title") or payload.get("display_name") or folder.name)
            payload_source = str(
                payload.get("source_type") or payload.get("platform") or ""
            )
            store_platform = normalize_platform_if_known(payload_source) or (
                normalize_platform(payload_source) if payload_source else ""
            )
            source_type = infer_initial_source_type(
                mod_id=mod_id,
                had_row=had_row,
                existing_source=str((prev or {}).get("source_type") or ""),
                existing_platform=str((prev or {}).get("platform") or ""),
                payload_source=payload_source,
            )
            if not had_row:
                result.imported += 1

            content_status = compute_content_status(
                folder_present=True,
                missing_content=_folder_missing_content(folder),
                metadata_missing=not _folder_has_metadata(folder),
                backup_status=str((prev or {}).get("backup_status") or ""),
            )
            library_status = content_status_to_library_status(content_status)

            from services.identity_service import persist_identity, persist_workspace_id
            from services.mod_identity import source_url_embeds_internal
            from services.mod_identity_authority import sanitize_platform_external_id

            raw_url = str(payload.get("url") or payload.get("source_url") or "").strip()
            if source_url_embeds_internal(raw_url, internal_pk=mod_id):
                raw_url = ""
            ws = persist_workspace_id(
                platform=store_platform,
                mod_id=mod_id,
                workspace_id=str(payload.get("workspace_id") or ""),
                source_url=raw_url,
                external_id=str(payload.get("external_id") or ""),
            )
            ext = sanitize_platform_external_id(
                store_platform, str(payload.get("external_id") or ""), mod_id=mod_id
            )
            persist_kw: dict = {
                "internal_id": read_internal_id(payload),
                "source_type": source_type,
                "content_status": content_status,
                "library_status": library_status,
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
                    mod_id,
                    source="reconcile",
                    reason="bind",
                    **persist_kw,
                )
            except Exception:  # noqa: BLE001
                logger.debug("update identity fields failed for %s", mod_id, exc_info=True)

            if sync_after_metadata_change(mod_id, folder, "restore" if had_row else "import"):
                result.synced += 1
                if had_row and renamed:
                    result.restored += 1

    # Identity conflicts: same mod_id → multiple live folders
    for mid, paths in id_to_paths.items():
        live = [p for p in paths if p.is_dir()]
        if len(live) > 1:
            result.conflicts += 1
            try:
                db.update_mod_identity_fields(
                    mid,
                    content_status=CONTENT_IDENTITY_CONFLICT,
                    library_status=LIBRARY_STATUS_CONFLICT,
                    folder_present=True,
                )
            except Exception:  # noqa: BLE001
                pass

    # --- Step 2: backup / DB rows without disk ---
    try:
        for row in db.iter_mod_backup_rows():
            mid = str(row.get("mod_id") or "").strip()
            if not mid.isdigit() or mid in seen_ids:
                continue
            lkp = str(row.get("last_known_path") or "").strip()
            path = Path(lkp) if lkp else None
            if path is not None and path.is_dir():
                if sync_after_metadata_change(mid, path, "restore"):
                    result.synced += 1
                    result.restored += 1
                seen_ids.add(mid)
                continue
            mark_missing(mid)
            try:
                bstatus = str(row.get("backup_status") or "").strip()
                content_status = compute_content_status(
                    folder_present=False,
                    backup_status=bstatus,
                )
                existing_source = row_source_type(row)
                db.update_mod_identity_fields(
                    mid,
                    source_type=existing_source if existing_source else None,
                    content_status=content_status,
                    library_status=content_status_to_library_status(content_status),
                    folder_present=False,
                )
            except Exception:  # noqa: BLE001
                pass
            result.missing += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile missing-pass failed: %s", exc)

    # Orphan backup dirs on disk not yet in SQLite.
    # Backup folder name is Internal/Steam PK storage — never Steam CREATE proof.
    try:
        from services.metadata_backup import load_backup

        def _under_library(path_text: str) -> bool:
            text = str(path_text or "").strip()
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
                    result.notes.append(f"IDENTITY_UNRESOLVED backup: {mid}")
                    continue
                meta_file = child / "metadata.json"
                if not meta_file.is_file():
                    continue
                try:
                    snap = load_backup(mid)
                    if snap is None:
                        continue
                    meta = snap.metadata
                    lkp = str(snap.last_known_path or "").strip()
                    if db.get_mod(mid) is None and lkp and not _under_library(lkp):
                        continue
                    if db.get_mod(mid) is None and not lkp:
                        result.notes.append(f"IDENTITY_UNRESOLVED backup: {mid}")
                        continue
                    title = str(
                        meta.get("title")
                        or meta.get("display_name")
                        or f"Unknown_Mod_{mid}"
                    )
                    payload_source = str(
                        meta.get("source_type") or meta.get("platform") or ""
                    )
                    store_platform = normalize_platform_if_known(payload_source) or (
                        normalize_platform(payload_source) if payload_source else ""
                    )
                    source_type = infer_initial_source_type(
                        mod_id=mid,
                        had_row=False,
                        payload_source=payload_source,
                    )
                    if source_type == SOURCE_EXTERNAL and store_platform:
                        source_type = normalize_library_source(store_platform)
                    from services.identity_service import (
                        create_mod_identity,
                        has_official_platform_identity,
                        persist_identity,
                    )

                    url = str(meta.get("url") or meta.get("source_url") or "")
                    ext = str(meta.get("external_id") or "")
                    ws_meta = str(meta.get("workspace_id") or "").strip()
                    steam_wid = ""
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
                    if db.get_mod(mid) is None:
                        if not official or not store_platform:
                            result.notes.append(f"IDENTITY_UNRESOLVED backup: {mid}")
                            continue
                        try:
                            if store_platform == PLATFORM_STEAM:
                                if not steam_wid:
                                    result.notes.append(
                                        f"IDENTITY_UNRESOLVED backup: {mid}"
                                    )
                                    continue
                                created = create_mod_identity(
                                    db,
                                    platform=PLATFORM_STEAM,
                                    workshop_id=steam_wid,
                                    external_id=steam_wid,
                                    source_url=url,
                                    title=title,
                                    app_id=int(meta.get("app_id") or 0),
                                    game_name=str(meta.get("game_name") or ""),
                                    operation="reconcile",
                                )
                            else:
                                created = create_mod_identity(
                                    db,
                                    platform=store_platform,
                                    external_id=ext,
                                    source_url=url,
                                    title=title,
                                    app_id=int(meta.get("app_id") or 0),
                                    game_name=str(meta.get("game_name") or ""),
                                    operation="reconcile",
                                )
                            mid = created.mod_id
                        except Exception as exc:  # noqa: BLE001
                            logger.info(
                                "orphan backup identity create skipped for %s: %s",
                                mid,
                                exc,
                            )
                            result.notes.append(f"IDENTITY_UNRESOLVED backup: {mid}")
                            continue
                    present = bool(lkp and Path(lkp).is_dir())
                    content_status = compute_content_status(folder_present=present)
                    persist_identity(
                        db,
                        mid,
                        source="reconcile",
                        reason="backup_orphan",
                        internal_id=read_internal_id(meta) or None,
                        source_type=source_type,
                        content_status=content_status,
                        library_status=content_status_to_library_status(content_status),
                        last_known_path=lkp,
                        folder_present=present,
                        sticky_source=True,
                    )
                    if present:
                        sync_after_metadata_change(mid, lkp, "restore")
                        result.synced += 1
                    else:
                        result.missing += 1
                    seen_ids.add(mid)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "orphan backup import failed for %s", mid, exc_info=True
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning("orphan backup scan failed: %s", exc)

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
        "reconcile_library done scanned=%s synced=%s imported=%s missing=%s "
        "renamed=%s conflicts=%s restored=%s root=%s",
        result.scanned,
        result.synced,
        result.imported,
        result.missing,
        result.renamed,
        result.conflicts,
        result.restored,
        root,
    )
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
    global _reconcile_running, _reconcile_pending_root, _startup_hold
    global _shutdown_requested, _reconcile_thread
    with _reconcile_lock:
        _reconcile_running = False
        _reconcile_pending_root = None
        _startup_hold = False
        _shutdown_requested = False
        _reconcile_thread = None
        _idle_listeners.clear()


def request_reconcile_shutdown() -> None:
    """Refuse new / queued reconcile runs. Does not abort an in-flight pass."""
    global _shutdown_requested, _reconcile_pending_root
    with _reconcile_lock:
        _shutdown_requested = True
        _reconcile_pending_root = None


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


def start_reconcile_library_async(library_root: str | Path | None = None) -> bool:
    """Run :func:`reconcile_library` on a daemon thread (non-blocking).

    Concurrent callers are coalesced: if a run is in progress, the latest
    *library_root* is queued and executed once after the current run finishes.
    """
    global _reconcile_running, _reconcile_pending_root, _startup_hold
    global _reconcile_thread
    root = str(library_root) if library_root else None
    with _reconcile_lock:
        if _shutdown_requested:
            logger.info("reconcile_library skipped; shutdown in progress")
            return False
        _startup_hold = False
        if _reconcile_running:
            _reconcile_pending_root = root
            logger.info("reconcile_library already running; queued follow-up")
            try:
                from services.startup_io_trace import note_reconcile_queued

                note_reconcile_queued()
            except Exception:  # noqa: BLE001
                pass
            return False
        _reconcile_running = True
        _reconcile_pending_root = None

    def _worker() -> None:
        global _reconcile_running, _reconcile_pending_root, _startup_hold
        current = root
        while True:
            try:
                reconcile_library(current)
            except Exception:  # noqa: BLE001
                logger.exception("reconcile_library crashed")
            with _reconcile_lock:
                pending = _reconcile_pending_root
                _reconcile_pending_root = None
                if pending is None or _shutdown_requested:
                    _reconcile_running = False
                    _startup_hold = False
                    break
                current = pending
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
    Unified game list for the Library sidebar.

    Delegates to :func:`services.game_library.resolve_games` (filesystem >
    backup history > games table). Snapshot cards supply accurate counts
    and ``content_status`` hints for aggregation (no extra disk scans).
    """
    from services.game_library import resolve_games_as_dicts
    from services.game_status import ModStatusHint
    from services.mod_library_cache import build_library_snapshot

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
                category=cat,
                folder_absent=bool(card.folder_absent),
            )
        )
    return resolve_games_as_dicts(
        library_root, mod_counts=counts, mod_hints=hints
    )
