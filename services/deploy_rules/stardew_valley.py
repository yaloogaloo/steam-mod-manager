"""Stardew Valley deploy: SMAPI mods → configured Mods dir via ``manifest.json``."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

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
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME
from services.importers.local_scanner import is_skipped_mod_path_part

logger = logging.getLogger(__name__)

STARDEW_VALLEY_APP_ID = 413150
SMAPI_MANIFEST_NAME = "manifest.json"
ENTRY_TYPE_SMAPI = "smapi_mod"

_IGNORE_DIR_NAMES = frozenset({INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _mods_root(ctx: DeployContext) -> Path | StrategyResult:
    """Resolve configured Mods directory (``game.mod_path``)."""
    raw = str(ctx.config.mod_path or "").strip()
    if not raw:
        return StrategyResult(
            success=False,
            error="请先配置游戏部署目录（星露谷物语需要 Mods 路径）",
            deploy_type=StardewValleyStrategy.deploy_type,
        )
    return Path(raw).expanduser()


def _path_skipped(rel_parts: tuple[str, ...]) -> bool:
    return any(
        part in _IGNORE_DIR_NAMES or is_skipped_mod_path_part(part)
        for part in rel_parts
    )


def find_smapi_mod_roots(content_root: Path) -> list[Path]:
    """
    Recursively find directories that contain ``manifest.json``.

    Each such directory is treated as one SMAPI Mod root.
    """
    root = Path(content_root).resolve()
    if not root.is_dir():
        return []
    from services.deploy_fs import safe_iter_files

    found: list[Path] = []
    seen: set[Path] = set()
    for manifest in sorted(safe_iter_files(root, name=SMAPI_MANIFEST_NAME)):
        try:
            rel_parts = manifest.relative_to(root).parts
        except ValueError:
            continue
        if _path_skipped(rel_parts):
            continue
        mod_root = manifest.parent.resolve()
        if mod_root in seen:
            continue
        seen.add(mod_root)
        found.append(mod_root)
    return found


def _flat_mod_dirname(ctx: DeployContext) -> str:
    """
    Name for a flat archive (manifest at content root).

    Prefer the single archive stem under the managed folder; otherwise the
    managed library folder name.
    """
    managed = ctx.library_folder()
    try:
        from services.importers.archive import is_archive_path

        archives = [
            p
            for p in sorted(managed.iterdir())
            if p.is_file() and is_archive_path(p)
        ]
    except OSError:
        archives = []
    if len(archives) == 1:
        return archives[0].stem
    return managed.name


def _target_dirname(mod_root: Path, content_root: Path, ctx: DeployContext) -> str:
    try:
        if mod_root.resolve() == content_root.resolve():
            return _flat_mod_dirname(ctx)
    except OSError:
        pass
    return mod_root.name


def _iter_mod_files(
    mod_root: Path,
    *,
    content_root: Path,
    allowed_rel_paths: frozenset[str] | None,
) -> list[Path]:
    from services.deploy_fs import safe_iter_files

    files: list[Path] = []
    for path in sorted(safe_iter_files(mod_root)):
        try:
            rel_parts = path.relative_to(content_root).parts
        except ValueError:
            try:
                rel_parts = path.relative_to(mod_root).parts
            except ValueError:
                continue
        if _path_skipped(rel_parts):
            continue
        if not is_rel_path_allowed(content_root, path, allowed_rel_paths):
            # Allow-list may be relative to content_root; also accept paths
            # under mod_root when the allow-list is unset (None).
            if allowed_rel_paths is not None:
                if not is_rel_path_allowed(mod_root, path, allowed_rel_paths):
                    continue
        files.append(path)
    return files


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


class StardewValleyStrategy(DeployStrategy):
    """
    Stardew Valley (AppID 413150):

    Deploy every SMAPI Mod root (directory containing ``manifest.json``) into
    ``game.mod_path/<ModDir>/``. Flat archives (manifest at extract root) use
    the archive / library folder name as ``<ModDir>``.
    """

    deploy_type = "stardew_valley"

    def plan(self, ctx: DeployContext) -> StrategyResult:
        mods = _mods_root(ctx)
        if isinstance(mods, StrategyResult):
            return mods

        content = ctx.content_root().resolve()
        mod_roots = find_smapi_mod_roots(content)
        if not mod_roots:
            return StrategyResult(
                success=False,
                error="未找到 SMAPI Mod（缺少 manifest.json）",
                deploy_type=self.deploy_type,
            )

        entries: list[ManifestFileEntry] = []
        target_dirs: list[str] = []
        for mod_root in mod_roots:
            dirname = _target_dirname(mod_root, content, ctx)
            if not dirname:
                continue
            dest_root = (mods / dirname).resolve()
            target_dirs.append(str(dest_root))
            for src in _iter_mod_files(
                mod_root,
                content_root=content,
                allowed_rel_paths=ctx.allowed_rel_paths,
            ):
                rel = src.relative_to(mod_root)
                entries.append(
                    ManifestFileEntry(
                        source=str(src),
                        target=str((dest_root / rel).resolve()),
                        type=ENTRY_TYPE_SMAPI,
                    )
                )

        if not entries:
            return StrategyResult(
                success=False,
                error="没有可部署的文件",
                deploy_type=self.deploy_type,
            )
        return StrategyResult(
            success=True,
            target=target_dirs[0] if len(target_dirs) == 1 else str(mods.resolve()),
            copied_files=len(entries),
            deploy_type=self.deploy_type,
            files=entries,
        )

    def deploy(self, ctx: DeployContext) -> StrategyResult:
        planned = self.plan(ctx)
        if not planned.success:
            return planned

        mods = _mods_root(ctx)
        if isinstance(mods, StrategyResult):
            return mods

        try:
            mods.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            err = str(exc).lower()
            if "permission" in err or "denied" in err:
                return StrategyResult(
                    success=False,
                    error=f"Permission denied：无法创建 Mods 目录（{exc}）",
                    deploy_type=self.deploy_type,
                )
            return StrategyResult(
                success=False,
                error=f"无法创建 Mods 目录：{mods}（{exc}）",
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
                    type=entry.type or ENTRY_TYPE_SMAPI,
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
            "[DEPLOY] stardew_valley mod_id=%s files=%s target=%s",
            ctx.mod_id,
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
        raw = str(ctx.config.mod_path or "").strip()
        if raw:
            try:
                stop_roots.append(Path(raw).expanduser().resolve())
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
