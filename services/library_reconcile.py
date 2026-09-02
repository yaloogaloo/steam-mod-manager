"""Library reconciliation — disk + SQLite + backup into one consistent view."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from core.mod_platform import (
    PLATFORM_STEAM,
    is_internal_mod_id,
    normalize_platform,
    normalize_platform_if_known,
)
from core.models import ModMetadata
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
    is_steam_workshop_id,
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
]

_reconcile_lock = threading.Lock()
_reconcile_running = False
_reconcile_pending_root: str | None = None


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

    db = get_db()
    manager = ModFileManager(root)
    seen_ids: set[str] = set()
    id_to_paths: dict[str, list[Path]] = {}

    # --- Step 1 + 3: disk mods ---
    for folder in manager.list_managed_mods():
        result.scanned += 1
        raw = read_info_metadata_dict(folder) or {}
        folder_key = str(folder.resolve())
        if not resolve_existing_mod_id(raw):
            path_mid = db.find_mod_by_last_known_path(folder_key)
            if path_mid:
                raw = dict(raw)
                raw.setdefault("published_file_id", path_mid)
                raw["_managed_path"] = folder_key
        had_row = bool(resolve_existing_mod_id(raw))
        mod_id, payload, changed = ensure_mod_identity(folder, raw)
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
            folder_title = str(payload.get("title") or folder.name)
            placeholder = is_empty_mod_placeholder(folder.name) or is_empty_mod_placeholder(
                folder_title
            )
            official = has_official_platform_identity(
                platform=plat,
                external_id=ext,
                source_url=url,
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
            created_id = ""
            try:
                if plat == PLATFORM_STEAM:
                    from core.mod_platform import steam_workshop_url as _swu
                    from urllib.parse import parse_qs, urlparse

                    wid = ext if ext.isdigit() and not is_internal_mod_id(ext) else ""
                    if not wid and "id=" in url:
                        wid = parse_qs(urlparse(url).query).get("id", [""])[0]
                    if wid.isdigit() and not is_internal_mod_id(wid):
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
                elif plat and (url or ext):
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
                    external_id=ext,
                ) or (mod_id if plat != PLATFORM_STEAM else "")
                payload["identity_status"] = "complete"
                changed = True
                had_row = False
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
        prev_path = str((prev or {}).get("last_known_path") or "").strip()
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

        try:
            if is_steam_workshop_id(mod_id):
                db.upsert_mod(
                    ModMetadata(
                        published_file_id=mod_id,
                        title=title,
                        description=str(payload.get("description") or ""),
                        game_name=str(payload.get("game_name") or folder.parent.name),
                        managed_path=str(folder.resolve()),
                        app_id=int(payload.get("app_id") or 0),
                        source_type=store_platform or source_type,
                        url=str(payload.get("url") or payload.get("source_url") or ""),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("upsert stub failed for %s: %s", mod_id, exc)

        content_status = compute_content_status(
            folder_present=True,
            missing_content=_folder_missing_content(folder),
            metadata_missing=not _folder_has_metadata(folder),
            backup_status=str((prev or {}).get("backup_status") or ""),
        )
        library_status = content_status_to_library_status(content_status)

        from services.identity_service import persist_workspace_id
        from services.mod_identity_authority import sanitize_platform_external_id

        ws = persist_workspace_id(
            platform=store_platform,
            mod_id=mod_id,
            workspace_id=str(payload.get("workspace_id") or ""),
            source_url=str(payload.get("url") or payload.get("source_url") or ""),
            external_id=str(payload.get("external_id") or ""),
        )
        ext = sanitize_platform_external_id(
            store_platform, str(payload.get("external_id") or ""), mod_id=mod_id
        )
        try:
            db.update_mod_identity_fields(
                mod_id,
                internal_id=read_internal_id(payload),
                source_type=source_type,
                content_status=content_status,
                library_status=library_status,
                last_known_path=str(folder.resolve()),
                folder_present=True,
                title=title,
                platform=store_platform or None,
                source_url=str(payload.get("url") or payload.get("source_url") or "")
                or None,
                external_id=ext or None,
                workspace_id=ws,
                app_id=int(payload.get("app_id") or 0) or None,
                sticky_source=True,
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

    # Orphan backup dirs on disk not yet in SQLite
    try:
        from services.metadata_backup import load_backup

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
                    db.upsert_mod(
                        ModMetadata(
                            published_file_id=mid,
                            title=title,
                            description=str(meta.get("description") or ""),
                            game_name=str(meta.get("game_name") or ""),
                            managed_path=str(snap.last_known_path or ""),
                            app_id=int(meta.get("app_id") or 0),
                            source_type=store_platform or source_type,
                            url=str(meta.get("url") or meta.get("source_url") or ""),
                        )
                    )
                    lkp = str(snap.last_known_path or "").strip()
                    present = bool(lkp and Path(lkp).is_dir())
                    content_status = compute_content_status(folder_present=present)
                    db.update_mod_identity_fields(
                        mid,
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
    return result


def start_reconcile_library_async(library_root: str | Path | None = None) -> bool:
    """Run :func:`reconcile_library` on a daemon thread (non-blocking).

    Concurrent callers are coalesced: if a run is in progress, the latest
    *library_root* is queued and executed once after the current run finishes.
    """
    global _reconcile_running, _reconcile_pending_root
    root = str(library_root) if library_root else None
    with _reconcile_lock:
        if _reconcile_running:
            _reconcile_pending_root = root
            logger.info("reconcile_library already running; queued follow-up")
            return False
        _reconcile_running = True
        _reconcile_pending_root = None

    def _worker() -> None:
        global _reconcile_running, _reconcile_pending_root
        current = root
        while True:
            try:
                reconcile_library(current)
            except Exception:  # noqa: BLE001
                logger.exception("reconcile_library crashed")
            with _reconcile_lock:
                pending = _reconcile_pending_root
                _reconcile_pending_root = None
                if pending is None:
                    _reconcile_running = False
                    return
                current = pending

    threading.Thread(
        target=_worker, name="library-reconcile", daemon=True
    ).start()
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
