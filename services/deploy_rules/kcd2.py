"""Kingdom Come: Deliverance II — deploy the directory that holds mod.manifest."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from services.deploy_rules.base import DeployContext, DeployStrategy, StrategyResult, inert_strategy_deploy
from services.deploy_rules.generic import deploy_wrapper_folder
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    remove_empty_parents,
)
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME
from services.importers.local_scanner import is_skipped_mod_path_part

logger = logging.getLogger(__name__)

KCD2_APP_ID = 1771300
DEPLOY_TYPE_KCD2 = "kcd2_mod_manifest"
MANIFEST_NAME = "mod.manifest"

_IGNORE_DIR_NAMES = frozenset({INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"})
_STAGING_DIR = re.compile(r"^(?:ex_|deploy_)[0-9a-f]{6,}$|^content$", re.IGNORECASE)

_MISSING_MOD_PATH = "请先配置游戏部署目录"
_MISSING_MANIFEST = "天国拯救Ⅱ部署失败：未找到 mod.manifest"
_AMBIGUOUS_MANIFEST = (
    "天国拯救Ⅱ部署失败：无法唯一确定包含 mod.manifest 的 Mod 根目录"
)


def _path_skipped(rel_parts: tuple[str, ...]) -> bool:
    return any(
        part in _IGNORE_DIR_NAMES or is_skipped_mod_path_part(part)
        for part in rel_parts
    )


def _search_roots(ctx: DeployContext) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()

    def _add(raw: Path | str) -> None:
        path = Path(raw)
        try:
            path = path.resolve()
        except OSError:
            return
        if path in seen or not path.is_dir():
            return
        seen.add(path)
        roots.append(path)

    _add(ctx.content_root())
    for raw in getattr(ctx, "extract_overlay_roots", ()) or ():
        _add(raw)
    return roots


def _manifest_paths(roots: list[Path]) -> list[Path]:
    from services.deploy_fs import safe_iter_files

    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for path in safe_iter_files(root, name=MANIFEST_NAME):
            try:
                rel_parts = path.resolve().relative_to(root).parts
            except (OSError, ValueError):
                continue
            if _path_skipped(rel_parts):
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(resolved)
    return found


def _contains(ancestor: Path, other: Path) -> bool:
    if other == ancestor:
        return True
    try:
        other.relative_to(ancestor)
    except ValueError:
        return False
    return True


def unique_manifest_root(manifests: list[Path]) -> Path | None:
    """Directory that contains a manifest and every other manifest.

    Sibling Mod roots are not a single root. Returns None when the set is
    empty or more than one directory could be the root.
    """
    parents: list[Path] = []
    for manifest in manifests:
        parent = manifest.parent.resolve()
        if parent not in parents:
            parents.append(parent)
    if not parents:
        return None
    candidates = [
        parent
        for parent in parents
        if all(_contains(parent, other) for other in parents)
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]


def _is_extract_staging(path: Path) -> bool:
    try:
        from services.importers.archive import import_cache_root

        path.resolve().relative_to(import_cache_root().resolve())
    except (OSError, ValueError):
        return False
    return _STAGING_DIR.match(path.name) is not None


def kcd2_deploy_dirname(mod_root: Path, ctx: DeployContext) -> str:
    """Name under ``game.mod_path``.

    A real content directory keeps its own name. An extract staging bucket
    (``ex_*`` / ``deploy_*`` / ``content``) has no package name, so the
    existing library-folder rule applies: ASCII basename stays, a Han
    basename becomes ``mod_{workspace_id}``.
    """
    root = mod_root.resolve()
    library = ctx.library_folder().resolve()
    if root == library or _is_extract_staging(root):
        return deploy_wrapper_folder(library.name, ctx.workspace_id)
    return root.name


def _payload_files(
    mod_root: Path,
    roots: list[Path],
    *,
    skip_archives: bool,
) -> list[Path]:
    from services.deploy_fs import safe_iter_files
    from services.importers.archive import is_archive_path

    mod_root = mod_root.resolve()
    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for path in safe_iter_files(root):
            try:
                resolved = path.resolve()
                rel = resolved.relative_to(mod_root)
            except (OSError, ValueError):
                continue
            if _path_skipped(rel.parts):
                continue
            if skip_archives and is_archive_path(resolved.name):
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(resolved)
    return files


class KingdomCome2Strategy(DeployStrategy):
    """Deploy ``<Mods>/<directory that contains mod.manifest>/``."""

    deploy_type = DEPLOY_TYPE_KCD2

    def plan(self, ctx: DeployContext) -> StrategyResult:
        raw = str(ctx.config.mod_path or "").strip()
        if not raw:
            return StrategyResult(
                success=False,
                error=_MISSING_MOD_PATH,
                deploy_type=self.deploy_type,
            )
        mods = Path(raw).expanduser()
        roots = _search_roots(ctx)
        manifests = _manifest_paths(roots)
        if not manifests:
            return StrategyResult(
                success=False,
                error=_MISSING_MANIFEST,
                deploy_type=self.deploy_type,
            )
        mod_root = unique_manifest_root(manifests)
        if mod_root is None:
            return StrategyResult(
                success=False,
                error=_AMBIGUOUS_MANIFEST,
                deploy_type=self.deploy_type,
            )
        try:
            dirname = kcd2_deploy_dirname(mod_root, ctx)
        except ValueError:
            return StrategyResult(
                success=False,
                error="无法生成部署目录：缺少 Workspace ID",
                deploy_type=self.deploy_type,
            )
        if not dirname or dirname in {".", ".."}:
            return StrategyResult(
                success=False,
                error=_AMBIGUOUS_MANIFEST,
                deploy_type=self.deploy_type,
            )
        dest = (mods / dirname).resolve()
        files = _payload_files(
            mod_root,
            roots,
            skip_archives=bool(getattr(ctx, "extract_overlay_roots", ()) or ()),
        )
        if not files:
            return StrategyResult(
                success=False,
                error=_MISSING_MANIFEST,
                deploy_type=self.deploy_type,
            )
        entries = [
            ManifestFileEntry(
                source=str(src),
                target=str((dest / src.relative_to(mod_root)).resolve()),
                source_relative=src.relative_to(mod_root).as_posix(),
                relative=src.relative_to(mod_root).as_posix(),
                type=DEPLOY_TYPE_KCD2,
            )
            for src in files
        ]
        return StrategyResult(
            success=True,
            target=str(dest),
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
            for stop_at in stop_roots:
                try:
                    if target.resolve().is_relative_to(stop_at):
                        remove_empty_parents(target, stop_at=stop_at)
                        break
                except (OSError, ValueError):
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
