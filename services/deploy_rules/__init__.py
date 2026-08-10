"""Extensible deploy strategies selected by ``game.deploy_type`` / AppID."""

from __future__ import annotations

from services.deploy_rules.anno import ANNO_1800_APP_ID, Anno1800Strategy
from services.deploy_rules.base import DeployContext, DeployStrategy, StrategyResult
from services.deploy_rules.custom import DEPLOY_TYPE_CUSTOM_PATH, CustomPathStrategy
from services.deploy_rules.generic import FolderCopyStrategy
from services.deploy_rules.manifest import (
    MANIFEST_FILENAME,
    DeployManifest,
    ManifestFileEntry,
    delete_manifest,
    load_manifest,
    save_manifest,
)
from services.deploy_rules.palworld import PalworldPakStrategy, PalworldStrategy

DEPLOY_TYPE_FOLDER_COPY = FolderCopyStrategy.deploy_type
DEPLOY_TYPE_PALWORLD_PAK = PalworldStrategy.deploy_type
DEPLOY_TYPE_ANNO_1800 = Anno1800Strategy.deploy_type

# Steam AppID — always use enhanced PalworldStrategy (pak rules + folder_copy fallback).
PALWORLD_APP_ID = 1623730

_STRATEGIES: dict[str, DeployStrategy] = {
    DEPLOY_TYPE_FOLDER_COPY: FolderCopyStrategy(),
    DEPLOY_TYPE_PALWORLD_PAK: PalworldStrategy(),
    DEPLOY_TYPE_ANNO_1800: Anno1800Strategy(),
    DEPLOY_TYPE_CUSTOM_PATH: CustomPathStrategy(),
}


def resolve_deploy_type(app_id: int | str, deploy_type: str | None) -> str:
    """
    Pick effective deploy type.

    Palworld (1623730) always uses the enhanced ``palworld_pak`` strategy
    (special pak rules with folder_copy fallback).
    Anno 1800 (916440) always deploys into ``<install>/mods/``.
    Other games keep configured type.
    """
    try:
        aid = int(app_id)
    except (TypeError, ValueError):
        aid = 0
    if aid == PALWORLD_APP_ID:
        return DEPLOY_TYPE_PALWORLD_PAK
    if aid == ANNO_1800_APP_ID:
        return DEPLOY_TYPE_ANNO_1800
    key = (deploy_type or DEPLOY_TYPE_FOLDER_COPY).strip() or DEPLOY_TYPE_FOLDER_COPY
    return key


def get_strategy(
    deploy_type: str,
    *,
    app_id: int | str = 0,
) -> DeployStrategy | None:
    key = resolve_deploy_type(app_id, deploy_type)
    return _STRATEGIES.get(key)


def supported_deploy_types() -> tuple[str, ...]:
    return tuple(_STRATEGIES.keys())


__all__ = [
    "ANNO_1800_APP_ID",
    "DEPLOY_TYPE_ANNO_1800",
    "DEPLOY_TYPE_CUSTOM_PATH",
    "DEPLOY_TYPE_FOLDER_COPY",
    "DEPLOY_TYPE_PALWORLD_PAK",
    "PALWORLD_APP_ID",
    "Anno1800Strategy",
    "CustomPathStrategy",
    "DeployContext",
    "DeployManifest",
    "DeployStrategy",
    "MANIFEST_FILENAME",
    "ManifestFileEntry",
    "PalworldPakStrategy",
    "PalworldStrategy",
    "StrategyResult",
    "delete_manifest",
    "get_strategy",
    "load_manifest",
    "resolve_deploy_type",
    "save_manifest",
    "supported_deploy_types",
]
