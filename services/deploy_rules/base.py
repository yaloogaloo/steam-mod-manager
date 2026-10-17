"""Deploy strategy base types.

ARCHITECTURE CONTRACT (permanent)

Strategy = PATH-MAPPING ADAPTER, not a deployment engine.

Allowed:
- decide source → target path mapping for a game
- contribute entries that become DeployFilePlan.files
- undeploy by deleting paths listed in the Deploy Manifest only

Forbidden:
- copy / move / extract as a Strategy-owned deploy pipeline
- call ArchiveExtractor (or any extract primitive) directly
- after-before / snapshot-diff / post-copy filesystem scans to infer deployed files
- decide Core deploy success, run Backup/Verify, or author Manifest independently
- call self.plan() from deploy(); deploy() must stay inert

Authoritative file list: DeployFilePlan.files.
Execution owner: ModDeployer Core (Resolve → FilePlan → Backup → Apply → Verify → Manifest).
New games: add mapping rules here; never invent a second deploy pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.db_manager import GameDeployConfig
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry


def is_rel_path_allowed(
    source: Path,
    path: Path,
    allowed_rel_paths: frozenset[str] | None,
) -> bool:
    """True when *path* may be deployed under an optional allow-list."""
    if allowed_rel_paths is None:
        return True
    try:
        rel = path.resolve().relative_to(Path(source).resolve()).as_posix()
    except ValueError:
        rel = path.name
    if rel in allowed_rel_paths or path.name in allowed_rel_paths:
        return True
    return any(
        rel.endswith("/" + a) or a.endswith("/" + rel)
        for a in allowed_rel_paths
        if a
    )


@dataclass
class DeployContext:
    """Inputs shared by all deploy strategies.

    ``internal_id`` is Frozen Entity Identity (UUID). Never ``str(mod_id)``.
    ``mod_pk`` is the SQLite ``mods.mod_id`` handle for DAL/SQL only.
    ``workspace_id`` is platform display identity for game rules (WH3, etc.).
    """

    internal_id: str
    source: Path
    app_id: int
    config: GameDeployConfig
    deploy_type: str
    # When set, only these relative paths (posix) may be deployed.
    # None = legacy behaviour (entire Mod folder / strategy scan).
    allowed_rel_paths: frozenset[str] | None = None
    # Managed library folder (manifest + deploy target name). Defaults to source.
    managed_path: Path | None = None
    # Non-empty → CustomPathStrategy wins over game-level rules.
    custom_deploy_path: str = ""
    # Optional workspace / Steam file id (used by game-specific rules).
    workspace_id: str = ""
    # SQLite ``mods.mod_id``. 0 = unset (tests); production always sets PK.
    mod_pk: int = 0
    # Extracted archive trees only. Outer managed files stay on content_root
    # and are never copied into this overlay.
    extract_overlay_roots: tuple[Path, ...] = ()

    def content_root(self) -> Path:
        """Directory whose files are copied (may be an extract staging folder)."""
        return Path(self.source)

    def library_folder(self) -> Path:
        """Original managed Mod folder under the library."""
        return Path(self.managed_path) if self.managed_path is not None else Path(self.source)


@dataclass
class StrategyResult:
    """
    Path-mapping outcome from ``plan()`` (or undeploy outcome).

    For Core Deploy, ``files`` feed DeployFilePlan. Success of Core Deploy is
    decided by Verify — not by StrategyResult.success from ``deploy()``.
    """

    success: bool
    error: str = ""
    target: str = ""
    copied_files: int = 0
    deploy_type: str = ""
    deploy_time: str = ""
    files: list[ManifestFileEntry] = field(default_factory=list)
    manifest: DeployManifest | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "success": self.success,
            "mod_id": "",
        }
        if self.error:
            out["error"] = self.error
        if self.target:
            out["target"] = self.target
        if self.copied_files:
            out["copied_files"] = self.copied_files
        if self.deploy_type:
            out["deploy_type"] = self.deploy_type
        if self.deploy_time:
            out["deploy_time"] = self.deploy_time
        return out


def inert_strategy_deploy(deploy_type: str) -> StrategyResult:
    """
    Compatibility shell only: Strategy.deploy must not mutate the filesystem.

    Callers that need a real deploy use ``ModDeployer.deploy_mod`` (or
    ``plan`` + ``apply_file_plan`` in tests).
    """
    return StrategyResult(
        success=False,
        error=(
            f"{deploy_type or 'strategy'}.deploy is inert; "
            "deployment is owned by ModDeployer FilePlan Apply"
        ),
        deploy_type=deploy_type,
    )


class DeployStrategy(ABC):
    """
    ARCHITECTURE CONTRACT — path-mapping adapter for one ``game.deploy_type``.

    ``plan(ctx)`` builds source→target mappings (no I/O mutation).
    ``deploy(ctx)`` is inert; Core Apply owns copy/extract.
    ``undeploy`` deletes only Manifest-listed targets.
    """

    deploy_type: str = ""

    def plan(self, ctx: DeployContext) -> StrategyResult:
        """
        Build intended source→target mappings only (no copy/extract).

        Output ``files`` become DeployFilePlan — the sole deploy file list.
        """
        return StrategyResult(
            success=False,
            error=f"策略未实现 plan()：{self.deploy_type}",
            deploy_type=self.deploy_type,
        )

    def deploy(self, ctx: DeployContext) -> StrategyResult:
        """Inert compatibility shell — must not plan, copy, extract, or account."""
        return inert_strategy_deploy(self.deploy_type)

    @abstractmethod
    def undeploy(
        self,
        ctx: DeployContext,
        manifest: DeployManifest | None,
    ) -> StrategyResult:
        """
        Remove previously deployed files using *manifest*.

        Must only delete paths listed in the manifest — never wipe a whole
        target tree.
        """
