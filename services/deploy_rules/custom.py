"""Highest-priority custom absolute-path deploy (bypasses game strategies)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from services.deploy_rules.base import (
    DeployContext,
    DeployStrategy,
    StrategyResult,
    inert_strategy_deploy,
)
from services.deploy_rules.generic import _iter_deployable_files
from services.deploy_rules.manifest import ManifestFileEntry, remove_empty_parents
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

logger = logging.getLogger(__name__)

DEPLOY_TYPE_CUSTOM_PATH = "custom_path"
_IGNORE_DIR_NAMES = frozenset({INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class CustomPathStrategy(DeployStrategy):
    """
    Path mapping into a user-specified absolute directory.

    Never wraps the payload in the managed folder name — maps content files
    into ``ctx.custom_deploy_path``. Skips ``.info`` / ``info``.
    Deployment I/O is owned by ModDeployer Core Apply (Phase 3).
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
                source_relative=src_file.relative_to(source).as_posix(),
                relative=src_file.relative_to(source).as_posix(),
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
        """Inert — Core Apply consumes ``plan()`` FilePlan entries."""
        return inert_strategy_deploy(self.deploy_type)

    def undeploy(
        self,
        ctx: DeployContext,
        manifest,
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
                try:
                    if target.resolve().is_relative_to(stop_at):
                        remove_empty_parents(target, stop_at=stop_at)
                except (ValueError, OSError):
                    pass

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
