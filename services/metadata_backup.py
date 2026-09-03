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
from services.file_ops import (
    INFO_DIR_NAME,
    METADATA_FILENAME,
    ModFileManager,
    read_info_metadata_dict,
)
from services.offline.paths import OFFLINE_SNAPSHOT_DIR

logger = logging.getLogger(__name__)

BACKUP_DIR_NAME = "mod_backup"
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


def backup_root(mod_id: int | str) -> Path:
    """``data/mod_backup/<mod_id>/``"""
    return data_dir() / BACKUP_DIR_NAME / str(mod_id).strip()


def _resolve_mod_id(mod_path: Path, data: dict[str, Any] | None) -> str:
    payload = dict(data or {})
    try:
        payload.setdefault(
            "_managed_path",
            str(mod_path.resolve()) if mod_path.exists() else str(mod_path),
        )
        payload.setdefault("_folder_name", mod_path.name)
        from services.mod_identity import resolve_existing_mod_id

        found = resolve_existing_mod_id(payload)
        if found.isdigit():
            return found
    except Exception:  # noqa: BLE001
        logger.debug("backup identity resolve failed for %s", mod_path, exc_info=True)
    mid = str(payload.get("published_file_id") or "").strip()
    if not mid.isdigit() and mod_path.name.isdigit():
        mid = mod_path.name
    return mid


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


def _copy_offline_tree(src_offline: Path, dest_offline: Path) -> str:
    """
    Mirror ``.info/offline/`` into backup (file copy only — no HTML processing).

    When source index is missing, remove backup offline tree.
    Idempotent: skips copytree when index sha256 matches.
    """
    index = src_offline / BACKUP_OFFLINE_INDEX
    if not index.is_file():
        _clear_backup_offline(dest_offline)
        return ""
    backup_index = dest_offline / BACKUP_OFFLINE_INDEX
    try:
        if backup_index.is_file() and _same_file_content(index, backup_index):
            return str(backup_index.resolve())
        t_copy = time.perf_counter()
        if dest_offline.is_dir():
            shutil.rmtree(dest_offline)
        shutil.copytree(src_offline, dest_offline)
        try:
            from services.reconcile_observability import add_copy

            nbytes = 0
            try:
                if backup_index.is_file():
                    nbytes = int((dest_offline / BACKUP_OFFLINE_INDEX).stat().st_size)
            except OSError:
                nbytes = 0
            add_copy(
                files=1,
                nbytes=nbytes,
                ms=(time.perf_counter() - t_copy) * 1000.0,
            )
        except Exception:  # noqa: BLE001
            pass
        if backup_index.is_file():
            return str(backup_index.resolve())
    except OSError as exc:
        logger.warning("Failed to copy offline backup %s: %s", dest_offline, exc)
        return ""
    return ""


def snapshot_from_mod_folder(mod_path: str | Path) -> BackupSnapshot | None:
    """
    Read ``.info/metadata.json`` and mirror cover / offline into ``data/mod_backup/``.

    Never writes back to the Mod folder. Missing ``.info`` assets delete matching
    backup assets (folder-absent is handled by callers — not this function).
    """
    root = Path(mod_path)
    if not root.is_dir():
        return None

    t_scan = time.perf_counter()
    data = read_info_metadata_dict(root) or {}
    mid = _resolve_mod_id(root, data)
    try:
        from services.reconcile_observability import add_scan_ms

        add_scan_ms((time.perf_counter() - t_scan) * 1000.0)
    except Exception:  # noqa: BLE001
        pass
    if not mid.isdigit():
        return None

    dest = backup_root(mid)
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
        logger.warning("Failed to write backup metadata for %s: %s", mid, exc)
        return None

    info_dir = root / INFO_DIR_NAME
    t_assets = time.perf_counter()
    cover_src = _find_info_cover(info_dir)
    offline_src = info_dir / OFFLINE_SNAPSHOT_DIR
    try:
        from services.reconcile_observability import add_scan_ms

        add_scan_ms((time.perf_counter() - t_assets) * 1000.0)
    except Exception:  # noqa: BLE001
        pass
    cover_abs = _copy_cover(cover_src, dest)
    offline_abs = _copy_offline_tree(offline_src, dest / BACKUP_OFFLINE_DIR)

    return BackupSnapshot(
        mod_id=mid,
        metadata=data,
        cover_path=cover_abs,
        offline_path=offline_abs,
        last_known_path=str(root.resolve()),
    )


