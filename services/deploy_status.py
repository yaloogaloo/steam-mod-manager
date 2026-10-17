"""Deployment status (Phase 8) — separate from Library ``content_status``.

Runtime statuses:

- ``not_deployed`` / ``deployed`` / ``outdated`` / ``conflict``

DB may still store legacy ``failed``. Manifest remains under Library ``.info``
(existing project convention — do not invent a second primary store).
"""

from __future__ import annotations

import hashlib
import logging
import time
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
    CONTENT_CONTENT_MISSING,
    row_content_status,
    row_identity_status,
)
from services.status_authority import (
    IDENTITY_STATUS_CONFLICT,
    normalize_content_axis,
    normalize_identity_status,
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
# Path-lifecycle specifics (prefer these over DEPLOY_ERR_MOD_PATH_MISSING).
DEPLOY_ERR_CUSTOM_PATH_MISSING = "Mod自定义部署路径不存在"
DEPLOY_ERR_GAME_INSTALL_MISSING = "游戏安装目录不存在"
DEPLOY_ERR_GAME_MOD_PATH_MISSING = "游戏Mod部署目录不存在"
DEPLOY_ERR_IDENTITY_RESOLVE = "Identity解析失败"
DEPLOY_ERR_ENTITY_DISK_MISSING = "Mod实体存在，但磁盘目录缺失"
DEPLOY_ERR_TARGET_FOREIGN = "目标目录已存在其他内容，无法部署"
DEPLOY_ERR_PERMISSION = "无法写入游戏 Mod 目录，请检查权限"
DEPLOY_ERR_COPY = "部署失败：文件复制错误"
DEPLOY_ERR_UNDEPLOY_MISMATCH = "部署清单不匹配，无法安全删除部署"

_IGNORE_DIR_NAMES = frozenset({INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"})


def deploy_block_reason_for_content_status(content_status: str | None) -> str | None:
    """Return a deploy-blocking error for content_missing, else None."""
    if normalize_content_axis(content_status) == CONTENT_CONTENT_MISSING:
        return DEPLOY_BLOCKED_CONTENT_MISSING
    return None


def deploy_block_reason_for_identity_status(identity_status: str | None) -> str | None:
    """Return a deploy-blocking error for identity conflicts, else None."""
    if normalize_identity_status(identity_status) == IDENTITY_STATUS_CONFLICT:
        return DEPLOY_BLOCKED_IDENTITY_CONFLICT
    return None


def deploy_block_reason_for_mod_row(row: dict[str, Any] | None) -> str | None:
    """Combined content + identity deploy gate from a DB backup row."""
    if not row:
        return None
    return (
        deploy_block_reason_for_content_status(row_content_status(row))
        or deploy_block_reason_for_identity_status(row_identity_status(row))
    )


def content_status_for_mod(
    mod_pk: int | str,
    *,
    db: DatabaseManager | None = None,
) -> str:
    from services.mod_library_cache import dal_mod_pk

    database = db if db is not None else get_db()
    mid = dal_mod_pk(mod_pk)
    if not mid:
        return row_content_status(None)
    try:
        row = database.get_mod_backup_row(mid)
    except Exception:  # noqa: BLE001
        row = None
    return row_content_status(row)


def _iter_deployable_rel_files(source: Path) -> list[Path]:
    from services.deploy_fs import safe_iter_files

    files: list[Path] = []
    root = Path(source)
    if not root.is_dir():
        return files
    for path in safe_iter_files(root):
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in _IGNORE_DIR_NAMES or is_skipped_mod_path_part(part) for part in rel_parts):
            continue
        files.append(path)
    return files


def content_fingerprint(
    source: Path,
    *,
    files: list[tuple[str, Path]] | None = None,
) -> str:
    """
    Lightweight fingerprint of deployable Library content.

    Uses relative path + size + mtime_ns (no full file hash).
    When *files* is provided (FilePlan sources), do not walk the source tree.
    """
    root = Path(source)
    lines: list[str] = []
    stat_count = 0
    reused_sizes = 0
    sizes: dict[str, int] = {}
    if files is not None:
        try:
            from services.deploy_apply import current_apply_source_sizes

            sizes = current_apply_source_sizes()
        except Exception:  # noqa: BLE001
            sizes = {}
        items = files
    else:
        items = [
            (path.relative_to(root).as_posix(), path)
            for path in _iter_deployable_rel_files(root)
        ]
    for rel, path in sorted(items, key=lambda item: str(item[0]).lower()):
        size = None
        mtime_ns = 0
        if sizes:
            key = str(path)
            if key not in sizes:
                try:
                    from services.deploy_op_profile import cached_resolve

                    key = cached_resolve(path)
                except OSError:
                    key = str(path)
            if key in sizes:
                size = int(sizes[key])
                reused_sizes += 1
        if size is None:
            try:
                t_stat = time.perf_counter()
                st = path.stat()
                stat_count += 1
                size = int(st.st_size)
                mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
                from services.deploy_op_profile import record_op

                record_op(
                    "stat",
                    (time.perf_counter() - t_stat) * 1000.0,
                    path=str(path),
                )
            except OSError:
                continue
        lines.append(f"{rel}|{size}|{mtime_ns}")
    try:
        from services.deploy_stage_log import current_deploy_timing

        sess = current_deploy_timing()
        if sess is not None:
            sess.diagnostics["fingerprint_stat_count"] = stat_count
            sess.diagnostics["fingerprint_reused_copy_sizes"] = reused_sizes
            sess.diagnostics["fingerprint_file_count"] = len(lines)
    except Exception:  # noqa: BLE001
        pass
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return digest


def folder_copy_target_for(
    managed: Path,
    *,
    mod_path: str | Path,
    workspace_id: str = "",
) -> Path:
    """Game-mods wrapper for *managed*. Uses deploy_wrapper_folder, never PK."""
    from services.deploy_rules.generic import deploy_wrapper_folder

    basename = Path(managed).name
    ws = str(workspace_id or "").strip()
    try:
        name = deploy_wrapper_folder(basename, ws) if ws else basename
    except ValueError:
        name = basename
    return (Path(mod_path).expanduser() / name).resolve()


def _norm(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path))


def _target_has_payload(target: Path) -> bool:
    from services.deploy_fs import safe_iter_files

    if not target.exists():
        return False
    if target.is_file():
        return True
    for path in safe_iter_files(target):
        if path.name in {INFO_DIR_NAME, LEGACY_INFO_DIR_NAME}:
            continue
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
    internal_id: str,
    managed: Path,
    mod_path: str | Path,
    library_root: str | Path | None = None,
    mod_pk: str | int | None = None,
    workspace_id: str = "",
) -> str:
    """
    Classify the folder_copy destination.

    Returns: ``absent`` | ``empty`` | ``ours`` | ``foreign``

    *internal_id* is Frozen UUID. *mod_pk* is SQLite PK for legacy manifests
    that only store JSON ``mod_id``. Ownership is never inferred from
    folder.name, Workshop ID, or numeric tokens.
    """
    mid = str(internal_id).strip()
    pk = str(mod_pk or "").strip()

    def _ours(manifest) -> bool:
        frozen = str(manifest.internal_id or "").strip()
        claimed_pk = str(manifest.mod_id or "").strip()
        if frozen and frozen == mid:
            return True
        if pk and claimed_pk == pk:
            return True
        if claimed_pk and claimed_pk == mid:
            return True
        return False
    target = folder_copy_target_for(
        managed, mod_path=mod_path, workspace_id=workspace_id
    )
    if not target.exists():
        return "absent"
    if not _target_has_payload(target):
        return "empty"

    our = load_manifest(managed)
    if our is not None and (
        not str(our.internal_id or our.mod_id or "").strip() or _ours(our)
    ):
        if manifest_owns_target(our, target):
            return "ours"
        # Manifest for this mod exists but does not claim target — still ours if
        # deploy_path folder name matches and no other owner.
        if _ours(our) and not our.files:
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
                if _ours(other):
                    continue
                if manifest_owns_target(other, target):
                    return "foreign"
        except Exception:  # noqa: BLE001
            logger.debug("foreign ownership scan failed", exc_info=True)

    if our is not None and _ours(our):
        # Previously deployed by us but fingerprint/targets drifted — allow update
        if any(
            _norm(e.target).startswith(_norm(target)) for e in (our.files or [])
        ):
            return "ours"
        # Same identity on manifest with empty/partial claim: treat as ours for update
        return "ours"

    # Payload present, no safe ownership → conflict
    return "foreign"


