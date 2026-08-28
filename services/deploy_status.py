"""Deployment status (Phase 8) — separate from Library ``content_status``.

Runtime statuses:

- ``not_deployed`` / ``deployed`` / ``outdated`` / ``conflict``

DB may still store legacy ``failed``. Manifest remains under Library ``.info``
(existing project convention — do not invent a second primary store).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
    get_db,
)
from services.deploy_rules.manifest import DeployManifest, load_manifest
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, ModFileManager
from services.importers.local_scanner import is_skipped_mod_path_part
from services.library_status import (
    CONTENT_BACKUP_INVALID,
    CONTENT_FOLDER_MISSING,
    CONTENT_IDENTITY_CONFLICT,
    row_content_status,
)

logger = logging.getLogger(__name__)

# Phase 8 deployment_status (UI / lifecycle — not content_status)
DEPLOYMENT_NOT_DEPLOYED = "not_deployed"
DEPLOYMENT_DEPLOYED = "deployed"
DEPLOYMENT_OUTDATED = "outdated"
DEPLOYMENT_CONFLICT = "conflict"

# Also exported for callers that need the legacy failed label
DEPLOYMENT_FAILED = "failed"

DEPLOY_BLOCKED_FOLDER_MISSING = "内容目录不存在，无法部署"
DEPLOY_BLOCKED_BACKUP_INVALID = "Backup 无效，无法部署"
DEPLOY_BLOCKED_IDENTITY_CONFLICT = "身份冲突，无法部署"
DEPLOY_BLOCKED_CONTENT_MISSING = "该 Mod 内容缺失，无法部署"
DEPLOY_ERR_MOD_PATH_MISSING = "Mod 安装目录不存在，请检查游戏设置"
DEPLOY_ERR_TARGET_FOREIGN = "目标目录已存在其他内容，无法部署"
DEPLOY_ERR_PERMISSION = "无法写入游戏 Mod 目录，请检查权限"
DEPLOY_ERR_COPY = "部署失败：文件复制错误"
DEPLOY_ERR_UNDEPLOY_MISMATCH = "部署清单不匹配，无法安全删除部署"

_IGNORE_DIR_NAMES = frozenset({INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"})


def deploy_block_reason_for_content_status(content_status: str | None) -> str | None:
    """Return a deploy-blocking error for unhealthy content, else None."""
    key = str(content_status or "").strip()
    if key == CONTENT_FOLDER_MISSING:
        return DEPLOY_BLOCKED_FOLDER_MISSING
    if key == CONTENT_BACKUP_INVALID:
        return DEPLOY_BLOCKED_BACKUP_INVALID
    if key == CONTENT_IDENTITY_CONFLICT:
        return DEPLOY_BLOCKED_IDENTITY_CONFLICT
    return None


def content_status_for_mod(
    mod_id: int | str,
    *,
    db: DatabaseManager | None = None,
) -> str:
    database = db if db is not None else get_db()
    mid = str(mod_id).strip()
    try:
        row = database.get_mod_backup_row(mid)
    except Exception:  # noqa: BLE001
        row = None
    return row_content_status(row)


def _iter_deployable_rel_files(source: Path) -> list[Path]:
    files: list[Path] = []
    root = Path(source)
    if not root.is_dir():
        return files
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in _IGNORE_DIR_NAMES or is_skipped_mod_path_part(part) for part in rel_parts):
            continue
        files.append(path)
    return files


def content_fingerprint(source: Path) -> str:
    """
    Lightweight fingerprint of deployable Library content.

    Uses relative path + size + mtime_ns (no full file hash).
    """
    root = Path(source)
    lines: list[str] = []
    for path in sorted(_iter_deployable_rel_files(root), key=lambda p: str(p).lower()):
        try:
            st = path.stat()
            rel = path.relative_to(root).as_posix()
            lines.append(f"{rel}|{st.st_size}|{getattr(st, 'st_mtime_ns', int(st.st_mtime * 1e9))}")
        except OSError:
            continue
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return digest


def folder_copy_target_for(
    managed: Path,
    *,
    mod_path: str | Path,
) -> Path:
    return (Path(mod_path).expanduser() / Path(managed).name).resolve()


def _norm(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path))


def _target_has_payload(target: Path) -> bool:
    if not target.exists():
        return False
    if target.is_file():
        return True
    try:
        for path in target.rglob("*"):
            if path.is_file():
                # Ignore empty marker-only trees
                if path.name in {INFO_DIR_NAME, LEGACY_INFO_DIR_NAME}:
                    continue
                return True
    except OSError:
        return True
    return False


def manifest_owns_target(manifest: DeployManifest | None, target: Path) -> bool:
    if manifest is None or not manifest.files:
        return False
    try:
        root = target.resolve()
    except OSError:
        root = Path(target)
    for entry in manifest.files:
        raw = str(entry.target or "").strip()
        if not raw:
            continue
        try:
            p = Path(raw).expanduser().resolve()
        except OSError:
            p = Path(raw)
        try:
            if p == root or p.is_relative_to(root) or root.is_relative_to(p):
                return True
        except (ValueError, AttributeError):
            if _norm(p).startswith(_norm(root)):
                return True
    return False


def classify_folder_copy_target(
    *,
    mod_id: str,
    managed: Path,
    mod_path: str | Path,
    library_root: str | Path | None = None,
) -> str:
    """
    Classify the folder_copy destination.

    Returns: ``absent`` | ``empty`` | ``ours`` | ``foreign``
    """
    mid = str(mod_id).strip()
    target = folder_copy_target_for(managed, mod_path=mod_path)
    if not target.exists():
        return "absent"
    if not _target_has_payload(target):
        return "empty"

    our = load_manifest(managed)
    if our is not None and str(our.mod_id or "").strip() in ("", mid):
        if manifest_owns_target(our, target):
            return "ours"
        # Manifest for this mod exists but does not claim target — still ours if
        # deploy_path folder name matches and no other owner.
        if str(our.mod_id or "").strip() == mid and not our.files:
            pass

    # Other Mods' manifests claiming files under target?
    if library_root is not None:
        try:
            mgr = ModFileManager(library_root)
            for folder in mgr.list_managed_mods():
                if folder.resolve() == Path(managed).resolve():
                    continue
                other = load_manifest(folder)
                if other is None:
                    continue
                other_id = str(other.mod_id or "").strip()
                if other_id and other_id == mid:
                    continue
                if manifest_owns_target(other, target):
                    return "foreign"
        except Exception:  # noqa: BLE001
            logger.debug("foreign ownership scan failed", exc_info=True)

    if our is not None and str(our.mod_id or "").strip() == mid:
        # Previously deployed by us but fingerprint/targets drifted — allow update
        if any(
            _norm(e.target).startswith(_norm(target)) for e in (our.files or [])
        ):
            return "ours"
        # Same mod_id on manifest with empty/partial claim: treat as ours for update
        return "ours"

    # Payload present, no safe ownership → conflict
    return "foreign"


def resolve_deployment_status(
    mod_id: int | str,
    *,
    library_root: str | Path | None = None,
    db: DatabaseManager | None = None,
    managed_path: Path | None = None,
) -> str:
    """
    Compute Phase 8 ``deployment_status`` without mutating disk/DB.
    """
    mid = str(mod_id).strip()
    database = db if db is not None else get_db()
    info = database.get_mod_deploy_info(mid) if mid.isdigit() else None
    db_status = (
        str(info.deploy_status or "").strip() if info else DEPLOY_STATUS_NOT_DEPLOYED
    ) or DEPLOY_STATUS_NOT_DEPLOYED

    if db_status == DEPLOY_STATUS_FAILED:
        return DEPLOYMENT_FAILED

    root = Path(library_root) if library_root else None
    source = managed_path
    if source is None and root is not None:
        source = ModFileManager(root).find_by_published_id(mid)

    # Conflict: configured folder_copy target exists but is foreign
    if source is not None and Path(source).is_dir() and info is not None:
        app_id = int(info.app_id or 0)
        if app_id:
            try:
                cfg = database.get_game_deploy_config(app_id)
            except Exception:  # noqa: BLE001
                cfg = None
            if cfg and str(cfg.mod_path or "").strip():
                kind = classify_folder_copy_target(
                    mod_id=mid,
                    managed=Path(source),
                    mod_path=cfg.mod_path,
                    library_root=root,
                )
                if kind == "foreign":
                    return DEPLOYMENT_CONFLICT

    if db_status != DEPLOY_STATUS_DEPLOYED:
        return DEPLOYMENT_NOT_DEPLOYED

    if source is None or not Path(source).is_dir():
        # Deployed in DB but library gone — still "deployed" from game side;
        # content_status handles library health separately.
        return DEPLOYMENT_DEPLOYED

    source = Path(source)
    manifest = load_manifest(source)
    if manifest is None:
        return DEPLOYMENT_DEPLOYED

    stored = str(getattr(manifest, "content_fingerprint", "") or "").strip()
    current = content_fingerprint(source)
    if stored and stored != current:
        return DEPLOYMENT_OUTDATED
    if not stored:
        # Legacy manifests: compare newest source mtime vs deploy_time string
        deploy_time = str(manifest.deploy_time or (info.deploy_time if info else "") or "")
        if deploy_time:
            try:
                newest = 0.0
                for path in _iter_deployable_rel_files(source):
                    try:
                        newest = max(newest, path.stat().st_mtime)
                    except OSError:
                        continue
                # ISO timestamps compare lexicographically when timezone-aware UTC
                from datetime import datetime, timezone

                try:
                    deployed_at = datetime.fromisoformat(deploy_time.replace("Z", "+00:00"))
                    if deployed_at.tzinfo is None:
                        deployed_at = deployed_at.replace(tzinfo=timezone.utc)
                    if newest > deployed_at.timestamp() + 1.0:
                        return DEPLOYMENT_OUTDATED
                except ValueError:
                    pass
            except Exception:  # noqa: BLE001
                pass
    return DEPLOYMENT_DEPLOYED


def install_path_missing(install_path: str | None) -> bool:
    """True when a configured install_path does not exist as a directory."""
    raw = str(install_path or "").strip()
    if not raw:
        return False
    return not Path(raw).expanduser().is_dir()


def resolve_game_install_path(
    *,
    mod_id: str | int | None = None,
    app_id: int | str | None = None,
    db: DatabaseManager | None = None,
) -> str:
    """
    Resolve ``games.install_path`` for a Mod.

    Flow (no game-name / AppID special cases):
      known app_id / mod.app_id → games row → install_path

    Returns ``""`` when the game cannot be resolved or install_path is unset.
    """
    database = db if db is not None else get_db()
    aid = 0
    try:
        aid = int(app_id or 0)
    except (TypeError, ValueError):
        aid = 0

    mid = str(mod_id or "").strip()
    if not aid and mid.isdigit():
        try:
            info = database.get_mod_display_info(mid)
        except Exception:  # noqa: BLE001
            info = None
        if info is not None:
            try:
                aid = int(info.app_id or 0)
            except (TypeError, ValueError):
                aid = 0

    if not aid:
        return ""

    try:
        cfg = database.get_game_deploy_config(aid)
    except Exception:  # noqa: BLE001
        return ""
    if cfg is None:
        return ""
    return str(cfg.install_path or "").strip()


def enrich_manifest_fingerprint(
    manifest: DeployManifest,
    *,
    source: Path,
    managed: Path | None = None,
) -> DeployManifest:
    """Attach fingerprint / source_path onto an in-memory manifest (mutates)."""
    fp = content_fingerprint(source)
    setattr(manifest, "content_fingerprint", fp)
    root = Path(managed) if managed is not None else Path(source)
    setattr(manifest, "source_path", str(root.resolve()))
    return manifest


def as_public_status(status: str) -> dict[str, Any]:
    return {"deployment_status": str(status or DEPLOYMENT_NOT_DEPLOYED)}
