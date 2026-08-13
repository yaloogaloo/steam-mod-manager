"""Palworld enhanced deploy strategy: special pak rules + folder_copy fallback."""

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
from services.deploy_rules.generic import FolderCopyStrategy
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

logger = logging.getLogger(__name__)

_IGNORE_DIR_NAMES = frozenset(
    {
        INFO_DIR_NAME,
        LEGACY_INFO_DIR_NAME,
        "assets",
        "Assets",
        "历史版本",
    }
)
LOGIC_MODS_DIR = "LogicMods"
PAKS_DIR = "Paks"
PAKS_REL = Path("Pal") / "Content" / "Paks"
TILDE_MODS = "~mods"
ENTRY_TYPE_PAK = "pak"
ENTRY_TYPE_FOLDER = "folder_copy"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _iter_pak_files(directory: Path) -> list[Path]:
    """List ``*.pak`` files directly under *directory* (one level)."""
    if not directory.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(directory.glob("*.pak")):
        if path.is_file() and not path.name.startswith("."):
            out.append(path)
    return out


def _is_special_pak_relative(rel_parts: tuple[str, ...]) -> bool:
    """True if this relative path is handled by Palworld pak rules (not folder_copy)."""
    if not rel_parts:
        return False
    if rel_parts[0] == LOGIC_MODS_DIR:
        return True
    if rel_parts[0] == PAKS_DIR:
        return True
    if len(rel_parts) == 1 and rel_parts[0].lower().endswith(".pak"):
        return True
    return False


def _exclude_from_folder_copy(rel_parts: tuple[str, ...]) -> bool:
    """
    Paths handled by pak rules, plus any ``*.pak`` (never folder_copy into mod_path).
    """
    if _is_special_pak_relative(rel_parts):
        return True
    if rel_parts and rel_parts[-1].lower().endswith(".pak"):
        return True
    return False