def resolve_deployment_status(
    internal_id: int | str,
    *,
    library_root: str | Path | None = None,
    db: DatabaseManager | None = None,
    managed_path: Path | None = None,
) -> str:
    """
    Compute Phase 8 ``deployment_status`` without mutating disk/DB.

    IMPORTANT:
    This may scan the managed folder (``content_fingerprint``) and walk other Mod
    manifests. Do not call from the Qt GUI thread — use a worker (see Detail panel).
    """
    from services.deploy_paths import (
        resolve_deploy_identity,
        resolve_deploy_managed_path,
    )

    database = db if db is not None else get_db()
    frozen = str(internal_id or "").strip()
    mid = resolve_deploy_identity(frozen, db=database)
    info = database.get_mod_deploy_info(mid) if mid.isdigit() else None
    db_status = (
        str(info.deploy_status or "").strip() if info else DEPLOY_STATUS_NOT_DEPLOYED
    ) or DEPLOY_STATUS_NOT_DEPLOYED

    if db_status == DEPLOY_STATUS_FAILED:
        return DEPLOYMENT_FAILED

    if db_status != DEPLOY_STATUS_DEPLOYED:
        return DEPLOYMENT_NOT_DEPLOYED

    root = Path(library_root) if library_root else None
    source = managed_path
    if source is None:
        source = resolve_deploy_managed_path(
            frozen or mid,
            db=database,
            library_root=root,
            file_manager=ModFileManager(root) if root is not None else None,
        )

    # Conflict: configured folder_copy target exists but is foreign (deployed only).
    if source is not None and Path(source).is_dir() and info is not None:
        app_id = int(info.app_id or 0)
        if app_id:
            try:
                cfg = database.get_game_deploy_config(app_id)
            except Exception:  # noqa: BLE001
                cfg = None
            if cfg and str(cfg.mod_path or "").strip():
                workspace_id = ""
                try:
                    display = database.get_mod_display_info(mid)
                    if display is not None:
                        workspace_id = str(display.workspace_id or "").strip()
                except Exception:  # noqa: BLE001
                    workspace_id = ""
                kind = classify_folder_copy_target(
                    internal_id=frozen,
                    mod_pk=mid,
                    managed=Path(source),
                    mod_path=cfg.mod_path,
                    library_root=root,
                    workspace_id=workspace_id,
                )
                if kind == "foreign":
                    return DEPLOYMENT_CONFLICT

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
    mod_pk: str | int | None = None,
    internal_id: str | int | None = None,
    app_id: int | str | None = None,
    db: DatabaseManager | None = None,
) -> str:
    """
    Resolve ``games.install_path`` for a Mod.

    ``mod_pk`` is SQLite ``mods.mod_id``. ``internal_id`` remains a compatibility
    alias for Frozen UUID or digit PK (resolved via ``dal_mod_pk``).

    Flow (no game-name / AppID special cases):
      known app_id / mod.app_id → games row → install_path

    Returns ``""`` when the game cannot be resolved or install_path is unset.
    """
    from services.mod_library_cache import dal_mod_pk

    database = db if db is not None else get_db()
    aid = 0
    try:
        aid = int(app_id or 0)
    except (TypeError, ValueError):
        aid = 0

    token = str(mod_pk if mod_pk is not None else internal_id or "").strip()
    pk = dal_mod_pk(token) if token else ""
    if not aid and pk:
        try:
            info = database.get_mod_display_info(pk)
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
    plan_files: list | None = None,
) -> DeployManifest:
    """Attach fingerprint / source_path onto an in-memory manifest (mutates)."""
    fp_files: list[tuple[str, Path]] | None = None
    if plan_files:
        fp_files = []
        for entry in plan_files:
            rel = str(
                getattr(entry, "source_relative", "")
                or getattr(entry, "relative", "")
                or ""
            ).replace("\\", "/").strip()
            raw = str(getattr(entry, "source", "") or "").strip()
            if not rel or not raw:
                continue
            fp_files.append((rel, Path(raw)))
    fp = content_fingerprint(source, files=fp_files)
    setattr(manifest, "content_fingerprint", fp)
    root = Path(managed) if managed is not None else Path(source)
    setattr(manifest, "source_path", str(root.resolve()))
    return manifest

