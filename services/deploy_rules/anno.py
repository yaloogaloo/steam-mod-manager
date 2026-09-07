"""Anno 1800 path-mapping adapter: folders → mods/<name>/; archives → mods/.

Blueprint (stamps) Mods map into ``Documents/Anno 1800/stamps``.

Phase 3: ``plan()`` only builds FilePlan mappings (archive members via listing,
never ArchiveExtractor). ``deploy()`` is inert — Core Apply owns I/O.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from core.db_manager import GameDeployConfig
from services.deploy_archive_members import (
    archive_contains_prefix,
    iter_archive_members,
)
from services.deploy_rules.base import (
    DeployContext,
    DeployStrategy,
    StrategyResult,
    inert_strategy_deploy,
)
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


def _is_blueprint_category(ctx: DeployContext) -> bool:
    meta = read_info_metadata_dict(ctx.library_folder()) or {}
    cat = str(meta.get("category") or "").strip().lower()
    return cat in {c.lower() for c in _BLUEPRINT_CATEGORIES}


def _find_stamps_dir(root: Path) -> Path | None:
    """Locate a ``stamps`` directory under *root* (skip ``.info`` trees)."""
    from services.deploy_fs import safe_iter_dirs

    if not root.is_dir():
        return None
    direct = root / STAMPS_DIR_NAME
    if direct.is_dir():
        return direct
    for path in safe_iter_dirs(root, name=STAMPS_DIR_NAME):
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            parts = path.parts
        if any(part in _IGNORE_DIR_NAMES for part in parts):
            continue
        return path
    return None


def _iter_files_under(directory: Path) -> list[Path]:
    from services.deploy_fs import safe_iter_files

    if not directory.is_dir():
        return []
    return sorted(safe_iter_files(directory))


def _plan_stamps_entries(
    stamps_src: Path,
    stamps_dst: Path,
    *,
    source_label: str,
) -> list[ManifestFileEntry]:
    entries: list[ManifestFileEntry] = []
    for src in _iter_files_under(stamps_src):
        rel = src.relative_to(stamps_src).as_posix()
        entries.append(
            ManifestFileEntry(
                source=str(src.resolve()),
                target=str((stamps_dst / rel).resolve()),
                type=ENTRY_TYPE_STAMPS,
                source_relative=rel,
                relative=rel,
            )
        )
    return entries


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


def _plan_stamps_from_archives(archives: list[Path]) -> StrategyResult:
    """Map archive members under ``stamps/`` → Documents stamps root (no extract)."""
    stamps_dst = resolve_anno_stamps_dir()
    entries: list[ManifestFileEntry] = []
    try:
        for archive in archives:
            for member in iter_archive_members(archive):
                parts = member.replace("\\", "/").split("/")
                if STAMPS_DIR_NAME not in parts:
                    continue
                idx = parts.index(STAMPS_DIR_NAME)
                under = "/".join(parts[idx + 1 :])
                if not under:
                    continue
                entries.append(
                    ManifestFileEntry(
                        source=str(archive.resolve()),
                        target=str((stamps_dst / under).resolve()),
                        type=ENTRY_TYPE_STAMPS,
                        source_relative=member,
                        relative=under,
                    )
                )
    except (OSError, RuntimeError, FileNotFoundError) as exc:
        return StrategyResult(
            success=False,
            error=f"无法枚举压缩包成员：{exc}",
            deploy_type=_ANNO_DEPLOY_TYPE,
        )
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


def _looks_like_stamps_mod(ctx: DeployContext, archives: list[Path]) -> bool:
    """True when category is 蓝图, or a stamps folder exists under the Mod tree."""
    if _is_blueprint_category(ctx):
        return True
    content = ctx.content_root()
    if _find_stamps_dir(content) is not None:
        return True
    library = ctx.library_folder()
    try:
        if library.resolve() != content.resolve() and _find_stamps_dir(library) is not None:
            return True
    except OSError:
        pass
    return False


def _archives_have_stamps(archives: list[Path]) -> bool:
    for archive in archives:
        try:
            if archive_contains_prefix(archive, STAMPS_DIR_NAME):
                return True
            # nested: any/.../stamps/...
            for member in iter_archive_members(archive):
                parts = member.replace("\\", "/").split("/")
                if STAMPS_DIR_NAME in parts:
                    return True
        except (OSError, RuntimeError, FileNotFoundError):
            continue
    return False


def _plan_anno_archive_deploy(
    ctx: DeployContext,
    mods_root: Path,
    archives: list[Path],
) -> StrategyResult:
    """Map archive members → ``mods/<member>`` without extracting."""
    entries: list[ManifestFileEntry] = []
    try:
        for archive in archives:
            for member in iter_archive_members(archive):
                entries.append(
                    ManifestFileEntry(
                        source=str(archive.resolve()),
                        target=str((mods_root / member).resolve()),
                        type="archive",
                        source_relative=member,
                        relative=member,
                    )
                )
    except (OSError, RuntimeError, FileNotFoundError) as exc:
        return StrategyResult(
            success=False,
            error=f"无法枚举压缩包成员：{exc}",
            deploy_type=_ANNO_DEPLOY_TYPE,
        )
    if not entries:
        return StrategyResult(
            success=False,
            error="压缩包中没有可部署的文件",
            deploy_type=_ANNO_DEPLOY_TYPE,
        )
    return StrategyResult(
        success=True,
        target=str(mods_root.resolve()),
        copied_files=len(entries),
        deploy_type=_ANNO_DEPLOY_TYPE,
        files=entries,
    )


class Anno1800Strategy(DeployStrategy):
    """
    Anno 1800 path-mapping adapter.

    - Blueprint / stamps: map into ``Documents/Anno 1800/stamps``.
    - Archive mods: map zip members into ``<install>/mods/``.
    - Loose directory mods: ``mods/<managed_folder_name>/`` via folder plan.
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
                content_fingerprint=man.content_fingerprint,
                source_path=man.source_path,
                internal_id=man.internal_id or man.mod_id,
                schema_version=man.schema_version,
            )
        return result

    def _archive_paths(self, ctx: DeployContext) -> list[Path]:
        from services.deploy import collect_deploy_archives

        return collect_deploy_archives(ctx.internal_id, ctx.library_folder())

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
        if archives and _archives_have_stamps(archives):
            return self._retag(_plan_stamps_from_archives(archives))
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
        """Inert — Core Apply consumes ``plan()`` FilePlan entries."""
        return inert_strategy_deploy(self.deploy_type)

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
