"""Independent Mod metadata backup — survives manual deletion of the Mod folder."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.paths import data_dir
from services.backup_identity import (  # noqa: F401
    BACKUP_DIR_NAME,
    BackupIdentityError,
    backup_read_root,
    is_frozen_backup_uuid,
    prove_frozen_backup_key,
    resolve_backup_mod_pk,
    resolve_backup_storage_key,
    write_backup_root_for,
)
from services.file_ops import (
    INFO_DIR_NAME,
    METADATA_FILENAME,
    read_info_metadata_dict,
)
from services.offline.paths import resolve_offline_page

logger = logging.getLogger(__name__)

BACKUP_METADATA_NAME = "metadata.json"
BACKUP_COVER_BASENAME = "cover"
BACKUP_OFFLINE_DIR = "offline"
BACKUP_OFFLINE_INDEX = "index.html"


@dataclass(frozen=True)
class BackupSnapshot:
    """Loaded backup payload for UI when the Mod folder is absent."""

    mod_id: str
    metadata: dict[str, Any]
    cover_path: str = ""
    offline_path: str = ""
    last_known_path: str = ""


def backup_root(internal_id: int | str, *, mod_pk: int | str | None = None) -> Path:
    """Write root: ``data/mod_backup/<Frozen UUID>/``.

    Raises :class:`BackupIdentityError` when *internal_id* is not a Frozen UUID.
    Digit PK is never a write directory name.
    """
    key = resolve_backup_storage_key(internal_id=internal_id, mod_pk=mod_pk)
    return data_dir() / BACKUP_DIR_NAME / key


def readable_backup_root(
    internal_id: int | str | None,
    *,
    mod_pk: int | str | None = None,
) -> Path | None:
    """UUID directory first, legacy ``data/mod_backup/<mod_pk>/`` fallback."""
    return backup_read_root(
        internal_id=internal_id,
        mod_pk=mod_pk,
        base=data_dir() / BACKUP_DIR_NAME,
    )


def prove_backup_storage_key(
    hint: str | int | None = None,
    *,
    managed_path: str | Path | None = None,
    info: Mapping[str, Any] | None = None,
) -> str:
    """Prove the Backup **write** storage key (Frozen UUID).

    Never returns a digit PK. Unresolved / collapsed / synthetic → ``""``
    (caller must not write).
    """
    key = prove_frozen_backup_key(hint, managed_path=managed_path, info=info)
    if key:
        return key
    logger.warning(
        "backup write refused: storage key unresolved hint=%s path=%s",
        str(hint or "").strip() or "?",
        managed_path,
    )
    return ""


def _resolve_mod_id(mod_path: Path, data: dict[str, Any] | None) -> str:
    """Resolve Frozen UUID for backup write — never a digit PK or folder name."""
    return prove_frozen_backup_key(managed_path=mod_path, info=data)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    nbytes = 0
    t0 = time.perf_counter()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            nbytes += len(chunk)
            digest.update(chunk)
    try:
        from services.reconcile_observability import add_hash

        add_hash(files=1, nbytes=nbytes, ms=(time.perf_counter() - t0) * 1000.0)
    except Exception:  # noqa: BLE001
        pass
    return digest.hexdigest()


def _same_file_content(src: Path, dest: Path) -> bool:
    t0 = time.perf_counter()
    if not src.is_file() or not dest.is_file():
        try:
            from services.reconcile_observability import add_compare_ms

            add_compare_ms((time.perf_counter() - t0) * 1000.0)
        except Exception:  # noqa: BLE001
            pass
        return False
    try:
        src_st = src.stat()
        dest_st = dest.stat()
        if src_st.st_size != dest_st.st_size:
            try:
                from services.reconcile_observability import add_compare_ms, note_size_mismatch

                note_size_mismatch()
                add_compare_ms((time.perf_counter() - t0) * 1000.0)
            except Exception:  # noqa: BLE001
                pass
            return False
        mtime_equal = src_st.st_mtime == dest_st.st_mtime
        try:
            from services.reconcile_observability import (
                add_compare_ms,
                note_size_match_then_hash,
            )

            note_size_match_then_hash(mtime_equal=mtime_equal)
            add_compare_ms((time.perf_counter() - t0) * 1000.0)
        except Exception:  # noqa: BLE001
            pass
        return _file_sha256(src) == _file_sha256(dest)
    except OSError:
        return False


def _find_info_cover(info_dir: Path) -> Path | None:
    if not info_dir.is_dir():
        return None
    for pattern in ("cover.*", "preview.*"):
        for candidate in sorted(info_dir.glob(pattern)):
            if candidate.is_file():
                return candidate
    return None


def _clear_backup_covers(dest_dir: Path) -> None:
    try:
        for old in dest_dir.glob(f"{BACKUP_COVER_BASENAME}.*"):
            if old.is_file():
                old.unlink()
    except OSError as exc:
        logger.warning("Failed to clear backup covers in %s: %s", dest_dir, exc)


def _copy_cover(src: Path | None, dest_dir: Path) -> str:
    """
    Mirror cover into backup dir; return absolute path or ``""``.

    When *src* is missing, remove any existing backup cover (snapshot semantics).
    Idempotent: skips copy when sha256 matches the canonical target.
    """
    if src is None or not src.is_file():
        _clear_backup_covers(dest_dir)
        return ""
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower() or ".jpg"
    dest = dest_dir / f"{BACKUP_COVER_BASENAME}{ext}"
    try:
        for old in dest_dir.glob(f"{BACKUP_COVER_BASENAME}.*"):
            if old.is_file() and old.resolve() != dest.resolve():
                old.unlink()
    except OSError:
        pass
    try:
        if _same_file_content(src, dest):
            return str(dest.resolve())
        t_copy = time.perf_counter()
        shutil.copy2(src, dest)
        try:
            from services.reconcile_observability import add_copy

            add_copy(
                files=1,
                nbytes=int(dest.stat().st_size) if dest.is_file() else 0,
                ms=(time.perf_counter() - t_copy) * 1000.0,
            )
        except Exception:  # noqa: BLE001
            pass
        return str(dest.resolve())
    except OSError as exc:
        logger.warning("Failed to copy cover to backup %s: %s", dest, exc)
        return ""


def _clear_backup_offline(dest_offline: Path) -> None:
    try:
        if dest_offline.is_dir():
            shutil.rmtree(dest_offline)
    except OSError as exc:
        logger.warning("Failed to clear backup offline %s: %s", dest_offline, exc)


def _copy_offline_index(src_index: Path | None, dest_offline: Path) -> str:
    """
    Snapshot the canonical offline page plus its local dependency closure.

    Source is whatever OPEN already accepts: ``.info/offline/index.html`` first,
    then legacy Steam ``.info/index.html``. Never copytree ``.info`` or unused
    ``assets/``. Missing live index clears backup offline so a later snapshot
    cannot invent a page.
    """
    index = src_index if src_index is not None and src_index.is_file() else None
    if index is None:
        _clear_backup_offline(dest_offline)
        return ""
    from services.offline.backup_closure import snapshot_offline_closure

    t_copy = time.perf_counter()
    try:
        copied = snapshot_offline_closure(index, dest_offline)
    except OSError as exc:
        logger.warning("Failed to copy offline backup %s: %s", dest_offline, exc)
        return ""
    try:
        from services.reconcile_observability import add_copy

        backup_index = dest_offline / BACKUP_OFFLINE_INDEX
        nbytes = int(backup_index.stat().st_size) if backup_index.is_file() else 0
        add_copy(
            files=1,
            nbytes=nbytes,
            ms=(time.perf_counter() - t_copy) * 1000.0,
        )
    except Exception:  # noqa: BLE001
        pass
    return copied


def snapshot_from_mod_folder(
    mod_path: str | Path,
    *,
    owner_mod_id: str | int | None = None,
) -> BackupSnapshot | None:
    """
    Read ``.info/metadata.json`` and mirror cover / a usable offline snapshot
    into ``data/mod_backup/``. Discovers the same files OPEN uses
    (``.info/offline/index.html``, then legacy ``.info/index.html``) and stores
    ``offline/index.html`` plus the local dependency closure. Never copytree
    ``.info`` or unused ``assets/``.

    Never writes back to the Mod folder. Missing ``.info`` assets delete matching
    backup assets (folder-absent is handled by callers — not this function).

    ``owner_mod_id`` is the Internal Database ID (caller-proven). When omitted,
    ownership is resolved from ``.info/internal_id`` only — never from
    published_file_id / workspace_id / folder name.
    """
    root = Path(mod_path)
    if not root.is_dir():
        return None

    t_scan = time.perf_counter()
    data = read_info_metadata_dict(root) or {}
    frozen = prove_backup_storage_key(owner_mod_id, managed_path=root, info=data)
    try:
        from services.reconcile_observability import add_scan_ms

        add_scan_ms((time.perf_counter() - t_scan) * 1000.0)
    except Exception:  # noqa: BLE001
        pass
    if not is_frozen_backup_uuid(frozen):
        return None

    owner_pk = resolve_backup_mod_pk(
        internal_id=frozen,
        mod_pk=str(owner_mod_id or "").strip() or None,
    )
    dest = backup_root(frozen, mod_pk=owner_pk or None)
    dest.mkdir(parents=True, exist_ok=True)

    meta_file = dest / BACKUP_METADATA_NAME
    t_meta = time.perf_counter()
    meta_text = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        if meta_file.is_file():
            existing = meta_file.read_text(encoding="utf-8")
            try:
                from services.reconcile_observability import add_compare_ms

                add_compare_ms((time.perf_counter() - t_meta) * 1000.0)
            except Exception:  # noqa: BLE001
                pass
            if existing != meta_text:
                t_write = time.perf_counter()
                meta_file.write_text(meta_text, encoding="utf-8")
                try:
                    from services.reconcile_observability import add_copy, note_metadata_rewritten

                    note_metadata_rewritten()
                    add_copy(
                        files=1,
                        nbytes=len(meta_text.encode("utf-8")),
                        ms=(time.perf_counter() - t_write) * 1000.0,
                    )
                except Exception:  # noqa: BLE001
                    pass
        else:
            t_write = time.perf_counter()
            meta_file.write_text(meta_text, encoding="utf-8")
            try:
                from services.reconcile_observability import add_copy, note_metadata_rewritten

                note_metadata_rewritten()
                add_copy(
                    files=1,
                    nbytes=len(meta_text.encode("utf-8")),
                    ms=(time.perf_counter() - t_write) * 1000.0,
                )
            except Exception:  # noqa: BLE001
                pass
    except OSError as exc:
        logger.warning("Failed to write backup metadata for %s: %s", frozen, exc)
        return None

    info_dir = root / INFO_DIR_NAME
    t_assets = time.perf_counter()
    cover_src = _find_info_cover(info_dir)
    offline_src = resolve_offline_page(root)
    try:
        from services.reconcile_observability import add_scan_ms

        add_scan_ms((time.perf_counter() - t_assets) * 1000.0)
    except Exception:  # noqa: BLE001
        pass
    cover_abs = _copy_cover(cover_src, dest)
    offline_abs = _copy_offline_index(offline_src, dest / BACKUP_OFFLINE_DIR)

    return BackupSnapshot(
        mod_id=owner_pk or frozen,
        metadata=data,
        cover_path=cover_abs,
        offline_path=offline_abs,
        last_known_path=str(root.resolve()),
    )


def load_backup(mod_id: int | str) -> BackupSnapshot | None:
    """Load backup metadata + asset paths. *mod_id* may be Frozen UUID or PK.

    Prefers ``data/mod_backup/<UUID>/``, then legacy ``data/mod_backup/<PK>/``.
    """
    token = str(mod_id).strip()
    if not token:
        return None
    dest = readable_backup_root(token)
    if dest is None:
        return None
    meta_file = dest / BACKUP_METADATA_NAME
    if not meta_file.is_file():
        return None
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read backup metadata for %s: %s", token, exc)
        return None
    if not isinstance(data, dict):
        return None

    cover_abs = ""
    for candidate in sorted(dest.glob(f"{BACKUP_COVER_BASENAME}.*")):
        if candidate.is_file():
            cover_abs = str(candidate.resolve())
            break

    offline_index = dest / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    offline_abs = (
        str(offline_index.resolve()) if offline_index.is_file() else ""
    )

    pk = resolve_backup_mod_pk(
        internal_id=token, mod_pk=token if token.isdigit() else None
    )
    last_known = ""
    if pk.isdigit():
        try:
            from core.db_manager import get_db

            row = get_db().get_mod_backup_row(pk)
            if row is not None:
                last_known = str(row.get("last_known_path") or "").strip()
        except Exception:  # noqa: BLE001
            pass

    return BackupSnapshot(
        mod_id=pk or token,
        metadata=data,
        cover_path=cover_abs,
        offline_path=offline_abs,
        last_known_path=last_known,
    )


def mark_missing(mod_id: int | str) -> None:
    """Mark a Mod as folder-absent in SQLite (backup remains)."""
    token = str(mod_id).strip()
    mid = resolve_backup_mod_pk(
        internal_id=token, mod_pk=token if token.isdigit() else None
    )
    if not mid.isdigit():
        return
    try:
        from core.db_manager import get_db
        from services.offline.backup_offline_repair import snapshot_live_offline_only

        db = get_db()
        row = db.get_mod_backup_row(mid)
        lkp = str((row or {}).get("last_known_path") or "").strip()
        if lkp:
            live = Path(lkp)
            if live.is_dir():
                snapshot_live_offline_only(mid, live, db=db)
        db.set_mod_folder_present(mid, present=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mark_missing failed for %s: %s", mid, exc)


def restore_info_sidecar_from_backup(
    mod_id: int | str,
    managed_path: str | Path,
    *,
    db: Any | None = None,
) -> bool:
    """
    Restore ``.info`` from backup for an **existing** DB entity only.

    ``mod_id`` may be Frozen TEXT ``internal_id`` or a PK handle. Restore
    matches ``backup.internal_id == mods.internal_id``. Backup directories
    are read UUID-first then legacy PK. Writes never create a PK directory.

    Refuses when backup identity does not match the DB row (no create, no
    guess, no fallback).
    """
    from core.db_manager import get_db
    from services.identity_service import resolve_mod_pk

    folder = Path(managed_path)
    if not folder.is_dir():
        return False
    try:
        database = db if db is not None else get_db()
        mid = resolve_mod_pk(mod_id, db=database)
    except Exception:  # noqa: BLE001
        logger.debug("restore identity resolve failed", exc_info=True)
        return False
    if not mid.isdigit() or not folder.is_dir():
        return False
    info_meta = folder / INFO_DIR_NAME / METADATA_FILENAME
    if info_meta.is_file():
        return False
    try:
        row = database.get_mod_backup_row(mid)
        if row is None:
            return False
        frozen = str(row.get("internal_id") or "").strip()
        bak_root = readable_backup_root(
            frozen if is_frozen_backup_uuid(frozen) else None,
            mod_pk=mid,
        )
        if bak_root is None:
            return False
        bak_meta = bak_root / BACKUP_METADATA_NAME
        if not bak_meta.is_file():
            return False
        payload = json.loads(bak_meta.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False

        def _t(v: Any) -> str:
            return str(v or "").strip()

        from services.mod_identity import read_internal_id

        bak_uuid = read_internal_id(payload)
        bak_ws = _t(payload.get("workspace_id"))
        bak_plat = _t(payload.get("source_type") or payload.get("platform")).lower()
        bak_app = int(payload.get("app_id") or 0)
        row_uuid = _t(row.get("internal_id"))
        row_ws = _t(row.get("workspace_id"))
        row_plat = _t(row.get("platform") or row.get("source_type")).lower()
        row_app = int(row.get("app_id") or 0)

        if bak_uuid and row_uuid and bak_uuid != row_uuid:
            logger.warning("backup restore refused mid=%s internal_id mismatch", mid)
            return False
        # Cross-game isolation: app_id is evidence, not identity — refuse foreign metadata.
        if bak_app and row_app and bak_app != row_app:
            logger.warning("backup restore refused mid=%s app_id mismatch", mid)
            return False
        if not bak_app and row_app:
            from services.metadata_owner_guard import metadata_payload_is_foreign

            if metadata_payload_is_foreign(payload, entity_app_id=row_app):
                logger.warning(
                    "backup restore refused mid=%s foreign metadata url/app evidence",
                    mid,
                )
                return False
        if bak_plat and row_plat and bak_plat != row_plat:
            logger.warning("backup restore refused mid=%s platform mismatch", mid)
            return False
        # workspace_id is display/registration only — never a restore ownership key.
        _ = bak_ws, row_ws

        # Stamp DB authority onto restored sidecar. Never collapse to PK.
        # ``.info/internal_id`` must equal Entity.internal_id after restore.
        from services.mod_identity import set_info_internal_id

        payload = set_info_internal_id(payload, row_uuid or bak_uuid)
        if row_ws:
            payload["workspace_id"] = row_ws
        if row_plat:
            payload["platform"] = row_plat
            payload["source_type"] = row_plat
        if row_app:
            payload["app_id"] = row_app

        from services.file_ops import persist_unified_metadata_dict

        persist_unified_metadata_dict(
            folder, payload, sync_backup=False, sync_reason="restore"
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("restore_info_sidecar_from_backup failed mid=%s: %s", mid, exc)
        return False


def sync_metadata_backup(
    mod_path: str | Path,
    *,
    mod_id: str | int | None = None,
) -> None:
    """
    Low-level ``.info`` → ``data/mod_backup`` snapshot.

    Prefer :func:`services.metadata_backup_sync.sync_after_metadata_change`
    for write-event callers (adds reason logging + validation status).

    ``mod_id`` may be Frozen UUID or SQLite PK. Snapshot writes go to the UUID
    directory. SQL updates still use ``mod_pk``. Frozen restore proof is
    ``backup.internal_id == mods.internal_id``.

    When the Mod folder exists: snapshot ``.info`` → backup, mark folder present.
    When absent: mark missing only (does not create backup).
    """
    root = Path(mod_path)
    if not root.is_dir():
        # Never treat folder/workspace digits as ownership — path → DB row only.
        mid = str(mod_id or "").strip()
        if is_frozen_backup_uuid(mid):
            mapped = resolve_backup_mod_pk(internal_id=mid)
            if mapped.isdigit():
                mid = mapped
        if not mid.isdigit():
            try:
                from core.db_manager import get_db

                row = get_db().get_mod_backup_row_by_path(str(root))
                if row:
                    mid = str(row.get("mod_id") or "")
                if not mid.isdigit():
                    try:
                        resolved = root.resolve()
                    except OSError:
                        resolved = root
                    row = get_db().get_mod_backup_row_by_path(str(resolved))
                    if row:
                        mid = str(row.get("mod_id") or "")
            except Exception:  # noqa: BLE001
                mid = ""
        if mid.isdigit():
            rebound = False
            try:
                from services.mod_presence import rediscover_entity_path

                found = rediscover_entity_path(mid)
                if found.success and found.path:
                    rebound_path = Path(found.path)
                    if rebound_path.is_dir():
                        root = rebound_path
                        rebound = True
            except Exception:  # noqa: BLE001
                rebound = False
            if not rebound:
                mark_missing(mid)
                return
        else:
            return

    snapshot = snapshot_from_mod_folder(root, owner_mod_id=mod_id)
    if snapshot is None:
        return

    meta_json = json.dumps(snapshot.metadata, ensure_ascii=False)
    try:
        from core.db_manager import get_db

        t_persist = time.perf_counter()
        sql_pk = str(snapshot.mod_id or "").strip()
        if not sql_pk.isdigit():
            sql_pk = resolve_backup_mod_pk(internal_id=sql_pk)
        if not sql_pk.isdigit():
            return
        get_db().update_mod_backup_snapshot(
            sql_pk,
            last_known_path=snapshot.last_known_path,
            folder_present=True,
            backup_metadata_json=meta_json,
            backup_cover_path=snapshot.cover_path,
            backup_offline_path=snapshot.offline_path,
        )
        try:
            from services.reconcile_observability import add_persist_ms

            add_persist_ms((time.perf_counter() - t_persist) * 1000.0)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "DB backup snapshot update failed for %s: %s", snapshot.mod_id, exc
        )


def reconcile_folder_presence(library_root: str | Path | None = None) -> None:
    """
    Recompute ``folder_present`` from disk.

    Delegates to batched Presence Reconcile: one scan per game root,
    rediscovery before MISS, no per-row filesystem walk.
    """
    logger.debug("reconcile_folder_presence enter")
    try:
        from services.presence_reconcile import reconcile_presence

        reconcile_presence(library_root, notify=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile_folder_presence failed: %s", exc)
    logger.debug("reconcile_folder_presence leave")


def reconcile_library_presence(
    library_root: str | Path,
    *,
    on_disk_mod_ids: set[str] | None = None,
) -> None:
    """Compatibility wrapper — presence is always reconciled from ``last_known_path``."""
    del on_disk_mod_ids
    reconcile_folder_presence(library_root)


def is_mod_folder_absent(mod_id: int | str, managed_path: str | Path | None = None) -> bool:
    """True when the managed Mod directory does not exist (disk is source of truth)."""
    if managed_path is not None:
        return not Path(managed_path).is_dir()
    mid = str(mod_id).strip()
    if is_frozen_backup_uuid(mid):
        mapped = resolve_backup_mod_pk(internal_id=mid)
        mid = mapped if mapped.isdigit() else ""
    if not mid.isdigit():
        return False
    try:
        from core.db_manager import get_db

        row = get_db().get_mod_backup_row(mid)
    except Exception:  # noqa: BLE001
        return False
    if row is None:
        return False
    lkp = str(row.get("last_known_path") or "").strip()
    if lkp:
        return not Path(lkp).is_dir()
    return not bool(int(row.get("folder_present") or 0))
