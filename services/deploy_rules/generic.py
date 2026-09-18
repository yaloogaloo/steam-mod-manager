"""Generic folder_copy deploy strategy (copytree into game.mod_path)."""

from __future__ import annotations

import logging
import time
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


def contains_han_characters(folder_name: str) -> bool:
    """True when the Mod's own basename contains CJK Unified Ideographs.

    Uses ``Path(folder_name).name`` only — never parent path, game path,
    library root, or source URL.
    """
    from services.mod_path_normalizer import contains_chinese

    return contains_chinese(Path(str(folder_name or "")).name)


def deploy_folder_name(workspace_id: str) -> str:
    """Han-basename rename result: ``mod_{workspace_id}``.

    Never uses ``internal_id``, SQLite ``mod_pk``, pinyin, or ``uXXXX``.
    """
    ws = str(workspace_id or "").strip()
    if not ws:
        raise ValueError("deploy folder name requires workspace_id")
    cleaned = "".join("_" if ch in '<>:"/\\|?*' or ord(ch) < 32 else ch for ch in ws)
    cleaned = cleaned.strip(" .")
    if not cleaned:
        raise ValueError("deploy folder name requires workspace_id")
    return f"mod_{cleaned}"


def deploy_wrapper_folder(source_basename: str, workspace_id: str) -> str:
    """Default wrapper under ``game.mod_path``.

    ASCII / non-Han basename stays unchanged. Basename with Han becomes
    ``mod_{workspace_id}``. Parent-path Han is ignored.
    """
    name = Path(str(source_basename or "")).name
    if not contains_han_characters(name):
        return name
    return deploy_folder_name(workspace_id)


def _log_deploy_target(
    *,
    stage: str,
    source_folder: str,
    normalized_folder: str,
    final_target_path: Path | str,
) -> None:
    logger.info(
        "DEPLOY_TARGET:\n"
        "stage=%s\n"
        "source_folder=%s\n"
        "deploy_folder=%s\n"
        "final_target_path=%s",
        stage,
        source_folder,
        normalized_folder,
        final_target_path,
    )


def _deploy_folder_name(ctx: DeployContext) -> str:
    """Wrapper folder under ``game.mod_path`` — Han basename only is renamed."""
    return deploy_wrapper_folder(ctx.library_folder().name, ctx.workspace_id)


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


def iter_deploy_payload_files(ctx: DeployContext) -> list[tuple[Path, Path]]:
    """FilePlan sources: outer managed files plus extracted overlay.

    Extract overlay is applied first; a managed file at the same relative
    path wins. Archives on the managed tree are skipped when an overlay
    exists (they are extract units, not deploy files). Outer files are
    referenced in place — never copied into the extract staging tree.
    """
    source = ctx.content_root().resolve()
    overlays = [
        Path(root).resolve()
        for root in (getattr(ctx, "extract_overlay_roots", ()) or ())
        if root
    ]
    by_rel: dict[str, Path] = {}
    order: list[str] = []

    def _add(path: Path, root: Path, *, skip_archives: bool) -> None:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            return
        if skip_archives:
            from services.importers.archive import is_archive_path

            if is_archive_path(path.name):
                return
        if rel not in by_rel:
            order.append(rel)
        by_rel[rel] = path

    for root in overlays:
        for path in _iter_deployable_files(root, allowed_rel_paths=None):
            _add(path, root, skip_archives=False)
    for path in _iter_deployable_files(source, allowed_rel_paths=ctx.allowed_rel_paths):
        _add(path, source, skip_archives=bool(overlays))
    return [(by_rel[rel], Path(rel)) for rel in order]


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
        source_folder = ctx.library_folder().name
        try:
            folder_name = _deploy_folder_name(ctx)
        except ValueError:
            return StrategyResult(
                success=False,
                error="无法生成部署目录：缺少 Workspace ID",
            )
        target = (mod_path / folder_name).resolve()
        _log_deploy_target(
            stage="plan",
            source_folder=source_folder,
            normalized_folder=folder_name,
            final_target_path=target,
        )
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

        payload = iter_deploy_payload_files(ctx)
        from services.deploy_op_profile import note_tree, cached_resolve

        entries = []
        target_s = str(target)
        for src_file, rel in payload:
            rel_s = rel.as_posix()
            if ".." in rel.parts:
                target_path = cached_resolve(target / rel)
            else:
                target_path = str(target / rel)
            entries.append(
                ManifestFileEntry(
                    source=str(src_file),
                    target=target_path,
                    source_relative=rel_s,
                    relative=rel_s,
                )
            )
        note_tree(
            stage="plan",
            kind="source",
            root=str(source),
            file_count=len(entries),
        )
        note_tree(
            stage="plan",
            kind="target",
            root=target_s,
            file_count=len(entries),
        )
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