def load_backup(mod_id: int | str) -> BackupSnapshot | None:
    """Load backup metadata + asset paths for a Mod id."""
    mid = str(mod_id).strip()
    if not mid.isdigit():
        return None
    dest = backup_root(mid)
    meta_file = dest / BACKUP_METADATA_NAME
    if not meta_file.is_file():
        return None
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read backup metadata for %s: %s", mid, exc)
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

    last_known = ""
    try:
        from core.db_manager import get_db

        row = get_db().get_mod_backup_row(mid)
        if row is not None:
            last_known = str(row.get("last_known_path") or "").strip()
    except Exception:  # noqa: BLE001
        pass

    return BackupSnapshot(
        mod_id=mid,
        metadata=data,
        cover_path=cover_abs,
        offline_path=offline_abs,
        last_known_path=last_known,
    )


def mark_missing(mod_id: int | str) -> None:
    """Mark a Mod as folder-absent in SQLite (backup remains)."""
    mid = str(mod_id).strip()
    if not mid.isdigit():
        return
    try:
        from core.db_manager import get_db

        get_db().set_mod_folder_present(mid, present=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mark_missing failed for %s: %s", mid, exc)


def restore_check(
    mod_id: int | str,
    *,
    library_root: str | Path | None = None,
) -> bool:
    """
    If the Mod folder reappeared, run ``sync_metadata_backup`` and clear missing flag.

    Returns True when the folder was found and synced.
    """
    mid = str(mod_id).strip()
    if not mid.isdigit():
        return False
    try:
        from core.db_manager import get_db

        db = get_db()
        row = db.get_mod_backup_row(mid)
        if row is None:
            return False
        if bool(int(row.get("folder_present") or 0)):
            return False

        candidates: list[Path] = []
        lkp = str(row.get("last_known_path") or "").strip()
        if lkp:
            candidates.append(Path(lkp))
        if library_root is not None:
            from services.importers.materialize import find_managed_mod_path

            found = find_managed_mod_path(library_root, mid)
            if found is not None:
                candidates.append(found)

        for path in candidates:
            if path.is_dir():
                from services.metadata_backup_sync import sync_after_metadata_change

                sync_after_metadata_change(mid, path, "restore")
                return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("restore_check failed for %s: %s", mid, exc)
    return False


def sync_metadata_backup(mod_path: str | Path) -> None:
    """
    Low-level ``.info`` → ``data/mod_backup`` snapshot.

    Prefer :func:`services.metadata_backup_sync.sync_after_metadata_change`
    for write-event callers (adds reason logging + validation status).

    When the Mod folder exists: snapshot ``.info`` → backup, mark folder present.
    When absent: mark missing only (does not create backup).
    """
    root = Path(mod_path)
    if not root.is_dir():
        mid = ""
        if root.name.isdigit():
            mid = root.name
        if not mid.isdigit():
            try:
                from core.db_manager import get_db

                row = get_db().get_mod_backup_row_by_path(str(root))
                if row:
                    mid = str(row.get("mod_id") or "")
            except Exception:  # noqa: BLE001
                mid = ""
        if mid.isdigit():
            mark_missing(mid)
        return

    snapshot = snapshot_from_mod_folder(root)
    if snapshot is None:
        return

    meta_json = json.dumps(snapshot.metadata, ensure_ascii=False)
    try:
        from core.db_manager import get_db

        t_persist = time.perf_counter()
        get_db().update_mod_backup_snapshot(
            snapshot.mod_id,
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

    ``mods.folder_present`` is a cache. Real check is ``Path(last_known_path).exists()``.
    """
    logger.debug("reconcile_folder_presence enter")
    root = Path(library_root) if library_root is not None else None
    try:
        from core.db_manager import get_db

        db = get_db()
        for row in db.iter_mod_backup_rows():
            mid = str(row.get("mod_id") or "").strip()
            if not mid.isdigit():
                continue
            lkp = str(row.get("last_known_path") or "").strip()
            path = Path(lkp) if lkp else None
            if path is not None and path.is_dir():
                if not bool(int(row.get("folder_present") or 0)):
                    restore_check(mid, library_root=root)
                else:
                    db.set_mod_folder_present(mid, present=True)
                continue
            if root is not None and restore_check(mid, library_root=root):
                continue
            if lkp or str(row.get("backup_metadata_json") or "").strip():
                mark_missing(mid)
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


def metadata_dict_for_ui(snapshot: BackupSnapshot) -> dict[str, Any]:
    """Merge backup JSON with backup asset paths for library / detail display."""
    data = dict(snapshot.metadata)
    if snapshot.cover_path:
        data["cover_path"] = snapshot.cover_path
    if snapshot.offline_path:
        data["offline_page_path"] = snapshot.offline_path
    return data


def resolve_backup_cover(mod_id: int | str) -> Path | None:
    snap = load_backup(mod_id)
    if snap is None or not snap.cover_path:
        return None
    path = Path(snap.cover_path)
    return path if path.is_file() else None


def resolve_backup_offline(mod_id: int | str) -> Path | None:
    snap = load_backup(mod_id)
    if snap is None or not snap.offline_path:
        return None
    path = Path(snap.offline_path)
    return path if path.is_file() else None


def is_mod_folder_absent(mod_id: int | str, managed_path: str | Path | None = None) -> bool:
    """True when the managed Mod directory does not exist (disk is source of truth)."""
    if managed_path is not None:
        return not Path(managed_path).is_dir()
    mid = str(mod_id).strip()
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


def managed_path_from_backup_row(row: Mapping[str, Any]) -> Path:
    """Virtual library path for a folder-missing Mod (uses ``last_known_path``)."""
    lkp = str(row.get("last_known_path") or "").strip()
    if lkp:
        return Path(lkp)
    return Path(f"__missing__/{row.get('mod_id', '')}")


def mod_metadata_from_backup_row(row: Mapping[str, Any]) -> ModMetadata:
    """Build ``ModMetadata`` for library / detail when only backup exists."""
    from core.models import ModMetadata

    mid = str(row.get("mod_id") or "").strip()
    raw_json = str(row.get("backup_metadata_json") or "").strip()
    data: dict[str, Any] = {}
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict):
                data = parsed
        except json.JSONDecodeError:
            data = {}

    path = managed_path_from_backup_row(row)
    title = str(
        data.get("display_name")
        or data.get("title")
        or f"Unknown_Mod_{mid}"
    ).strip()
    cover = str(row.get("backup_cover_path") or data.get("cover_path") or "").strip()
    offline = str(
        row.get("backup_offline_path") or data.get("offline_page_path") or ""
    ).strip()
    game_name = str(data.get("game_name") or "").strip()
    if not game_name and path.parent.name not in ("", "__missing__"):
        game_name = path.parent.name

    return ModMetadata(
        published_file_id=mid,
        title=title,
        description=str(data.get("description") or "").strip(),
        preview_url=str(data.get("preview_url") or "").strip(),
        app_id=int(data.get("app_id") or row.get("app_id") or 0),
        game_name=game_name,
        managed_path=str(path),
        local_path=str(path),
        url=str(
            data.get("url") or data.get("source_url") or data.get("website") or ""
        ).strip(),
        cover_path=cover or None,
        offline_page_path=offline or None,
        source_type=str(
            data.get("source_type") or data.get("platform") or ""
        ).strip(),
        json_display_name=str(data.get("display_name") or "").strip(),
    )
