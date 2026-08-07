"""Deploy strategy base types."""

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
    """Inputs shared by all deploy strategies."""

    mod_id: str
    source: Path
    app_id: int
    config: GameDeployConfig
    deploy_type: str
    # When set, only these relative paths (posix) may be deployed.
    # None = legacy behaviour (entire Mod folder / strategy scan).
    allowed_rel_paths: frozenset[str] | None = None


@dataclass
class StrategyResult:
    """Outcome of deploy / undeploy (before DB update)."""

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


class DeployStrategy(ABC):
    """Pluggable deploy / undeploy rule for one ``game.deploy_type``."""

    deploy_type: str = ""

    def plan(self, ctx: DeployContext) -> StrategyResult:
        """Compute intended file mappings without copying (for conflict checks)."""
        return StrategyResult(
            success=False,
            error=f"策略未实现 plan()：{self.deploy_type}",
            deploy_type=self.deploy_type,
        )

    @abstractmethod
    def deploy(self, ctx: DeployContext) -> StrategyResult:
        """Copy files and return a result with file list for the manifest."""

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
