"""Mod folder path lifecycle — resolve, commit, and track rename/move transitions.

External callers must pass Internal ID (``mod_id`` / ``internal_id``).
``workspace_id`` is display/registration only and never locates a folder.
Filesystem renames must produce a :class:`PathChangeResult` and
commit the new path to SQLite + sidecar before treating the operation as
successful.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from services.file_ops import INFO_DIR_NAME, read_info_metadata_dict

logger = logging.getLogger(__name__)

__all__ = [
    "PathChangeResult",
    "PathLifecycleStage",
    "ResolvedModPath",
    "apply_path_prefixes_to_sidecar",
    "commit_path_change",
    "detect_path_drift",
    "discover_folder_by_internal_id",
    "record_filesystem_rename",
    "resolve_managed_folder",
    "resolve_mod_folder_by_internal_id",
    "resolve_refresh_folder",
    "resolve_mod_id",
]


class PathLifecycleStage(str, Enum):
    RESOLVE = "resolve"
    RENAME = "rename"
    SIDECAR_WRITE = "sidecar_write"
    DB_WRITE = "db_write"
    BACKUP_SYNC = "backup_sync"
    RECONCILE = "reconcile"


@dataclass
class PathChangeResult:
    mod_id: str
    success: bool
    old_path: Path | None = None
    new_path: Path | None = None
    renamed: bool = False
    stage: PathLifecycleStage | str = ""
    error: str = ""
    workspace_id: str = ""


@dataclass
class ResolvedModPath:
    mod_id: str
    path: Path | None
    workspace_id: str = ""
    resolved_from: str = ""
    stale_hint: bool = False


def resolve_mod_id(
    mod_id: str | int | None = None,
    *,
    workspace_id: str | int | None = None,
    app_id: int = 0,
    db=None,
) -> str:
    """Return canonical Internal ID. ``workspace_id`` is ignored (display-only)."""
    _ = (workspace_id, app_id, db)
    mid = str(mod_id or "").strip()
    if mid.isdigit():
        return mid
    return mid


def _path_is_dir(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    try:
        candidate = Path(path).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
    except OSError:
        pass
    return None


def discover_folder_by_internal_id(
    internal_id: str,
    *,
    library_root: str | Path | None = None,
    expected_mod_id: str = "",
    db=None,
) -> Path | None:
    """
    Find a managed folder whose ``.info.internal_id`` matches *internal_id*.

    Identity-only discovery (never folder name / workspace_id / path invent).
    When *expected_mod_id* is set, the sidecar must resolve to that DB entity.
    Cold path — prefer :func:`resolve_managed_folder` / path cache first.
    """
    from core.db_manager import get_db
    from services.file_ops import ModFileManager
    from services.mod_identity import read_internal_id

    key = str(internal_id or "").strip()
    if not key:
        return None
    root: Path | None
    if library_root is not None:
        root = Path(library_root)
    else:
        try:
            from core.paths import default_mod_library

            root = Path(default_mod_library())
        except Exception:  # noqa: BLE001
            root = None
    if root is None or not root.is_dir():
        return None

    database = db if db is not None else get_db()
    expect = str(expected_mod_id or "").strip()
    hits: list[Path] = []
    try:
        for folder in ModFileManager(root).list_managed_mods():
            if not folder.is_dir():
                continue
            raw = dict(read_info_metadata_dict(folder) or {})
            iid = read_internal_id(raw)
            if iid != key:
                continue
            if expect:
                if not _folder_proves_mod_entity(folder, mod_id=expect, db=database):
                    continue
            hits.append(folder.resolve())
    except Exception:  # noqa: BLE001
        logger.debug(
            "discover_folder_by_internal_id failed for %s", key, exc_info=True
        )
        return None
    if len(hits) == 1:
        return hits[0]
    # Multiple live folders with the same internal_id → conflict; do not guess.
    return None


def resolve_mod_folder_by_internal_id(
    internal_id: str | int,
    *,
    library_root: str | Path | None = None,
    db=None,
) -> Path | None:
    """
    Runtime folder resolution: entity proof → disk scan of ``.info.internal_id``.

    Accepts UUID ``mods.internal_id`` **or** integer PK ``mods.mod_id``.
    Never uses workspace_id / folder name / published_file_id / raw path as identity.

    ``last_known_path`` may be used only as a proven cache hint (must match
    ``.info.internal_id``); on miss, scan the managed library root.
    """
    from core.db_manager import get_db

    token = str(internal_id or "").strip()
    if not token:
        return None
    database = db if db is not None else get_db()

    pk = ""
    proof = token
    if token.isdigit():
        pk = token
        row = database.get_mod_backup_row(pk) or {}
        proof = str(row.get("internal_id") or "").strip() or pk
    else:
        found = database.find_mod_by_internal_id(token)
        if found:
            pk = str(found)
            row = database.get_mod_backup_row(pk) or {}
            proof = str(row.get("internal_id") or "").strip() or token
        else:
            row = {}

    # Proven cache hint only — never trust path without .info proof.
    if pk:
        lkp = _path_is_dir((row or {}).get("last_known_path"))
        if lkp is not None and _folder_proves_mod_entity(lkp, mod_id=pk, db=database):
            _rebind_last_known_path(database, pk, lkp)
            return lkp.resolve()

    scan_root = library_root
    if scan_root is None:
        try:
            from core.paths import default_mod_library

            scan_root = Path(default_mod_library())
        except Exception:  # noqa: BLE001
            scan_root = None
    if scan_root is None:
        return None

    discovered = discover_folder_by_internal_id(
        proof,
        library_root=scan_root,
        expected_mod_id=pk,
        db=database,
    )
    if discovered is None and pk and proof != pk:
        discovered = discover_folder_by_internal_id(
            pk,
            library_root=scan_root,
            expected_mod_id=pk,
            db=database,
        )
    if discovered is not None and pk:
        _rebind_last_known_path(database, pk, discovered)
    return discovered


def _folder_proves_mod_entity(
    folder: Path,
    *,
    mod_id: str,
    db,
) -> bool:
    """True when folder/.info.internal_id binds to *mod_id* (never path/name).

    Lightweight: read ``.info.internal_id`` only — no full ensure_mod_identity.
    """
    from services.mod_identity import read_internal_id

    try:
        raw = dict(read_info_metadata_dict(folder) or {})
    except Exception:  # noqa: BLE001
        return False
    iid = read_internal_id(raw)
    if not iid:
        return False
    want = str(mod_id)
    if iid == want:
        return True
    try:
        found = db.find_mod_by_internal_id(iid)
    except Exception:  # noqa: BLE001
        return False
    return found is not None and str(found) == want


def _rebind_last_known_path(database, mod_id: str, folder: Path) -> None:
    try:
        database.update_mod_identity_fields(
            mod_id,
            last_known_path=str(folder.resolve()),
            folder_present=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "rebind last_known_path failed mod_id=%s path=%s",
            mod_id,
            folder,
            exc_info=True,
        )
    try:
        from services.managed_path_cache import remember_resolved

        remember_resolved(mod_id, folder)
    except Exception:  # noqa: BLE001
        pass


def resolve_managed_folder(
    mod_id: str | int,
    *,
    hint_path: str | Path | None = None,
    workspace_id: str | int | None = None,
    library_root: str | Path | None = None,
    db=None,
) -> ResolvedModPath:
    """
    Resolve the on-disk managed folder for *mod_id*.

    Accept a candidate only when ``.info.internal_id`` proves the entity.
    Never match via workspace_id / folder name / path invent / published_file_id.
    """
    from core.db_manager import get_db

    mid = resolve_mod_id(mod_id, workspace_id=workspace_id, db=db)
    database = db if db is not None else get_db()
    wid = str(workspace_id or "").strip()
    stale_hint = False
    row: dict[str, Any] = {}

    if not mid.isdigit():
        return ResolvedModPath(
            mod_id=mid,
            path=None,
            workspace_id=wid,
            resolved_from="unresolved",
        )

    from services.managed_path_cache import (
        get_cached_managed_path,
        remember_resolved,
    )

    row = database.get_mod_backup_row(mid) or {}
    if not wid:
        wid = str(row.get("workspace_id") or "").strip()
    proof_key = str(row.get("internal_id") or "").strip() or mid

    live = _path_is_dir(hint_path)
    if live is not None and _folder_proves_mod_entity(live, mod_id=mid, db=database):
        remember_resolved(mid, live, library_root=library_root)
        return ResolvedModPath(
            mod_id=mid,
            path=live,
            workspace_id=wid,
            resolved_from="hint",
        )
    if hint_path is not None:
        try:
            if Path(hint_path).expanduser().exists() is False:
                stale_hint = True
        except OSError:
            stale_hint = True

    lkp = _path_is_dir(row.get("last_known_path"))
    if lkp is not None and _folder_proves_mod_entity(lkp, mod_id=mid, db=database):
        remember_resolved(mid, lkp, library_root=library_root)
        return ResolvedModPath(
            mod_id=mid,
            path=lkp,
            workspace_id=wid,
            resolved_from="last_known_path",
            stale_hint=stale_hint,
        )
    if lkp is not None:
        stale_hint = True

    cached = get_cached_managed_path(mid, library_root=library_root)
    if cached is not None:
        if _folder_proves_mod_entity(cached, mod_id=mid, db=database):
            return ResolvedModPath(
                mod_id=mid,
                path=cached,
                workspace_id=wid,
                resolved_from="path_cache",
                stale_hint=stale_hint,
            )
        from services.managed_path_cache import invalidate_managed_path_cache

        invalidate_managed_path_cache(mid, library_root=library_root)

    # Dead / unproven last_known_path: discover by .info.internal_id only.
    scan_root = library_root
    if scan_root is None:
        dead = str(row.get("last_known_path") or "").strip()
        if dead:
            try:
                dead_path = Path(dead).expanduser()
                if dead_path.parent.parent.is_dir():
                    scan_root = dead_path.parent.parent
            except OSError:
                scan_root = None
        if scan_root is None:
            try:
                from core.paths import default_mod_library

                scan_root = Path(default_mod_library())
            except Exception:  # noqa: BLE001
                scan_root = None

    if scan_root is not None:
        discovered = discover_folder_by_internal_id(
            proof_key,
            library_root=scan_root,
            expected_mod_id=mid,
            db=database,
        )
        if discovered is None and proof_key != mid:
            discovered = discover_folder_by_internal_id(
                mid,
                library_root=scan_root,
                expected_mod_id=mid,
                db=database,
            )
        if discovered is not None:
            _rebind_last_known_path(database, mid, discovered)
            remember_resolved(mid, discovered, library_root=scan_root)
            return ResolvedModPath(
                mod_id=mid,
                path=discovered,
                workspace_id=wid,
                resolved_from="info_internal_id",
                stale_hint=True,
            )

    return ResolvedModPath(
        mod_id=mid,
        path=None,
        workspace_id=wid,
        resolved_from="unresolved",
        stale_hint=stale_hint,
    )


def resolve_refresh_folder(
    mod_id: str | int,
    managed_path: str | Path,
    *,
    db=None,
) -> Path:
    """Resolve live folder for refresh; heal stale UI hints via DB."""
    mid = str(mod_id or "").strip()
    resolved = resolve_managed_folder(mid, hint_path=managed_path, db=db)
    folder = resolved.path
    if folder is not None and folder.is_dir():
        return folder
    healed = resolve_managed_folder(mid, db=db)
    if healed.path is not None and healed.path.is_dir():
        logger.info(
            "path healed for refresh mod_id=%s from=%s path=%s",
            mid,
            healed.resolved_from,
            healed.path,
        )
        return healed.path
    return Path(managed_path).expanduser()


def apply_path_prefixes_to_sidecar(
    data: dict[str, Any],
    *,
    old_prefix: str,
    new_prefix: str,
) -> dict[str, Any]:
    """Rewrite path-prefixed sidecar keys after folder rename."""
    if not old_prefix or not new_prefix or old_prefix == new_prefix:
        return data
    out = dict(data)
    out["managed_path"] = new_prefix
    out["local_path"] = new_prefix
    for key in ("offline_page_path", "offline_page", "source_path"):
        raw = str(out.get(key) or "")
        if raw.startswith(old_prefix):
            out[key] = new_prefix + raw[len(old_prefix) :]
    return out


def commit_path_change(
    mod_id: str | int,
    *,
    old_path: str | Path | None,
    new_path: str | Path,
    renamed: bool = False,
    reason: str = "refresh",
    sync_backup: bool = True,
    db=None,
) -> PathChangeResult:
    """
    Persist folder path to SQLite + sidecar (+ optional backup sync).

    Call immediately after a filesystem rename so DB/sidecar never lag behind
    disk. Returns ``success=False`` with ``stage`` when a required write fails.
    """
    from core.db_manager import get_db

    mid = str(mod_id or "").strip()
    old_p: Path | None = None
    if old_path is not None:
        try:
            old_p = Path(old_path).expanduser()
        except OSError:
            old_p = Path(str(old_path))

    try:
        new_p = Path(new_path).expanduser().resolve()
    except OSError as exc:
        return PathChangeResult(
            mod_id=mid,
            success=False,
            old_path=old_p,
            new_path=None,
            renamed=renamed,
            stage=PathLifecycleStage.RESOLVE,
            error=str(exc),
        )

    if not new_p.is_dir():
        return PathChangeResult(
            mod_id=mid,
            success=False,
            old_path=old_p,
            new_path=new_p,
            renamed=renamed,
            stage=PathLifecycleStage.RESOLVE,
            error=f"目标目录不存在: {new_p}",
        )

    # Path Lifecycle gate: application / library / data roots are never Mod roots.
    try:
        from services.mod_path_validation import (
            InvalidModRootError,
            validate_managed_mod_path,
        )

        validate_managed_mod_path(new_p)
    except InvalidModRootError as exc:
        return PathChangeResult(
            mod_id=mid,
            success=False,
            old_path=old_p,
            new_path=new_p,
            renamed=renamed,
            stage=PathLifecycleStage.RESOLVE,
            error=str(exc),
        )

    database = db if db is not None else get_db()
    row = database.get_mod_backup_row(mid) if mid.isdigit() else {}
    workspace_id = str((row or {}).get("workspace_id") or "").strip()
    resolved = str(new_p)

    # --- sidecar ---
    info = new_p / INFO_DIR_NAME / "metadata.json"
    try:
        data = read_info_metadata_dict(new_p) or {}
        old_prefix = str(old_p.resolve()) if old_p is not None else ""
        if not old_prefix:
            old_prefix = str(data.get("managed_path") or data.get("local_path") or "")
        merged = apply_path_prefixes_to_sidecar(
            data,
            old_prefix=old_prefix,
            new_prefix=resolved,
        )
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        return PathChangeResult(
            mod_id=mid,
            success=False,
            old_path=old_p,
            new_path=new_p,
            renamed=renamed,
            stage=PathLifecycleStage.SIDECAR_WRITE,
            error=f"sidecar 路径更新失败: {exc}",
            workspace_id=workspace_id,
        )

    # --- SQLite ---
    if mid.isdigit():
        try:
            from services.content_status_eval import persist_evaluated_content_status

            database.update_mod_identity_fields(
                mid,
                last_known_path=resolved,
                folder_present=True,
            )
            persist_evaluated_content_status(
                mid,
                new_p,
                db=database,
                folder_present=True,
                sync_sticky_marker=True,
            )
        except Exception as exc:  # noqa: BLE001
            return PathChangeResult(
                mod_id=mid,
                success=False,
                old_path=old_p,
                new_path=new_p,
                renamed=renamed,
                stage=PathLifecycleStage.DB_WRITE,
                error=f"数据库路径更新失败: {exc}",
                workspace_id=workspace_id,
            )

    if sync_backup and mid.isdigit():
        try:
            from services.metadata_backup_sync import sync_after_metadata_change

            sync_after_metadata_change(mid, new_p, reason)
        except Exception as exc:  # noqa: BLE001
            # Backup sync must not fail metadata refresh — same as pre-lifecycle
            # modio_metadata_refresh end-of-flow (swallowed there).
            logger.warning(
                "path commit backup sync failed mod_id=%s path=%s: %s",
                mid,
                new_p,
                exc,
            )

    logger.info(
        "path committed mod_id=%s renamed=%s old=%s new=%s reason=%s",
        mid,
        renamed,
        old_p,
        new_p,
        reason,
    )
    return PathChangeResult(
        mod_id=mid,
        success=True,
        old_path=old_p,
        new_path=new_p,
        renamed=renamed,
        stage=PathLifecycleStage.DB_WRITE if renamed else PathLifecycleStage.RECONCILE,
        workspace_id=workspace_id,
    )


def record_filesystem_rename(
    mod_id: str | int,
    old_path: str | Path,
    new_path: str | Path,
    *,
    reason: str = "refresh",
    db=None,
) -> PathChangeResult:
    """Record a rename/move that already happened on disk."""
    return commit_path_change(
        mod_id,
        old_path=old_path,
        new_path=new_path,
        renamed=True,
        reason=reason,
        db=db,
    )


def detect_path_drift(
    mod_id: str | int,
    disk_path: str | Path,
    *,
    db=None,
) -> PathChangeResult | None:
    """
    Return a :class:`PathChangeResult` when *disk_path* differs from DB
    ``last_known_path``; ``None`` when already aligned.
    """
    from core.db_manager import get_db

    mid = str(mod_id or "").strip()
    if not mid.isdigit():
        return None
    try:
        folder = Path(disk_path).expanduser().resolve()
    except OSError:
        return None
    database = db if db is not None else get_db()
    row = database.get_mod_backup_row(mid) or {}
    prev = str(row.get("last_known_path") or "").strip()
    if not prev:
        return None
    try:
        if Path(prev).resolve() == folder:
            return None
    except OSError:
        pass
    if Path(prev).is_dir() and Path(prev) != folder:
        # Two folders — identity conflict; do not auto-commit.
        return None
    return commit_path_change(
        mid,
        old_path=prev,
        new_path=folder,
        renamed=True,
        reason="reconcile",
        db=database,
    )
