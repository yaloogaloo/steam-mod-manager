"""Anno 1800 deploy: loose folders → ``mods/<name>/``; archives → extract into ``mods/``."""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from core.db_manager import GameDeployConfig
from services.deploy_rules.base import DeployContext, DeployStrategy, StrategyResult
from services.deploy_rules.generic import FolderCopyStrategy
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry

logger = logging.getLogger(__name__)

ANNO_1800_APP_ID = 916440
MODS_DIR_NAME = "mods"
_ANNO_DEPLOY_TYPE = "anno_1800"


def resolve_anno_mods_root(config: GameDeployConfig) -> Path | None:
    """
    Anno 1800 Mod root: ``<install_path>/mods``.

    Falls back to configured ``mod_path`` when install_path is empty
    (user already pointed deploy at the mods folder).
    """
    install = str(config.install_path or "").strip()
    if install:
        return Path(install).expanduser() / MODS_DIR_NAME
    mod_path = str(config.mod_path or "").strip()
    if mod_path:
        return Path(mod_path).expanduser()
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _snapshot_files(root: Path) -> set[Path]:
    if not root.is_dir():
        return set()
    return {p.resolve() for p in root.rglob("*") if p.is_file()}


def _manifest_entries_for_paths(
    mods_root: Path,
    deployed: list[Path],
    *,
    source: str,
) -> list[ManifestFileEntry]:
    root = mods_root.resolve()
    entries: list[ManifestFileEntry] = []
    for path in deployed:
        try:
            path.resolve().relative_to(root)
        except ValueError:
            continue
        entries.append(
            ManifestFileEntry(
                source=source,
                target=str(path.resolve()),
            )
        )
    return entries


def _plan_anno_archive_deploy(
    ctx: DeployContext,
    mods_root: Path,
    archives: list[Path],
) -> StrategyResult:
    """Dry-run: extract archives to a temp dir (preserve zip roots) → map under mods/."""
    from services.importers.archive import (
        cleanup_import_cache,
        extract_archive,
        import_cache_root,
    )

    stage = import_cache_root() / f"anno_plan_{uuid.uuid4().hex}"
    stage.mkdir(parents=True, exist_ok=False)
    try:
        for archive in archives:
            extract_archive(archive, dest_dir=stage)
        files = sorted(p for p in stage.rglob("*") if p.is_file())
        if not files:
            return StrategyResult(
                success=False,
                error="压缩包解压后没有可部署的文件",
                deploy_type=_ANNO_DEPLOY_TYPE,
            )
        entries: list[ManifestFileEntry] = []
        for path in files:
            rel = path.relative_to(stage)
            entries.append(
                ManifestFileEntry(
                    source=str(archives[0]),
                    target=str((mods_root / rel).resolve()),
                )
            )
        return StrategyResult(
            success=True,
            target=str(mods_root.resolve()),
            copied_files=len(entries),
            deploy_type=_ANNO_DEPLOY_TYPE,
            files=entries,
        )
    except OSError as exc:
        err = str(exc).lower()
        if "permission" in err or "denied" in err:
            msg = f"Permission denied：{exc}"
        else:
            msg = f"解压失败：{exc}"
        return StrategyResult(
            success=False, error=msg, deploy_type=_ANNO_DEPLOY_TYPE
        )
    finally:
        cleanup_import_cache(stage)


