"""Generic folder_copy deploy strategy (copytree into game.mod_path)."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from services.deploy_rules.base import (
    DeployContext,
    DeployStrategy,
    StrategyResult,
    inert_strategy_deploy,
    is_rel_path_allowed,
)
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    remove_empty_parents,
)
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME
from services.importers.local_scanner import is_skipped_mod_path_part

logger = logging.getLogger(__name__)

_IGNORE_DIR_NAMES = frozenset({INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"})

# CJK Unified Ideographs — Civ VI deploy-folder rule only (not a global path validator).
_CJK_HAN_START = "\u4e00"
_CJK_HAN_END = "\u9fff"


def contains_chinese(text: str) -> bool:
    """True when *text* contains at least one CJK Unified Ideograph (汉字)."""
    return any(_CJK_HAN_START <= ch <= _CJK_HAN_END for ch in str(text or ""))


def _civ6_deploy_folder_name(ctx: DeployContext) -> str:
    """
    Civ VI only: Chinese library folder names cannot activate in-game.

    Map deploy target folder to ``workspace_id`` only. Never use
    ``ctx.internal_id`` here — in DeployContext that field holds the SQLite
    ``mods.mod_id`` PK, which must not become the on-disk deploy folder name.
    Never rename the library source folder.
    """
    folder_name = ctx.library_folder().name
    from core.mod_platform import is_civilization_vi_game

    if not is_civilization_vi_game(game_id=ctx.app_id):
        return folder_name
    if not contains_chinese(folder_name):
        return folder_name
    wid = str(ctx.workspace_id or "").strip()
    return wid if wid else folder_name


def _deploy_ignore(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in _IGNORE_DIR_NAMES or is_skipped_mod_path_part(name)}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _iter_deployable_files(
    source: Path,
    *,
    allowed_rel_paths: frozenset[str] | None = None,
) -> list[Path]:
    from services.deploy_fs import safe_iter_files

    files: list[Path] = []
    for path in safe_iter_files(source):
        try:
            rel_parts = path.relative_to(source).parts
        except ValueError:
            continue
        if any(is_skipped_mod_path_part(part) for part in rel_parts):
            continue
        if not is_rel_path_allowed(source, path, allowed_rel_paths):
            continue
        files.append(path)
    return files


class FolderCopyStrategy(DeployStrategy):
    """
    Copy managed Mod folder into ``game.mod_path/<mod_folder>/``.

    Skips ``.info`` / ``info``. Records every copied file in the result.
    """

    deploy_type = "folder_copy"

    def plan(self, ctx: DeployContext) -> StrategyResult:
        mod_path_raw = str(ctx.config.mod_path or "").strip()
        if not mod_path_raw:
            return StrategyResult(success=False, error="请先配置游戏部署目录")

        mod_path = Path(mod_path_raw).expanduser()
        folder_name = _civ6_deploy_folder_name(ctx)
        target = (mod_path / folder_name).resolve()
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
        manifest: DeployManifest | None,
    ) -> StrategyResult:
        if manifest is None or not manifest.files:
            return StrategyResult(
                success=True,
                deploy_type=self.deploy_type,
                error="",
            )

        roots: set[Path] = set()
        mod_path_raw = str(ctx.config.mod_path or "").strip()
        if mod_path_raw:
            roots.add(Path(mod_path_raw).expanduser().resolve())

        errors: list[str] = []
        removed = 0
        for entry in manifest.files:
            target = Path(entry.target)
            try:
                if target.is_file():
                    target.unlink()
                    removed += 1
                elif target.exists() and not target.is_dir():
                    target.unlink()
                    removed += 1
            except OSError as exc:
                errors.append(f"{target}: {exc}")
                continue
            # prune empty dirs under mod_path only
            stop = None
            for root in roots:
                try:
                    if target.resolve().is_relative_to(root):
                        stop = root
                        break
                except (ValueError, OSError):
                    continue
            if stop is not None:
                remove_empty_parents(target, stop_at=stop)

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
