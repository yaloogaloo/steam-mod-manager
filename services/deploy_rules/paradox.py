"""Paradox Launcher activation (Stellaris, CK3) — never copy Workshop files."""

from __future__ import annotations

from services.deploy_rules.base import (
    DeployContext,
    DeployStrategy,
    StrategyResult,
    inert_strategy_deploy,
)

DEPLOY_TYPE_PARADOX_LAUNCHER = "paradox_launcher"


class ParadoxLauncherStrategy(DeployStrategy):
    """Confirm the library folder exists. Core Apply must not copy Workshop files."""

    deploy_type = DEPLOY_TYPE_PARADOX_LAUNCHER

    def plan(self, ctx: DeployContext) -> StrategyResult:
        library = ctx.library_folder()
        return StrategyResult(
            success=True,
            target=str(library),
            copied_files=0,
            deploy_type=self.deploy_type,
            files=[],
        )

    def deploy(self, ctx: DeployContext) -> StrategyResult:
        del ctx
        return inert_strategy_deploy(self.deploy_type)

    def undeploy(self, ctx: DeployContext, manifest: object | None) -> StrategyResult:
        del ctx, manifest
        return StrategyResult(success=True, deploy_type=self.deploy_type)
