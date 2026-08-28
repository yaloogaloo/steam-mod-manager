"""Mod folder path lifecycle — resolve, commit, and track rename/move transitions.

External callers should prefer ``mod_id`` / ``workspace_id`` over frozen ``Path``
snapshots. Filesystem renames must produce a :class:`PathChangeResult` and
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
from services.library_status import (
    CONTENT_HEALTHY,
    content_status_to_library_status,
)

logger = logging.getLogger(__name__)

__all__ = [
    "PathChangeResult",
    "PathLifecycleStage",
    "ResolvedModPath",
    "apply_path_prefixes_to_sidecar",
    "commit_path_change",
    "detect_path_drift",
    "record_filesystem_rename",
    "resolve_managed_folder",
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
    db=None,
) -> str:
    """Return canonical ``mod_id``; map *workspace_id* when *mod_id* is absent."""
    mid = str(mod_id or "").strip()
    if mid.isdigit():
        return mid
    wid = str(workspace_id or "").strip()
    if not wid:
        return mid
    from core.db_manager import get_db

    database = db if db is not None else get_db()
    try:
        with database._lock:
            row = database._conn.execute(
                "SELECT mod_id FROM mods WHERE workspace_id = ? LIMIT 1",
                (wid,),
            ).fetchone()
        if row is not None:
            return str(row["mod_id"] or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("workspace_id lookup failed for %s: %s", wid, exc)
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


def resolve_managed_folder(
    mod_id: str | int,
    *,
    hint_path: str | Path | None = None,
    workspace_id: str | int | None = None,
    db=None,
) -> ResolvedModPath:
    """
    Resolve the on-disk managed folder for *mod_id*.

    Priority: valid *hint_path* → SQLite ``last_known_path`` → sidecar on hint.
    """
    from core.db_manager import get_db

    mid = resolve_mod_id(mod_id, workspace_id=workspace_id, db=db)
    database = db if db is not None else get_db()
    wid = str(workspace_id or "").strip()
    stale_hint = False

    live = _path_is_dir(hint_path)
    if live is not None:
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

    if mid.isdigit():
        row = database.get_mod_backup_row(mid) or {}
        if not wid:
            wid = str(row.get("workspace_id") or "").strip()
        lkp = _path_is_dir(row.get("last_known_path"))
        if lkp is not None:
            return ResolvedModPath(
                mod_id=mid,
                path=lkp,
                workspace_id=wid,
                resolved_from="last_known_path",
                stale_hint=stale_hint,
            )

    # Sidecar on stale hint may still exist after rename (same internal_id).
    if hint_path is not None and mid.isdigit():
        try:
            sidecar = read_info_metadata_dict(Path(hint_path)) or {}
            for key in ("managed_path", "local_path"):
                alt = _path_is_dir(sidecar.get(key))
                if alt is not None:
                    return ResolvedModPath(
                        mod_id=mid,
                        path=alt,
                        workspace_id=wid or str(sidecar.get("workspace_id") or ""),
                        resolved_from=f"sidecar_{key}",
                        stale_hint=True,
                    )
        except OSError:
            pass

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
            database.update_mod_identity_fields(
                mid,
                last_known_path=resolved,
                folder_present=True,
                content_status=CONTENT_HEALTHY,
                library_status=content_status_to_library_status(CONTENT_HEALTHY),
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
