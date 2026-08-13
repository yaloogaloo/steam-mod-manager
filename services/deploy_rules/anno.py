"""Anno 1800 deploy: loose folders → ``mods/<name>/``; archives → extract into ``mods/``.

Blueprint (stamps) Mods merge into ``Documents/Anno 1800/stamps``.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from core.db_manager import GameDeployConfig
from services.deploy_rules.base import DeployContext, DeployStrategy, StrategyResult
from services.deploy_rules.generic import FolderCopyStrategy
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    remove_empty_parents,
)
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, read_info_metadata_dict

logger = logging.getLogger(__name__)

ANNO_1800_APP_ID = 916440
MODS_DIR_NAME = "mods"
STAMPS_DIR_NAME = "stamps"
ANNO_DOCS_DIR_NAME = "Anno 1800"
ENTRY_TYPE_STAMPS = "stamps"
_ANNO_DEPLOY_TYPE = "anno_1800"
_IGNORE_DIR_NAMES = frozenset({INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"})
_BLUEPRINT_CATEGORIES = frozenset(
    {"蓝图", "stamps", "stamp", "blueprint", "blueprints"}
)


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


def resolve_anno_stamps_dir() -> Path:
    """``~/Documents/Anno 1800/stamps`` — never hard-code a username."""
    docs_path = Path.home() / "Documents"
    anno_docs_dir = docs_path / ANNO_DOCS_DIR_NAME
    return anno_docs_dir / STAMPS_DIR_NAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _is_blueprint_category(ctx: DeployContext) -> bool:
    meta = read_info_metadata_dict(ctx.library_folder()) or {}
    cat = str(meta.get("category") or "").strip().lower()
    return cat in {c.lower() for c in _BLUEPRINT_CATEGORIES}


def _find_stamps_dir(root: Path) -> Path | None:
    """Locate a ``stamps`` directory under *root* (skip ``.info`` trees)."""
    if not root.is_dir():
        return None
    direct = root / STAMPS_DIR_NAME
    if direct.is_dir():
        return direct
    for path in root.rglob(STAMPS_DIR_NAME):
        if not path.is_dir():
            continue
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            parts = path.parts
        if any(part in _IGNORE_DIR_NAMES for part in parts):
            continue
        return path
    return None


def _iter_files_under(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.rglob("*") if p.is_file())


def _copy_stamps_tree(
    stamps_src: Path,
    stamps_dst: Path,
    *,
    source_label: str,
) -> list[ManifestFileEntry]:
    """Merge-copy *stamps_src* contents into *stamps_dst* (overwrite same names)."""
    os.makedirs(stamps_dst, exist_ok=True)
    entries: list[ManifestFileEntry] = []
    for src in _iter_files_under(stamps_src):
        rel = src.relative_to(stamps_src)
        dst = (stamps_dst / rel).resolve()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        entries.append(
            ManifestFileEntry(
                source=source_label,
                target=str(dst),
                type=ENTRY_TYPE_STAMPS,
            )
        )
    return entries


def _plan_stamps_entries(
    stamps_src: Path,
    stamps_dst: Path,
    *,
    source_label: str,
) -> list[ManifestFileEntry]:
    return [
        ManifestFileEntry(
            source=source_label,
            target=str((stamps_dst / src.relative_to(stamps_src)).resolve()),
            type=ENTRY_TYPE_STAMPS,
        )
        for src in _iter_files_under(stamps_src)
    ]


def _deploy_stamps_from_directory(
    ctx: DeployContext,
    content_root: Path,
    *,
    source_label: str | None = None,
) -> StrategyResult:
    stamps_src = _find_stamps_dir(content_root)
    if stamps_src is None:
        return StrategyResult(
            success=False,
            error="蓝图 Mod 中未找到 stamps 目录",
            deploy_type=_ANNO_DEPLOY_TYPE,
        )
    stamps_dst = resolve_anno_stamps_dir()
    label = source_label or str(content_root)
    try:
        entries = _copy_stamps_tree(stamps_src, stamps_dst, source_label=label)
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
            error=f"蓝图拷贝失败：{exc}",
            deploy_type=_ANNO_DEPLOY_TYPE,
        )
    if not entries:
        return StrategyResult(
            success=False,
            error="stamps 目录中没有可部署的文件",
            deploy_type=_ANNO_DEPLOY_TYPE,
        )
    when = _utc_now()
    manifest = DeployManifest(
        mod_id=ctx.mod_id,
        deploy_time=when,
        deploy_type=_ANNO_DEPLOY_TYPE,
        files=entries,
    )
    logger.info(
        "[DEPLOY] anno stamps mod_id=%s files=%s target=%s",
        ctx.mod_id,
        len(entries),
        stamps_dst,
    )
    return StrategyResult(
        success=True,
        target=str(stamps_dst.resolve()),
        copied_files=len(entries),
        deploy_type=_ANNO_DEPLOY_TYPE,
        deploy_time=when,
        files=entries,
        manifest=manifest,
    )


def _plan_stamps_from_directory(
    content_root: Path,
    *,
    source_label: str | None = None,
) -> StrategyResult:
    stamps_src = _find_stamps_dir(content_root)
    if stamps_src is None:
        return StrategyResult(
            success=False,
            error="蓝图 Mod 中未找到 stamps 目录",
            deploy_type=_ANNO_DEPLOY_TYPE,
        )
    stamps_dst = resolve_anno_stamps_dir()
    label = source_label or str(content_root)
    entries = _plan_stamps_entries(stamps_src, stamps_dst, source_label=label)
    if not entries:
        return StrategyResult(
            success=False,
            error="stamps 目录中没有可部署的文件",
            deploy_type=_ANNO_DEPLOY_TYPE,
        )
    return StrategyResult(
        success=True,
        target=str(stamps_dst.resolve()),
        copied_files=len(entries),
        deploy_type=_ANNO_DEPLOY_TYPE,
        files=entries,
    )


def _extract_archives_to_stage(archives: list[Path]) -> Path:
    from services.importers.archive import extract_archive, import_cache_root

    stage = import_cache_root() / f"anno_stamps_{uuid.uuid4().hex}"
    stage.mkdir(parents=True, exist_ok=False)
    for archive in archives:
        extract_archive(archive, dest_dir=stage)
    return stage


def _deploy_stamps_from_archives(
    ctx: DeployContext,
    archives: list[Path],
) -> StrategyResult:
    from services.importers.archive import cleanup_import_cache

    stage: Path | None = None
    try:
        stage = _extract_archives_to_stage(archives)
        label = str(archives[0]) if len(archives) == 1 else str(ctx.library_folder())
        return _deploy_stamps_from_directory(ctx, stage, source_label=label)
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
        if stage is not None:
            cleanup_import_cache(stage)


def _plan_stamps_from_archives(archives: list[Path]) -> StrategyResult:
    from services.importers.archive import cleanup_import_cache

    stage: Path | None = None
    try:
        stage = _extract_archives_to_stage(archives)
        label = str(archives[0])
        return _plan_stamps_from_directory(stage, source_label=label)
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
        if stage is not None:
            cleanup_import_cache(stage)


def _looks_like_stamps_mod(ctx: DeployContext, archives: list[Path]) -> bool:
    """True when category is 蓝图, or a stamps folder exists under the Mod tree."""
    if _is_blueprint_category(ctx):
        return True
    content = ctx.content_root()
    if _find_stamps_dir(content) is not None:
        return True
    library = ctx.library_folder()
    if library.resolve() != content.resolve() and _find_stamps_dir(library) is not None:
        return True
    return False


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

    - Blueprint / stamps Mods: merge into ``Documents/Anno 1800/stamps``.
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

    def _try_stamps_plan(
        self, ctx: DeployContext, archives: list[Path]
    ) -> StrategyResult | None:
        """Return stamps plan when this Mod is a blueprint; else None."""
        loose_root = ctx.content_root()
        if _find_stamps_dir(loose_root) is None:
            lib = ctx.library_folder()
            if _find_stamps_dir(lib) is not None:
                loose_root = lib
        known = _looks_like_stamps_mod(ctx, archives)
        if known and _find_stamps_dir(loose_root) is not None:
            return self._retag(
                _plan_stamps_from_directory(
                    loose_root, source_label=str(ctx.library_folder())
                )
            )
        if known and archives:
            return self._retag(_plan_stamps_from_archives(archives))
        if archives:
            # Peek inside archives for a stamps/ tree (no category required).
            from services.importers.archive import cleanup_import_cache

            stage: Path | None = None
            try:
                stage = _extract_archives_to_stage(archives)
                if _find_stamps_dir(stage) is not None:
                    return self._retag(
                        _plan_stamps_from_directory(
                            stage, source_label=str(archives[0])
                        )
                    )
            except OSError:
                return None
            finally:
                if stage is not None:
                    cleanup_import_cache(stage)
        return None

    def _try_stamps_deploy(
        self, ctx: DeployContext, archives: list[Path]
    ) -> StrategyResult | None:
        """Return stamps deploy result when this Mod is a blueprint; else None."""
        loose_root = ctx.content_root()
        if _find_stamps_dir(loose_root) is None:
            lib = ctx.library_folder()
            if _find_stamps_dir(lib) is not None:
                loose_root = lib
        known = _looks_like_stamps_mod(ctx, archives)
        if known and _find_stamps_dir(loose_root) is not None:
            return self._retag(
                _deploy_stamps_from_directory(
                    ctx, loose_root, source_label=str(ctx.library_folder())
                )
            )
        if known and archives:
            return self._retag(_deploy_stamps_from_archives(ctx, archives))
        if archives:
            from services.importers.archive import cleanup_import_cache

            stage: Path | None = None
            try:
                stage = _extract_archives_to_stage(archives)
                if _find_stamps_dir(stage) is not None:
                    label = (
                        str(archives[0])
                        if len(archives) == 1
                        else str(ctx.library_folder())
                    )
                    result = _deploy_stamps_from_directory(
                        ctx, stage, source_label=label
                    )
                    return self._retag(result)
            except OSError as exc:
                err = str(exc).lower()
                if "permission" in err or "denied" in err:
                    msg = f"Permission denied：{exc}"
                else:
                    msg = f"解压失败：{exc}"
                return StrategyResult(
                    success=False, error=msg, deploy_type=self.deploy_type
                )
            finally:
                if stage is not None:
                    cleanup_import_cache(stage)
        return None

    def plan(self, ctx: DeployContext) -> StrategyResult:
        archives = self._archive_paths(ctx)
        stamps = self._try_stamps_plan(ctx, archives)
        if stamps is not None:
            return stamps

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
        archives = self._archive_paths(ctx)
        stamps = self._try_stamps_deploy(ctx, archives)
        if stamps is not None:
            return stamps

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
        if manifest is None or not manifest.files:
            return StrategyResult(success=True, deploy_type=self.deploy_type)

        stamps_root = resolve_anno_stamps_dir().resolve()
        stop_roots: list[Path] = [stamps_root]
        mods_root = resolve_anno_mods_root(ctx.config)
        if mods_root is not None:
            try:
                stop_roots.append(mods_root.expanduser().resolve())
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