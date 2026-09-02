"""Slay the Spire deploy: ``*.jar`` → ``<install>/mods/``; ModTheSpire → game root."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from services.deploy_rules.base import (
    DeployContext,
    DeployStrategy,
    StrategyResult,
    is_rel_path_allowed,
)
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    remove_empty_parents,
)
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, read_info_metadata_dict

logger = logging.getLogger(__name__)

SLAY_THE_SPIRE_APP_ID = 646570
# ModTheSpire (loader) — deploys into the game install root, not mods/.
PREREQUISITE_WORKSPACE_ID = "1605060445"
MODS_DIR_NAME = "mods"
ENTRY_TYPE_JAR = "jar"
ENTRY_TYPE_PREREQ = "prerequisite"

_IGNORE_DIR_NAMES = frozenset({INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ids_equal(left: Any, right: str) -> bool:
    """Compare workspace / published ids allowing str vs int."""
    if left is None:
        return False
    try:
        return str(int(str(left).strip())) == str(int(str(right).strip()))
    except (TypeError, ValueError):
        return str(left).strip() == str(right).strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _is_prerequisite_mod(ctx: DeployContext, meta: Mapping[str, Any] | None) -> bool:
    """
    Root-deploy when Workspace ID is ModTheSpire, or metadata marks prerequisite.

    Category ``前置`` alone is not enough (e.g. BaseMod is also tagged 前置 but
    must still land in ``mods/``).
    """
    data = dict(meta or {})
    for candidate in (
        getattr(ctx, "workspace_id", "") or "",
        ctx.mod_id,
        data.get("workspace_id"),
        data.get("published_file_id"),
    ):
        if _ids_equal(candidate, PREREQUISITE_WORKSPACE_ID):
            return True
    if _truthy(data.get("is_prerequisite")) or _truthy(data.get("is_prereq")):
        return True
    if _truthy(data.get("deploy_to_game_root")):
        return True
    return False


def _install_root(ctx: DeployContext) -> Path | StrategyResult:
    install = str(ctx.config.install_path or "").strip()
    if not install:
        return StrategyResult(
            success=False,
            error="请先配置游戏安装目录（杀戮尖塔部署需要安装路径）",
            deploy_type=SlayTheSpireStrategy.deploy_type,
        )
    root = Path(install).expanduser().resolve()
    if not root.is_dir():
        return StrategyResult(
            success=False,
            error=f"游戏安装目录不存在：{root}",
            deploy_type=SlayTheSpireStrategy.deploy_type,
        )
    return root


def _iter_source_files(
    source: Path,
    *,
    allowed_rel_paths: frozenset[str] | None = None,
    jars_only: bool = False,
) -> list[Path]:
    from services.deploy_fs import safe_iter_files

    files: list[Path] = []
    for path in sorted(
        safe_iter_files(source, suffix=".jar" if jars_only else None)
    ):
        try:
            rel_parts = path.relative_to(source).parts
        except ValueError:
            continue
        if any(part in _IGNORE_DIR_NAMES for part in rel_parts):
            continue
        if not is_rel_path_allowed(source, path, allowed_rel_paths):
            continue
        files.append(path)
    return files


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


class SlayTheSpireStrategy(DeployStrategy):
    """
    Slay the Spire (AppID 646570):

    - Normal Mods: copy every ``*.jar`` into ``<install>/mods/`` (create if needed).
    - Prerequisite / ModTheSpire (workspace ``1605060445``): copy payload into
      ``<install>/`` (game root).
    """

    deploy_type = "slay_the_spire"

    def _meta(self, ctx: DeployContext) -> dict[str, Any]:
        return read_info_metadata_dict(ctx.library_folder()) or {}

    def plan(self, ctx: DeployContext) -> StrategyResult:
        root = _install_root(ctx)
        if isinstance(root, StrategyResult):
            return root
        source = ctx.content_root().resolve()
        meta = self._meta(ctx)
        if _is_prerequisite_mod(ctx, meta):
            files = _iter_source_files(
                source, allowed_rel_paths=ctx.allowed_rel_paths, jars_only=False
            )
            entries = [
                ManifestFileEntry(
                    source=str(src),
                    target=str((root / src.relative_to(source)).resolve()),
                    type=ENTRY_TYPE_PREREQ,
                )
                for src in files
            ]
            target = str(root)
        else:
            mods_dir = root / MODS_DIR_NAME
            files = _iter_source_files(
                source, allowed_rel_paths=ctx.allowed_rel_paths, jars_only=True
            )
            entries = [
                ManifestFileEntry(
                    source=str(src),
                    target=str((mods_dir / src.name).resolve()),
                    type=ENTRY_TYPE_JAR,
                )
                for src in files
            ]
            target = str(mods_dir.resolve())
        if not entries:
            return StrategyResult(
                success=False,
                error=(
                    "没有可部署的文件"
                    if _is_prerequisite_mod(ctx, meta)
                    else "未找到可部署的 .jar 文件"
                ),
                deploy_type=self.deploy_type,
            )
        return StrategyResult(
            success=True,
            target=target,
            copied_files=len(entries),
            deploy_type=self.deploy_type,
            files=entries,
        )

    def deploy(self, ctx: DeployContext) -> StrategyResult:
        planned = self.plan(ctx)
        if not planned.success:
            return planned

        root = _install_root(ctx)
        if isinstance(root, StrategyResult):
            return root

        meta = self._meta(ctx)
        is_prereq = _is_prerequisite_mod(ctx, meta)
        if not is_prereq:
            mods_dir = root / MODS_DIR_NAME
            try:
                os.makedirs(mods_dir, exist_ok=True)
            except OSError as exc:
                err = str(exc).lower()
                if "permission" in err or "denied" in err:
                    return StrategyResult(
                        success=False,
                        error=f"Permission denied：无法创建 mods 目录（{exc}）",
                        deploy_type=self.deploy_type,
                    )
                return StrategyResult(
                    success=False,
                    error=f"无法创建 mods 目录：{mods_dir}（{exc}）",
                    deploy_type=self.deploy_type,
                )

        copied: list[ManifestFileEntry] = []
        for entry in planned.files:
            src = Path(entry.source)
            dst = Path(entry.target)
            try:
                _copy_file(src, dst)
            except OSError as exc:
                err = str(exc).lower()
                if "permission" in err or "denied" in err:
                    return StrategyResult(
                        success=False,
                        error=f"Permission denied：{src} → {dst}（{exc}）",
                        deploy_type=self.deploy_type,
                    )
                return StrategyResult(
                    success=False,
                    error=f"复制失败：{src} → {dst}（{exc}）",
                    deploy_type=self.deploy_type,
                )
            copied.append(
                ManifestFileEntry(
                    source=str(src.resolve()),
                    target=str(dst.resolve()),
                    type=entry.type,
                )
            )

        when = _utc_now()
        manifest = DeployManifest(
            mod_id=ctx.mod_id,
            deploy_time=when,
            deploy_type=self.deploy_type,
            files=copied,
        )
        logger.info(
            "[DEPLOY] slay_the_spire mod_id=%s prereq=%s files=%s target=%s",
            ctx.mod_id,
            is_prereq,
            len(copied),
            planned.target,
        )
        return StrategyResult(
            success=True,
            target=planned.target,
            copied_files=len(copied),
            deploy_type=self.deploy_type,
            deploy_time=when,
            files=copied,
            manifest=manifest,
        )

    def undeploy(
        self,
        ctx: DeployContext,
        manifest: DeployManifest | None,
    ) -> StrategyResult:
        if manifest is None or not manifest.files:
            return StrategyResult(success=True, deploy_type=self.deploy_type)

        stop_roots: list[Path] = []
        install = str(ctx.config.install_path or "").strip()
        if install:
            try:
                root = Path(install).expanduser().resolve()
                stop_roots.append(root)
                stop_roots.append(root / MODS_DIR_NAME)
            except OSError:
                pass

        errors: list[str] = []
        removed = 0
        for entry in manifest.files:
            target = Path(entry.target)
            try:
                if target.is_file() or (target.exists() and not target.is_dir()):
                    target.unlink()
                    removed += 1
            except OSError as exc:
                errors.append(f"{target}: {exc}")
                continue
            try:
                resolved = target.resolve()
            except OSError:
                resolved = target
            for stop_at in stop_roots:
                try:
                    if resolved.is_relative_to(stop_at):
                        remove_empty_parents(target, stop_at=stop_at)
                        break
                except (ValueError, OSError):
                    continue

        if errors:
            return StrategyResult(
                success=False,
                error="部分文件删除失败：" + "; ".join(errors[:3]),
                copied_files=removed,
                deploy_type=self.deploy_type,
            )
        return StrategyResult(
            success=True,
            copied_files=removed,
            deploy_type=self.deploy_type,
        )
