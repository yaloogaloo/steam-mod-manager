"""Generic folder_copy deploy strategy (copytree into game.mod_path)."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from services.deploy_rules.base import DeployContext, DeployStrategy, StrategyResult, is_rel_path_allowed
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    remove_empty_parents,
)
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

logger = logging.getLogger(__name__)

_IGNORE_DIR_NAMES = frozenset({INFO_DIR_NAME, LEGACY_INFO_DIR_NAME})


def _deploy_ignore(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in _IGNORE_DIR_NAMES}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _iter_deployable_files(
    source: Path,
    *,
    allowed_rel_paths: frozenset[str] | None = None,
) -> list[Path]:
    files: list[Path] = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
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
        target = (mod_path / ctx.source.name).resolve()
        source = ctx.source.resolve()

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
        planned = self.plan(ctx)
        if not planned.success:
            return planned

        mod_path = Path(str(ctx.config.mod_path).strip()).expanduser()
        try:
            mod_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            err = str(exc).lower()
            if "permission" in err or "denied" in err or "拒绝" in str(exc):
                msg = f"Permission denied：无法创建 Mod 部署目录（{exc}）"
            else:
                msg = f"无法创建 Mod 部署目录：{mod_path}（{exc}）"
            return StrategyResult(success=False, error=msg)

        target = Path(planned.target)
        source = ctx.source.resolve()

        # Legacy: full tree copy when no mod_files allow-list.
        # Multi-file Mods: copy only enabled entries.
        if ctx.allowed_rel_paths is None:
            try:
                shutil.copytree(
                    source,
                    target,
                    dirs_exist_ok=True,
                    ignore=_deploy_ignore,
                )
            except OSError as exc:
                err = str(exc).lower()
                if "permission" in err or "denied" in err:
                    return StrategyResult(
                        success=False, error=f"Permission denied：{exc}"
                    )
                return StrategyResult(success=False, error=f"复制失败：{exc}")
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
                            success=False, error=f"Permission denied：{exc}"
                        )
                    return StrategyResult(success=False, error=f"复制失败：{exc}")

        for banned in _IGNORE_DIR_NAMES:
            if (target / banned).exists():
                return StrategyResult(
                    success=False,
                    error=f"部署异常：目标不应包含 {banned}",
                )

        when = _utc_now()
        manifest = DeployManifest(
            mod_id=ctx.mod_id,
            deploy_time=when,
            deploy_type=self.deploy_type,
            files=list(planned.files),
        )
        return StrategyResult(
            success=True,
            target=str(target),
            copied_files=len(planned.files),
            deploy_type=self.deploy_type,
            deploy_time=when,
            files=list(planned.files),
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
