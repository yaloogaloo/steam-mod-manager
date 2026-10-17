"""Total War: WARHAMMER III (AppID 1142710) — library activation, not copy-to-data."""

from __future__ import annotations

from pathlib import Path

from core.mod_platform import WARHAMMER3_APP_IDS
from services.deploy_rules.base import DeployContext, StrategyResult, inert_strategy_deploy
from services.deploy_rules.manifest import ManifestFileEntry
from services.deploy_rules.pak_mod_path import (
    PakModPathStrategy,
    iter_suffix_payload_files,
)

WARHAMMER3_APP_ID = next(iter(WARHAMMER3_APP_IDS))
DEPLOY_TYPE_WARHAMMER3 = "warhammer3_pack"
ENTRY_TYPE_PACK = "pack"

_MISSING_PACK = "战锤 III Mod 部署失败：未找到 .pack 文件"


def _collect_library_pack_entries(ctx: DeployContext) -> list[ManifestFileEntry]:
    """List ``*.pack`` under the managed library folder. Targets stay in-library."""
    source = ctx.content_root().resolve()
    entries: list[ManifestFileEntry] = []
    seen: set[str] = set()
    for path in iter_suffix_payload_files(
        source, suffix=".pack", allowed_rel_paths=ctx.allowed_rel_paths
    ):
        key = path.name.lower()
        if key in seen:
            continue
        seen.add(key)
        resolved = str(path.resolve())
        entries.append(
            ManifestFileEntry(
                source=resolved,
                target=resolved,
                type=ENTRY_TYPE_PACK,
            )
        )
    return entries


def _is_under(path: Path, root: Path) -> bool:
    try:
        return path == root or path.is_relative_to(root)
    except (ValueError, OSError):
        return False


def _library_has_archives(root: Path) -> bool:
    from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME
    from services.importers.archive import is_archive_path

    skip = {INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"}
    try:
        for path in root.iterdir():
            if path.name in skip or path.name.startswith("."):
                continue
            if path.is_file() and is_archive_path(path):
                return True
    except OSError:
        return False
    return False


class Warhammer3Strategy(PakModPathStrategy):
    """
    Warhammer III deploy mapping:

    Confirm local ``*.pack`` files or archives exist. Core Apply must not
    copy packs into ``game.mod_path``. Activation reads Workshop paths.
    """

    deploy_type = DEPLOY_TYPE_WARHAMMER3

    def plan(self, ctx: DeployContext) -> StrategyResult:
        library = ctx.library_folder().resolve()
        try:
            library_live = library.is_dir()
        except OSError:
            library_live = False
        if not library_live:
            from services.mod_presence import workshop_source_available

            if workshop_source_available(
                app_id=int(ctx.app_id or 0),
                workspace_id=str(ctx.workspace_id or ""),
                workshop_path=str(getattr(ctx.config, "workshop_path", "") or ""),
            ):
                return StrategyResult(
                    success=True,
                    target=str(library),
                    copied_files=0,
                    deploy_type=self.deploy_type,
                    files=[],
                )
            return StrategyResult(
                success=False,
                error=_MISSING_PACK,
                deploy_type=self.deploy_type,
            )
        entries = _collect_library_pack_entries(ctx)
        if not entries and not _library_has_archives(ctx.content_root()):
            return StrategyResult(
                success=False,
                error=_MISSING_PACK,
                deploy_type=self.deploy_type,
            )
        return StrategyResult(
            success=True,
            target=str(library),
            copied_files=0,
            deploy_type=self.deploy_type,
            files=entries,
        )

    def deploy(self, ctx: DeployContext) -> StrategyResult:
        """Inert — WH3 activation is owned by ModDeployer + used_mods.txt."""
        return inert_strategy_deploy(self.deploy_type)

    def undeploy(
        self,
        ctx: DeployContext,
        manifest: object | None,
    ) -> StrategyResult:
        """
        Never delete library packs.

        Old flatten manifests may still point at ``game.mod_path`` copies;
        those leftover data copies may be removed. Library files are skipped.
        """
        from services.deploy_rules.manifest import DeployManifest

        if manifest is None or not getattr(manifest, "files", None):
            return StrategyResult(success=True, deploy_type=self.deploy_type)
        if not isinstance(manifest, DeployManifest):
            return StrategyResult(success=True, deploy_type=self.deploy_type)

        library = ctx.library_folder().resolve()
        data_raw = str(ctx.config.mod_path or "").strip()
        data_root: Path | None = None
        if data_raw:
            try:
                data_root = Path(data_raw).expanduser().resolve()
            except OSError:
                data_root = None

        errors: list[str] = []
        removed = 0
        for entry in manifest.files:
            target = Path(str(entry.target or ""))
            try:
                resolved = target.resolve()
            except OSError:
                continue
            if _is_under(resolved, library):
                continue
            if data_root is None or not _is_under(resolved, data_root):
                continue
            try:
                if target.is_file() or (target.exists() and not target.is_dir()):
                    target.unlink()
                    removed += 1
            except OSError as exc:
                errors.append(f"{target}: {exc}")

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