def _deploy_anno_archives_to_mods_root(
    ctx: DeployContext,
    mods_root: Path,
    archives: list[Path],
) -> StrategyResult:
    """
    Extract archives directly into ``mods/`` — no managed-folder wrapper, no root strip.
    """
    from services.importers.archive import extract_archive

    target_dir = mods_root.resolve()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        err = str(exc).lower()
        if "permission" in err or "denied" in err or "拒绝" in str(exc):
            msg = f"Permission denied：无法创建 mods 目录（{exc}）"
        else:
            msg = f"无法创建 mods 目录：{target_dir}（{exc}）"
        return StrategyResult(
            success=False, error=msg, deploy_type=_ANNO_DEPLOY_TYPE
        )

    before = _snapshot_files(target_dir)
    source_label = str(archives[0]) if len(archives) == 1 else str(ctx.library_folder())
    try:
        for archive in archives:
            extract_archive(archive, dest_dir=target_dir)
    except OSError as exc:
        err = str(exc).lower()
        if "permission" in err or "denied" in err:
            return StrategyResult(
                success=False,
                error=f"Permission denied：{exc}",
                deploy_type=_ANNO_DEPLOY_TYPE,
            )
        return StrategyResult(
            success=False,
            error=f"解压失败：{exc}",
            deploy_type=_ANNO_DEPLOY_TYPE,
        )

    deployed = sorted(_snapshot_files(target_dir) - before)
    entries = _manifest_entries_for_paths(
        target_dir, deployed, source=source_label
    )
    if not entries:
        return StrategyResult(
            success=False,
            error="压缩包解压后没有可部署的文件",
            deploy_type=_ANNO_DEPLOY_TYPE,
        )

    when = _utc_now()
    manifest = DeployManifest(
        mod_id=ctx.mod_id,
        deploy_time=when,
        deploy_type=_ANNO_DEPLOY_TYPE,
        files=entries,
    )
    return StrategyResult(
        success=True,
        target=str(target_dir),
        copied_files=len(entries),
        deploy_type=_ANNO_DEPLOY_TYPE,
        deploy_time=when,
        files=entries,
        manifest=manifest,
    )


class Anno1800Strategy(DeployStrategy):
    """
    Anno 1800 / 纪元1800:

    - Archive mods (mod.io zip): extract into ``<install>/mods/`` preserving
      in-archive folder names — never ``mods/<library_folder>/``.
    - Loose directory mods: ``mods/<managed_folder_name>/`` via folder_copy.
    """

    deploy_type = "anno_1800"

    def __init__(self) -> None:
        self._folder = FolderCopyStrategy()

    def _patched_context(self, ctx: DeployContext) -> DeployContext | StrategyResult:
        mods_root = resolve_anno_mods_root(ctx.config)
        if mods_root is None:
            return StrategyResult(
                success=False,
                error="请先配置游戏安装目录（将部署到 mods/）",
                deploy_type=self.deploy_type,
            )
        try:
            mods_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            err = str(exc).lower()
            if "permission" in err or "denied" in err or "拒绝" in str(exc):
                msg = f"Permission denied：无法创建 mods 目录（{exc}）"
            else:
                msg = f"无法创建 mods 目录：{mods_root}（{exc}）"
            return StrategyResult(
                success=False, error=msg, deploy_type=self.deploy_type
            )

        cfg = replace(ctx.config, mod_path=str(mods_root))
        return replace(ctx, config=cfg, deploy_type=FolderCopyStrategy.deploy_type)

    def _retag(self, result: StrategyResult) -> StrategyResult:
        result.deploy_type = self.deploy_type
        if result.manifest is not None:
            man = result.manifest
            result.manifest = DeployManifest(
                mod_id=man.mod_id,
                deploy_time=man.deploy_time,
                deploy_type=self.deploy_type,
                files=list(man.files),
            )
        return result

    def _archive_paths(self, ctx: DeployContext) -> list[Path]:
        from services.deploy import collect_deploy_archives

        return collect_deploy_archives(
            ctx.mod_id, ctx.library_folder()
        )

    def plan(self, ctx: DeployContext) -> StrategyResult:
        patched = self._patched_context(ctx)
        if isinstance(patched, StrategyResult):
            return patched
        archives = self._archive_paths(patched)
        if archives:
            mods_root = resolve_anno_mods_root(patched.config)
            assert mods_root is not None
            return self._retag(
                _plan_anno_archive_deploy(patched, mods_root, archives)
            )
        return self._retag(self._folder.plan(patched))

    def deploy(self, ctx: DeployContext) -> StrategyResult:
        patched = self._patched_context(ctx)
        if isinstance(patched, StrategyResult):
            return patched
        archives = self._archive_paths(patched)
        if archives:
            mods_root = resolve_anno_mods_root(patched.config)
            assert mods_root is not None
            return self._retag(
                _deploy_anno_archives_to_mods_root(patched, mods_root, archives)
            )
        return self._retag(self._folder.deploy(patched))

    def undeploy(
        self,
        ctx: DeployContext,
        manifest: DeployManifest | None,
    ) -> StrategyResult:
        patched = self._patched_context(ctx)
        if isinstance(patched, StrategyResult):
            return self._retag(self._folder.undeploy(ctx, manifest))
        return self._retag(self._folder.undeploy(patched, manifest))
