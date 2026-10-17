"""Generic pak deploy: copy ``*.pak`` flat into ``game.mod_path``."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from services.deploy_rules.base import DeployContext, DeployStrategy, StrategyResult, is_rel_path_allowed, inert_strategy_deploy
from services.deploy_rules.generic import FolderCopyStrategy
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    remove_empty_parents,
)
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME
from services.importers.local_scanner import is_skipped_mod_path_part

logger = logging.getLogger(__name__)

_IGNORE_DIR_NAMES = frozenset({INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"})
ENTRY_TYPE_PAK = "pak"
ENTRY_TYPE_FOLDER = "folder_copy"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iter_suffix_payload_files(
    source: Path,
    *,
    suffix: str,
    allowed_rel_paths: frozenset[str] | None = None,
) -> list[Path]:
    """Recursively list payload files with *suffix* (case-insensitive).

    Skips ``.info`` / ``info`` / ``历史版本`` and other ignored path parts.
    """
    from services.deploy_fs import safe_iter_files

    source = source.resolve()
    needle = str(suffix or "").strip()
    if not needle.startswith("."):
        needle = f".{needle}"
    out: list[Path] = []
    for path in sorted(safe_iter_files(source, suffix=needle)):
        if path.name.startswith("."):
            continue
        try:
            rel_parts = path.relative_to(source).parts
        except ValueError:
            continue
        if any(is_skipped_mod_path_part(part) for part in rel_parts):
            continue
        if any(part in _IGNORE_DIR_NAMES for part in rel_parts):
            continue
        if not is_rel_path_allowed(source, path, allowed_rel_paths):
            continue
        out.append(path)
    return out


def _iter_pak_files(
    source: Path,
    *,
    allowed_rel_paths: frozenset[str] | None = None,
) -> list[Path]:
    return iter_suffix_payload_files(
        source, suffix=".pak", allowed_rel_paths=allowed_rel_paths
    )


def content_has_pak_files(ctx: DeployContext) -> bool:
    """True when deploy content includes at least one ``*.pak`` file."""
    return bool(_iter_pak_files(ctx.content_root(), allowed_rel_paths=ctx.allowed_rel_paths))


def _is_pak_relative(rel_parts: tuple[str, ...]) -> bool:
    return bool(rel_parts) and rel_parts[-1].lower().endswith(".pak")


class PakModPathStrategy(DeployStrategy):
    """
    Content-based pak deployment for ``folder_copy`` games:

    1. Every ``*.pak`` under content → ``<game.mod_path>/<name>.pak`` (flat)
    2. Remaining non-pak files → generic ``folder_copy`` when configured
    """

    deploy_type = "pak_mod_path"

    def __init__(self) -> None:
        self._folder = FolderCopyStrategy()

    def _mod_path(self, ctx: DeployContext) -> Path | StrategyResult:
        mod_path_raw = str(ctx.config.mod_path or "").strip()
        if not mod_path_raw:
            return StrategyResult(
                success=False,
                error="请先配置游戏部署目录",
                deploy_type=self.deploy_type,
            )
        return Path(mod_path_raw).expanduser()

    def _collect_pak_entries(
        self, ctx: DeployContext, mod_path: Path
    ) -> list[ManifestFileEntry]:
        source = ctx.content_root().resolve()
        entries: list[ManifestFileEntry] = []
        seen: set[str] = set()
        for path in _iter_pak_files(source, allowed_rel_paths=ctx.allowed_rel_paths):
            key = path.name.lower()
            if key in seen:
                continue
            seen.add(key)
            dst = (mod_path / path.name).resolve()
            entries.append(
                ManifestFileEntry(
                    source=str(path.resolve()),
                    target=str(dst),
                    type=ENTRY_TYPE_PAK,
                )
            )
        return entries

    def _folder_entries_excluding_paks(self, ctx: DeployContext) -> StrategyResult:
        planned = self._folder.plan(ctx)
        if not planned.success:
            return planned
        source = ctx.content_root().resolve()
        filtered: list[ManifestFileEntry] = []
        for entry in planned.files:
            src = Path(entry.source)
            try:
                rel_parts = src.resolve().relative_to(source).parts
            except ValueError:
                continue
            if _is_pak_relative(rel_parts):
                continue
            filtered.append(
                ManifestFileEntry(
                    source=entry.source,
                    target=entry.target,
                    type=ENTRY_TYPE_FOLDER,
                )
            )
        return StrategyResult(
            success=True,
            target=planned.target,
            copied_files=len(filtered),
            deploy_type=self.deploy_type,
            files=filtered,
        )

    def plan(self, ctx: DeployContext) -> StrategyResult:
        mod_path = self._mod_path(ctx)
        if isinstance(mod_path, StrategyResult):
            return mod_path

        entries = self._collect_pak_entries(ctx, mod_path)
        if not entries:
            return StrategyResult(
                success=False,
                error="没有可部署的 pak 文件",
                deploy_type=self.deploy_type,
            )

        folder_plan = self._folder_entries_excluding_paks(ctx)
        if not folder_plan.success:
            return folder_plan
        entries.extend(folder_plan.files)

        return StrategyResult(
            success=True,
            target=str(mod_path.resolve()),
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

        mod_path_raw = str(ctx.config.mod_path or "").strip()
        stop_roots: list[Path] = []
        if mod_path_raw:
            try:
                stop_roots.append(Path(mod_path_raw).expanduser().resolve())
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
