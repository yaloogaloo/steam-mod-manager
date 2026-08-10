"""Highest-priority custom absolute-path deploy (bypasses game strategies)."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from services.deploy_rules.base import DeployContext, DeployStrategy, StrategyResult
from services.deploy_rules.generic import _deploy_ignore, _iter_deployable_files
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    remove_empty_parents,
)
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

logger = logging.getLogger(__name__)

DEPLOY_TYPE_CUSTOM_PATH = "custom_path"
_IGNORE_DIR_NAMES = frozenset({INFO_DIR_NAME, LEGACY_INFO_DIR_NAME})


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class CustomPathStrategy(DeployStrategy):
    """
    Copy Mod *contents* into a user-specified absolute directory.

    Never wraps the payload in the managed folder name — merges file/dir
    children into ``ctx.custom_deploy_path`` (like ``copytree(..., dirs_exist_ok)``).
    Skips ``.info`` / ``info``. Overrides Anno / Palworld / folder_copy rules.
    """

    deploy_type = DEPLOY_TYPE_CUSTOM_PATH

    def plan(self, ctx: DeployContext) -> StrategyResult:
        raw = str(ctx.custom_deploy_path or "").strip()
        if not raw:
            return StrategyResult(
                success=False,
                error="未配置自定义部署目录",
                deploy_type=self.deploy_type,
            )
        target = Path(raw).expanduser().resolve()
        source = ctx.content_root().resolve()

        try:
            if (
                source == target
                or target.is_relative_to(source)
                or source.is_relative_to(target)
            ):
                return StrategyResult(
                    success=False,
                    error=f"源与目标路径冲突：source={source} target={target}",
                    deploy_type=self.deploy_type,
                )
        except AttributeError:
            pass

        files = _iter_deployable_files(
            source, allowed_rel_paths=ctx.allowed_rel_paths
        )
        entries = [
            ManifestFileEntry(
                source=str(src_file),
                target=str((target / src_file.relative_to(source)).resolve()),
                type=self.deploy_type,
            )
            for src_file in files
        ]
        return StrategyResult(
            success=True,
            target=str(target),
            copied_files=len(entries),
            deploy_type=self.deploy_type,
            files=entries,
        )

    def deploy(self, ctx: DeployContext) -> StrategyResult:
        planned = self.plan(ctx)
        if not planned.success:
            return planned

        target = Path(planned.target)
        source = ctx.content_root().resolve()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            err = str(exc).lower()
            if "permission" in err or "denied" in err or "拒绝" in str(exc):
                msg = f"Permission denied：无法创建自定义部署目录（{exc}）"
            else:
                msg = f"无法创建自定义部署目录：{target}（{exc}）"
            return StrategyResult(
                success=False, error=msg, deploy_type=self.deploy_type
            )

        # Merge content of source into target — never nest the managed shell folder.
        if ctx.allowed_rel_paths is None:
            try:
                for child in source.iterdir():
                    if child.name in _IGNORE_DIR_NAMES:
                        continue
                    dest = target / child.name
                    if child.is_dir():
                        shutil.copytree(
                            child,
                            dest,
                            dirs_exist_ok=True,
                            ignore=_deploy_ignore,
                        )
                    elif child.is_file():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(child, dest)
            except OSError as exc:
                err = str(exc).lower()
                if "permission" in err or "denied" in err:
                    return StrategyResult(
                        success=False,
                        error=f"Permission denied：{exc}",
                        deploy_type=self.deploy_type,
                    )
                return StrategyResult(
                    success=False,
                    error=f"复制失败：{exc}",
                    deploy_type=self.deploy_type,
                )
        else:
            for entry in planned.files:
                src = Path(entry.source)
                dst = Path(entry.target)
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                except OSError as exc:
                    err = str(exc).lower()
                    if "permission" in err or "denied" in err:
                        return StrategyResult(
                            success=False,
                            error=f"Permission denied：{exc}",
                            deploy_type=self.deploy_type,
                        )
                    return StrategyResult(
                        success=False,
                        error=f"复制失败：{exc}",
                        deploy_type=self.deploy_type,
                    )

        for banned in _IGNORE_DIR_NAMES:
            if (target / banned).exists():
                return StrategyResult(
                    success=False,
                    error=f"部署异常：目标不应包含 {banned}",
                    deploy_type=self.deploy_type,
                )

        when = _utc_now()
        # Rebuild file list after copy so manifest matches on-disk payload.
        files = _iter_deployable_files(
            source, allowed_rel_paths=ctx.allowed_rel_paths
        )
        entries = [
            ManifestFileEntry(
                source=str(src_file),
                target=str((target / src_file.relative_to(source)).resolve()),
                type=self.deploy_type,
            )
            for src_file in files
        ]
        manifest = DeployManifest(
            mod_id=ctx.mod_id,
            deploy_time=when,
            deploy_type=self.deploy_type,
            files=entries,
        )
        logger.info(
            "[DEPLOY] custom_path mod_id=%s target=%s files=%s",
            ctx.mod_id,
            target,
            len(entries),
        )
        return StrategyResult(
            success=True,
            target=str(target),
            copied_files=len(entries),
            deploy_type=self.deploy_type,
            deploy_time=when,
            files=entries,
            manifest=manifest,
        )

    def undeploy(
        self,
        ctx: DeployContext,
        manifest: DeployManifest | None,
    ) -> StrategyResult:
        if manifest is None or not manifest.files:
            return StrategyResult(
                success=True,
                deploy_type=self.deploy_type,
                error="",
            )

        stop_at: Path | None = None
        raw = str(ctx.custom_deploy_path or "").strip()
        if raw:
            stop_at = Path(raw).expanduser().resolve()

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
            if stop_at is not None:
                remove_empty_parents(target, stop_at=stop_at)

        if errors:
            return StrategyResult(
                success=False,
                error="; ".join(errors[:5]),
                deploy_type=self.deploy_type,
                copied_files=removed,
            )
        return StrategyResult(
            success=True,
            deploy_type=self.deploy_type,
            copied_files=removed,
        )
