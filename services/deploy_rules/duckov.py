"""Escape from Duckov (AppID 3167020) — folder_copy + info.ini validation."""

from __future__ import annotations

import logging
from pathlib import Path

from services.deploy_rules.base import DeployContext, StrategyResult
from services.deploy_rules.generic import FolderCopyStrategy
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

logger = logging.getLogger(__name__)

DUCKOV_APP_ID = 3167020
DUCKOV_INFO_INI = "info.ini"
DEPLOY_TYPE_DUCKOV = "duckov"

_MISSING_MOD_PATH = (
    "无法部署：当前游戏尚未配置 Mod 部署目录。"
    "请先在游戏设置中配置 Mod 部署目录。"
)
_MISSING_INFO_INI_SOURCE = (
    "逃离鸭科夫 Mod 部署失败：源 Mod 中未找到 info.ini。"
)
_MISSING_INFO_INI_TARGET = (
    "逃离鸭科夫 Mod 部署失败：部署完成后未找到 info.ini，"
    "可能存在错误的压缩包目录嵌套。"
)
_NESTED_INFO_INI = (
    "逃离鸭科夫 Mod 部署失败：检测到错误的目录嵌套"
    "（info.ini 不在 Mod 根目录）。"
)

_IGNORE_PARTS = frozenset({INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"})


def _is_library_info_ini(path: Path) -> bool:
    """Ignore Steam Mod Manager ``.info/`` — not game ``info.ini``."""
    return INFO_DIR_NAME in path.parts or LEGACY_INFO_DIR_NAME in path.parts


def find_duckov_mod_root(extract_root: str | Path) -> Path | None:
    """
    Resolve Duckov mod content root inside an extracted archive tree.

    Prefers the shallowest ``info.ini`` (excluding ``.info/``). Unwraps a single
    wrapper folder when ``Wrapper/info.ini`` is the only mod layout.
    """
    root = Path(extract_root).expanduser().resolve()
    if not root.is_dir():
        return None

    if (root / DUCKOV_INFO_INI).is_file():
        return root

    from services.deploy_fs import safe_iter_files

    candidates: list[tuple[int, Path]] = []
    try:
        for ini in safe_iter_files(root, name=DUCKOV_INFO_INI):
            if _is_library_info_ini(ini):
                continue
            try:
                depth = len(ini.parent.relative_to(root).parts)
            except ValueError:
                continue
            candidates.append((depth, ini.parent.resolve()))
    except OSError:
        return None

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def normalize_duckov_content_root(content_root: Path) -> Path:
    """Return a root where ``info.ini`` sits at the mod content top level."""
    root = Path(content_root).resolve()
    if (root / DUCKOV_INFO_INI).is_file():
        return root
    found = find_duckov_mod_root(root)
    if found is not None and (found / DUCKOV_INFO_INI).is_file():
        return found
    return root


def validate_duckov_target(target: Path, *, folder_name: str) -> str | None:
    """
    Validate deployed Duckov mod layout.

    Requires ``<target>/info.ini``. Rejects common wrong nestings.
    Returns an error message or ``None`` when valid.
    """
    target = target.resolve()
    info = target / DUCKOV_INFO_INI
    if info.is_file():
        nested = target / folder_name / DUCKOV_INFO_INI
        if nested.is_file():
            return _NESTED_INFO_INI
        return None

    nested = target / folder_name / DUCKOV_INFO_INI
    if nested.is_file():
        return _MISSING_INFO_INI_TARGET

    from services.deploy_fs import safe_iter_files

    for ini in safe_iter_files(target, name=DUCKOV_INFO_INI):
        if not _is_library_info_ini(ini):
            return _NESTED_INFO_INI

    return _MISSING_INFO_INI_TARGET


def _ctx_with_source(ctx: DeployContext, source: Path) -> DeployContext:
    return DeployContext(
        mod_id=ctx.mod_id,
        source=source,
        app_id=ctx.app_id,
        config=ctx.config,
        deploy_type=ctx.deploy_type,
        allowed_rel_paths=ctx.allowed_rel_paths,
        managed_path=ctx.managed_path,
        custom_deploy_path=ctx.custom_deploy_path,
        workspace_id=ctx.workspace_id,
    )


class DuckovStrategy(FolderCopyStrategy):
    """
    Escape from Duckov: ``folder_copy`` into ``game.mod_path/<folder>/`` with
    mandatory ``info.ini`` at the mod root.
    """

    deploy_type = DEPLOY_TYPE_DUCKOV

    def _prepare_ctx(self, ctx: DeployContext) -> tuple[DeployContext, StrategyResult | None]:
        mod_path_raw = str(ctx.config.mod_path or "").strip()
        if not mod_path_raw:
            return ctx, StrategyResult(
                success=False,
                error=_MISSING_MOD_PATH,
                deploy_type=self.deploy_type,
            )

        normalized = normalize_duckov_content_root(ctx.content_root())
        if not (normalized / DUCKOV_INFO_INI).is_file():
            return ctx, StrategyResult(
                success=False,
                error=_MISSING_INFO_INI_SOURCE,
                deploy_type=self.deploy_type,
            )
        if normalized != ctx.content_root().resolve():
            logger.info(
                "[DEPLOY] duckov normalized content root %s -> %s",
                ctx.content_root(),
                normalized,
            )
        return _ctx_with_source(ctx, normalized), None

    def plan(self, ctx: DeployContext) -> StrategyResult:
        prepared, err = self._prepare_ctx(ctx)
        if err is not None:
            return err
        result = super().plan(prepared)
        if not result.success:
            result.deploy_type = self.deploy_type
            return result
        result.deploy_type = self.deploy_type
        return result

    def deploy(self, ctx: DeployContext) -> StrategyResult:
        prepared, err = self._prepare_ctx(ctx)
        if err is not None:
            return err
        result = super().deploy(prepared)
        if not result.success:
            result.deploy_type = self.deploy_type
            return result
        target_err = validate_duckov_target(
            Path(result.target),
            folder_name=ctx.library_folder().name,
        )
        if target_err:
            return StrategyResult(
                success=False,
                error=target_err,
                deploy_type=self.deploy_type,
            )
        result.deploy_type = self.deploy_type
        logger.info(
            "[DEPLOY] duckov mod_id=%s target=%s info.ini=ok files=%s",
            ctx.mod_id,
            result.target,
            result.copied_files,
        )
        return result

    def undeploy(
        self,
        ctx: DeployContext,
        manifest,
    ) -> StrategyResult:
        result = super().undeploy(ctx, manifest)
        result.deploy_type = self.deploy_type
        return result
