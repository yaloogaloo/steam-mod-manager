"""Stellaris (AppID 281990) — compatibility alias for Paradox Launcher activation."""

from __future__ import annotations

from core.mod_platform import STELLARIS_APP_IDS
from services.deploy_rules.paradox import (
    DEPLOY_TYPE_PARADOX_LAUNCHER,
    ParadoxLauncherStrategy,
)

STELLARIS_APP_ID = next(iter(STELLARIS_APP_IDS))
DEPLOY_TYPE_STELLARIS = "stellaris_launcher"


class StellarisStrategy(ParadoxLauncherStrategy):
    """Compatibility alias. Same inert mapping; deploy_type kept for old configs."""

    deploy_type = DEPLOY_TYPE_STELLARIS


__all__ = [
    "DEPLOY_TYPE_PARADOX_LAUNCHER",
    "DEPLOY_TYPE_STELLARIS",
    "STELLARIS_APP_ID",
    "ParadoxLauncherStrategy",
    "StellarisStrategy",
]