class PalworldStrategy(DeployStrategy):
    """
    Palworld enhanced rules (not pak-only):

    1. ``source/LogicMods/*.pak`` → ``<install>/Pal/Content/Paks/LogicMods/``
    2. ``source/*.pak`` / ``source/Paks/*.pak`` → ``<install>/Pal/Content/Paks/~mods/``
    3. If no special paks (or remaining non-pak content with ``mod_path``):
       Generic ``folder_copy`` into ``game.mod_path``.
    """

    deploy_type = "palworld_pak"

    def __init__(self) -> None:
        self._folder = FolderCopyStrategy()

    def _install_root(self, ctx: DeployContext) -> Path | StrategyResult:
        install = str(ctx.config.install_path or "").strip()
        if not install:
            return StrategyResult(
                success=False,
                error="请先配置游戏安装目录（Palworld pak 规则需要安装路径）",
                deploy_type=self.deploy_type,
            )
        root = Path(install).expanduser().resolve()
        if not root.is_dir():
            return StrategyResult(
                success=False,
                error=f"游戏安装目录不存在：{root}",
                deploy_type=self.deploy_type,
            )
        return root

    def _paks_root(self, ctx: DeployContext) -> Path | StrategyResult:
        install = self._install_root(ctx)
        if isinstance(install, StrategyResult):
            return install
        return install / PAKS_REL

    def _collect_pak_entries(
        self, ctx: DeployContext, paks: Path
    ) -> list[ManifestFileEntry]:
        source = ctx.source.resolve()
        logic_src = source / LOGIC_MODS_DIR
        logic_dst_root = paks / LOGIC_MODS_DIR
        tilde = paks / TILDE_MODS
        entries: list[ManifestFileEntry] = []
        seen_names: set[str] = set()

        for path in _iter_pak_files(logic_src):
            if not is_rel_path_allowed(source, path, ctx.allowed_rel_paths):
                continue
            dst = (logic_dst_root / path.name).resolve()
            entries.append(
                ManifestFileEntry(
                    source=str(path),
                    target=str(dst),
                    type=ENTRY_TYPE_PAK,
                )
            )
            seen_names.add(path.name.lower())

        for path in _iter_pak_files(source) + _iter_pak_files(source / PAKS_DIR):
            if not is_rel_path_allowed(source, path, ctx.allowed_rel_paths):
                continue
            key = path.name.lower()
            if key in seen_names:
                continue
            try:
                rel_parts = path.relative_to(source).parts
            except ValueError:
                continue
            if any(part in _IGNORE_DIR_NAMES for part in rel_parts):
                continue
            dst = (tilde / path.name).resolve()
            entries.append(
                ManifestFileEntry(
                    source=str(path),
                    target=str(dst),
                    type=ENTRY_TYPE_PAK,
                )
            )
            seen_names.add(key)

        return entries

    def _has_pak_sources(self, source: Path, ctx: DeployContext | None = None) -> bool:
        source = source.resolve()
        allowed = ctx.allowed_rel_paths if ctx is not None else None
        for path in (
            _iter_pak_files(source / LOGIC_MODS_DIR)
            + _iter_pak_files(source)
            + _iter_pak_files(source / PAKS_DIR)
        ):
            if is_rel_path_allowed(source, path, allowed):
                return True
        return False

    def _folder_entries_excluding_paks(
        self, ctx: DeployContext
    ) -> StrategyResult:
        """Plan folder_copy then drop paths already handled by pak rules."""
        planned = self._folder.plan(ctx)
        if not planned.success:
            return planned
        source = ctx.source.resolve()
        filtered: list[ManifestFileEntry] = []
        for entry in planned.files:
            src = Path(entry.source)
            try:
                rel_parts = src.resolve().relative_to(source).parts
            except ValueError:
                continue
            if _exclude_from_folder_copy(rel_parts):
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
        source = ctx.source.resolve()
        has_paks = self._has_pak_sources(source, ctx)
        entries: list[ManifestFileEntry] = []
        target = ""

        if has_paks:
            paks = self._paks_root(ctx)
            if isinstance(paks, StrategyResult):
                return paks
            pak_entries = self._collect_pak_entries(ctx, paks)
            entries.extend(pak_entries)
            target = str(paks.resolve())

            # Remaining non-pak content → folder_copy when mod_path is configured
            if str(ctx.config.mod_path or "").strip():
                folder_plan = self._folder_entries_excluding_paks(ctx)
                if not folder_plan.success:
                    return folder_plan
                entries.extend(folder_plan.files)
                if folder_plan.target and not target:
                    target = folder_plan.target
            return StrategyResult(
                success=True,
                target=target,
                copied_files=len(entries),
                deploy_type=self.deploy_type,
                files=entries,
            )

        # Step 3: no special paks → full generic folder_copy
        folder_plan = self._folder.plan(ctx)
        if not folder_plan.success:
            return StrategyResult(
                success=False,
                error=folder_plan.error,
                deploy_type=self.deploy_type,
            )
        tagged = [
            ManifestFileEntry(
                source=e.source, target=e.target, type=ENTRY_TYPE_FOLDER
            )
            for e in folder_plan.files
        ]
        return StrategyResult(
            success=True,
            target=folder_plan.target,
            copied_files=len(tagged),
            deploy_type=self.deploy_type,
            files=tagged,
        )

    def _copy_entries(
        self, entries: list[ManifestFileEntry]
    ) -> StrategyResult | list[ManifestFileEntry]:
        out: list[ManifestFileEntry] = []
        for entry in entries:
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
            out.append(
                ManifestFileEntry(
                    source=str(src.resolve()),
                    target=str(dst.resolve()),
                    type=entry.type,
                )
            )
        return out

    def deploy(self, ctx: DeployContext) -> StrategyResult:
        source = ctx.source.resolve()
        has_paks = self._has_pak_sources(source, ctx)

        if not has_paks:
            # Step 3 — fallback to generic folder_copy (do not fail for missing pak)
            result = self._folder.deploy(ctx)
            if not result.success:
                return StrategyResult(
                    success=False,
                    error=result.error,
                    deploy_type=self.deploy_type,
                )
            when = result.deploy_time or _utc_now()
            tagged = [
                ManifestFileEntry(
                    source=e.source, target=e.target, type=ENTRY_TYPE_FOLDER
                )
                for e in result.files
            ]
            manifest = DeployManifest(
                mod_id=ctx.mod_id,
                deploy_time=when,
                deploy_type=self.deploy_type,
                files=tagged,
            )
            return StrategyResult(
                success=True,
                target=result.target,
                copied_files=len(tagged),
                deploy_type=self.deploy_type,
                deploy_time=when,
                files=tagged,
                manifest=manifest,
            )

        # Steps 1–2: pak rules
        paks = self._paks_root(ctx)
        if isinstance(paks, StrategyResult):
            return paks
        try:
            (paks / TILDE_MODS).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return StrategyResult(
                success=False,
                error=f"无法创建 ~mods 目录：{paks / TILDE_MODS}（{exc}）",
                deploy_type=self.deploy_type,
            )

        entries = self._collect_pak_entries(ctx, paks)

        # Case D: also folder_copy remaining when mod_path is set
        if str(ctx.config.mod_path or "").strip():
            folder_plan = self._folder_entries_excluding_paks(ctx)
            if not folder_plan.success:
                return StrategyResult(
                    success=False,
                    error=folder_plan.error,
                    deploy_type=self.deploy_type,
                )
            if folder_plan.files:
                # Ensure mod_path exists via folder strategy mkdir path
                mod_path = Path(str(ctx.config.mod_path).strip()).expanduser()
                try:
                    mod_path.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    return StrategyResult(
                        success=False,
                        error=f"无法创建 Mod 部署目录：{mod_path}（{exc}）",
                        deploy_type=self.deploy_type,
                    )
                entries.extend(folder_plan.files)

        copied = self._copy_entries(entries)
        if isinstance(copied, StrategyResult):
            return copied

        when = _utc_now()
        manifest = DeployManifest(
            mod_id=ctx.mod_id,
            deploy_time=when,
            deploy_type=self.deploy_type,
            files=copied,
        )
        return StrategyResult(
            success=True,
            target=str(paks.resolve()),
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
        """Delete only manifest targets — never remove ``~mods`` / ``Paks`` / mod roots."""
        if manifest is None or not manifest.files:
            return StrategyResult(success=True, deploy_type=self.deploy_type)

        stop_roots: list[Path] = []
        paks = self._paks_root(ctx)
        if isinstance(paks, Path):
            stop_roots.append(paks.resolve())
        else:
            install = str(ctx.config.install_path or "").strip()
            if install:
                try:
                    stop_roots.append(
                        (Path(install).expanduser().resolve() / PAKS_REL)
                    )
                except OSError:
                    pass
        mod_path_raw = str(ctx.config.mod_path or "").strip()
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


# Backward-compatible alias
PalworldPakStrategy = PalworldStrategy
