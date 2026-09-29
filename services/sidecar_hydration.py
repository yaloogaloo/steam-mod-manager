"""Workshop payload vs SMM managed sidecar ownership.

Workshop ``.info`` is a *candidate* sidecar, not user-managed state.
Backup is the protection copy of user Cover / Offline.

After Steam Sync / Import copytree:

- Backup present → restore Backup onto Live (workshop sidecar does not win)
- Backup absent and source brought a cover/offline → keep it
- Both absent → remain missing (do not invent assets)

Never deletes Backup because Live or Workshop is missing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.file_ops import INFO_DIR_NAME
from services.metadata_backup import (
    BACKUP_COVER_BASENAME,
    BACKUP_OFFLINE_DIR,
    BACKUP_OFFLINE_INDEX,
    prove_backup_storage_key,
    readable_backup_root,
    restore_cover_from_backup,
    restore_offline_from_backup,
)
from services.backup_identity import is_frozen_backup_uuid
from services.offline.paths import resolve_offline_page

logger = logging.getLogger(__name__)

ACTION_BACKUP_RESTORE = "backup_restore"
ACTION_SOURCE_COPY = "source_copy"
ACTION_PRESERVE = "preserve"
ACTION_NONE = "none"


@dataclass(frozen=True)
class SidecarHydrationResult:
    ok: bool
    cover_action: str
    offline_action: str
    backup_cover: str
    live_cover: str
    backup_offline: str
    live_offline: str
    owner: str = ""
    restored: bool = False
    backup_committed: bool = False


def _present(path: Path | None) -> str:
    try:
        if path is not None and path.is_file():
            return "present"
    except OSError:
        return "absent"
    return "absent"


def _find_info_cover(root: Path) -> Path | None:
    info = root / INFO_DIR_NAME
    if not info.is_dir():
        return None
    for pattern in ("cover.*", "preview.*"):
        for candidate in sorted(info.glob(pattern)):
            if candidate.is_file():
                return candidate
    return None


def _find_backup_cover(backup_dir: Path | None) -> Path | None:
    if backup_dir is None or not backup_dir.is_dir():
        return None
    for candidate in sorted(backup_dir.glob(f"{BACKUP_COVER_BASENAME}.*")):
        if candidate.is_file():
            return candidate
    return None


def _backup_offline_index(backup_dir: Path | None) -> Path | None:
    if backup_dir is None:
        return None
    index = backup_dir / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    try:
        if index.is_file():
            return index
    except OSError:
        return None
    return None


def _same_bytes(left: Path, right: Path) -> bool:
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        return left.read_bytes() == right.read_bytes()
    except OSError:
        return False


def _clear_live_covers(root: Path) -> None:
    info = root / INFO_DIR_NAME
    if not info.is_dir():
        return
    for pattern in ("cover.*", "preview.*"):
        for candidate in info.glob(pattern):
            try:
                if candidate.is_file():
                    candidate.unlink()
            except OSError:
                pass


def _clear_untrusted_live_offline(root: Path) -> None:
    info = root / INFO_DIR_NAME
    offline = info / "offline"
    try:
        if offline.is_dir():
            import shutil

            shutil.rmtree(offline)
    except OSError:
        pass
    for name in ("index.html", "manifest.json"):
        stale = info / name
        try:
            if stale.is_file():
                stale.unlink()
        except OSError:
            pass


def resolve_sidecar_owner(
    managed: Path,
    *,
    owner_mod_id: str | int | None = None,
    workspace_id: str = "",
    app_id: int = 0,
) -> str:
    """Frozen UUID or digit PK, else empty. Never uses folder name as identity."""
    hint = str(owner_mod_id or "").strip()
    data: dict[str, Any] | None = None
    try:
        from services.file_ops import read_info_metadata_dict

        data = read_info_metadata_dict(managed) or {}
    except Exception:  # noqa: BLE001
        data = None
    frozen = prove_backup_storage_key(hint, managed_path=managed, info=data)
    if is_frozen_backup_uuid(frozen) or frozen.isdigit():
        return frozen
    wid = str(workspace_id or "").strip() or str((data or {}).get("workspace_id") or "").strip()
    if not wid:
        wid = str((data or {}).get("published_file_id") or "").strip()
    aid = int(app_id or 0) or int((data or {}).get("app_id") or 0)
    if wid and aid > 0:
        try:
            from core.db_manager import get_db
            from core.mod_platform import PLATFORM_STEAM

            hit = get_db().find_mod_for_registration(PLATFORM_STEAM, aid, wid)
            if hit is not None:
                iid = str(getattr(hit, "internal_id", "") or "").strip()
                if is_frozen_backup_uuid(iid):
                    return iid
                mid = str(getattr(hit, "mod_id", "") or "").strip()
                if mid.isdigit():
                    return mid
        except Exception:  # noqa: BLE001
            pass
    return hint if hint.isdigit() else ""


def _log_decision(
    *,
    asset: str,
    source: str,
    backup: str,
    live: str,
    action: str,
    owner: str,
) -> None:
    logger.info(
        "[SIDECAR] asset=%s source=%s backup=%s live=%s action=%s owner=%s",
        asset,
        source,
        backup,
        live,
        action,
        owner or "-",
    )


def hydrate_managed_sidecar(
    managed: str | Path,
    *,
    owner_mod_id: str | int | None = None,
    workspace_id: str = "",
    app_id: int = 0,
    source: str = "workshop",
    backup_wins: bool = True,
    commit_backup: bool = True,
) -> SidecarHydrationResult:
    """
    Reconcile Live Cover/Offline with Backup after a Workshop/Import copy.

    ``backup_wins=True`` (physical copytree): Backup user assets replace
    untrusted Workshop sidecar files.

    ``backup_wins=False`` (skip existing / Junction): never replace an
    existing Live asset; restore only when Live is missing.
    """
    root = Path(managed)
    if not root.is_dir():
        return SidecarHydrationResult(
            ok=False,
            cover_action=ACTION_NONE,
            offline_action=ACTION_NONE,
            backup_cover="absent",
            live_cover="absent",
            backup_offline="absent",
            live_offline="absent",
        )

    owner = resolve_sidecar_owner(
        root,
        owner_mod_id=owner_mod_id,
        workspace_id=workspace_id,
        app_id=app_id,
    )
    data: dict[str, Any] | None = None
    try:
        from services.file_ops import read_info_metadata_dict

        data = read_info_metadata_dict(root) or {}
    except Exception:  # noqa: BLE001
        data = None
    frozen = prove_backup_storage_key(owner, managed_path=root, info=data)
    bak_root = readable_backup_root(
        frozen if is_frozen_backup_uuid(frozen) else None,
        mod_pk=owner if str(owner).isdigit() else None,
    )

    bak_cover = _find_backup_cover(bak_root)
    live_cover = _find_info_cover(root)
    bak_off = _backup_offline_index(bak_root)
    live_off = resolve_offline_page(root)

    cover_action = ACTION_NONE
    offline_action = ACTION_NONE
    restored = False
    ok = True

    if bak_cover is not None:
        if backup_wins:
            if live_cover is None or not _same_bytes(live_cover, bak_cover):
                _clear_live_covers(root)
                restored_path = restore_cover_from_backup(root, owner_mod_id=owner)
                live_cover = _find_info_cover(root)
                if live_cover is None:
                    logger.warning(
                        "[SIDECAR] backup cover restore failed owner=%s path=%s",
                        owner or "-",
                        root,
                    )
                    ok = False
                else:
                    cover_action = ACTION_BACKUP_RESTORE
                    restored = True
            else:
                cover_action = ACTION_PRESERVE
        elif live_cover is None:
            restored_path = restore_cover_from_backup(root, owner_mod_id=owner)
            live_cover = _find_info_cover(root)
            if live_cover is None and restored_path:
                ok = False
            elif live_cover is not None:
                cover_action = ACTION_BACKUP_RESTORE
                restored = True
            else:
                cover_action = ACTION_NONE
        else:
            cover_action = ACTION_PRESERVE
    elif live_cover is not None:
        cover_action = ACTION_SOURCE_COPY if backup_wins else ACTION_PRESERVE
    _log_decision(
        asset="cover",
        source=source,
        backup=_present(bak_cover),
        live=_present(live_cover),
        action=cover_action,
        owner=owner,
    )

    if bak_off is not None:
        if backup_wins:
            if live_off is None or not _same_bytes(live_off, bak_off):
                _clear_untrusted_live_offline(root)
                restored_path = restore_offline_from_backup(root, owner_mod_id=owner)
                live_off = resolve_offline_page(root)
                if live_off is None:
                    logger.warning(
                        "[SIDECAR] backup offline restore failed owner=%s path=%s",
                        owner or "-",
                        root,
                    )
                    ok = False
                else:
                    offline_action = ACTION_BACKUP_RESTORE
                    restored = True
            else:
                offline_action = ACTION_PRESERVE
        elif live_off is None:
            restored_path = restore_offline_from_backup(root, owner_mod_id=owner)
            live_off = resolve_offline_page(root)
            if live_off is None and restored_path:
                ok = False
            elif live_off is not None:
                offline_action = ACTION_BACKUP_RESTORE
                restored = True
            else:
                offline_action = ACTION_NONE
        else:
            offline_action = ACTION_PRESERVE
    elif live_off is not None:
        offline_action = ACTION_SOURCE_COPY if backup_wins else ACTION_PRESERVE
    _log_decision(
        asset="offline",
        source=source,
        backup=_present(bak_off),
        live=_present(live_off),
        action=offline_action,
        owner=owner,
    )

    backup_committed = False
    if commit_backup and ok and owner:
        from services.metadata_backup_sync import sync_after_metadata_change

        # Physical copy: identity-resolved Backup must finish before Sync success.
        # Skip-existing restore: commit only when we actually wrote Live.
        need_commit = backup_wins or restored
        if need_commit:
            backup_committed = bool(
                sync_after_metadata_change(
                    owner, root, "sync", wait=True
                )
            )
            if not backup_committed:
                logger.warning(
                    "[SIDECAR] backup commit failed owner=%s path=%s",
                    owner,
                    root,
                )
                ok = False

    return SidecarHydrationResult(
        ok=ok,
        cover_action=cover_action,
        offline_action=offline_action,
        backup_cover=_present(bak_cover),
        live_cover=_present(_find_info_cover(root)),
        backup_offline=_present(_backup_offline_index(bak_root)),
        live_offline=_present(resolve_offline_page(root)),
        owner=owner,
        restored=restored,
        backup_committed=backup_committed,
    )


def protect_live_sidecar_before_overwrite(
    managed: str | Path,
    *,
    owner_mod_id: str | int | None = None,
    workspace_id: str = "",
    app_id: int = 0,
) -> bool:
    """Snapshot Live Cover/Offline into Backup before ``rmtree`` overwrite.

    Unresolved identity → True (nothing to protect in Backup).
    Identity resolved and Backup snapshot fails → False (refuse rmtree).
    """
    root = Path(managed)
    if not root.is_dir():
        return True
    owner = resolve_sidecar_owner(
        root,
        owner_mod_id=owner_mod_id,
        workspace_id=workspace_id,
        app_id=app_id,
    )
    if not owner:
        logger.info(
            "[SIDECAR] asset=protect source=managed backup=absent live=present "
            "action=none owner=-"
        )
        return True
    from services.metadata_backup_sync import sync_after_metadata_change

    ok = bool(sync_after_metadata_change(owner, root, "sync", wait=True))
    logger.info(
        "[SIDECAR] asset=protect source=managed backup=%s live=present action=%s owner=%s",
        "present" if ok else "absent",
        ACTION_PRESERVE if ok else ACTION_NONE,
        owner,
    )
    return ok
