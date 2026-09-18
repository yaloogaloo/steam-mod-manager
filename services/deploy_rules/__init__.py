"""Extensible deploy strategies selected by ``game.deploy_type`` / AppID."""

from __future__ import annotations

from core.mod_platform import CIVILIZATION_VI_APP_IDS
from services.deploy_rules.anno import ANNO_1800_APP_ID, Anno1800Strategy
from services.deploy_rules.base import DeployContext, DeployStrategy, StrategyResult
from services.deploy_rules.custom import DEPLOY_TYPE_CUSTOM_PATH, CustomPathStrategy
from services.deploy_rules.generic import (
    FolderCopyStrategy,
    contains_han_characters,
    deploy_folder_name,
    deploy_wrapper_folder,
)
from services.deploy_rules.manifest import (
    MANIFEST_FILENAME,
    DeployManifest,
    ManifestBackupInfo,
    ManifestFileEntry,
    delete_manifest,
    load_manifest,
    save_manifest,
)
from services.deploy_rules.pak_mod_path import (
    PakModPathStrategy,
    content_has_pak_files,
)
from services.deploy_rules.palworld import PalworldPakStrategy, PalworldStrategy
from services.deploy_rules.slay_the_spire import (
    SLAY_THE_SPIRE_APP_ID,
    SlayTheSpireStrategy,
)
from services.deploy_rules.stardew_valley import (
    STARDEW_VALLEY_APP_ID,
    StardewValleyStrategy,
)
from services.deploy_rules.duckov import (
    DUCKOV_APP_ID,
    DEPLOY_TYPE_DUCKOV,
    DuckovStrategy,
    find_duckov_mod_root,
)
from services.deploy_rules.stellaris import (
    STELLARIS_APP_ID,
    StellarisStrategy,
)
from services.deploy_rules.warhammer3 import (
    WARHAMMER3_APP_ID,
    Warhammer3Strategy,
)

# Steam AppID — Civilization VI always uses generic folder_copy.
CIVILIZATION_VI_APP_ID = next(iter(CIVILIZATION_VI_APP_IDS))

DEPLOY_TYPE_FOLDER_COPY = FolderCopyStrategy.deploy_type
DEPLOY_TYPE_PALWORLD_PAK = PalworldStrategy.deploy_type
DEPLOY_TYPE_ANNO_1800 = Anno1800Strategy.deploy_type
DEPLOY_TYPE_SLAY_THE_SPIRE = SlayTheSpireStrategy.deploy_type
DEPLOY_TYPE_STARDEW_VALLEY = StardewValleyStrategy.deploy_type
DEPLOY_TYPE_DUCKOV = DuckovStrategy.deploy_type
DEPLOY_TYPE_PAK_MOD_PATH = PakModPathStrategy.deploy_type
DEPLOY_TYPE_WARHAMMER3 = Warhammer3Strategy.deploy_type
DEPLOY_TYPE_STELLARIS = StellarisStrategy.deploy_type

# Steam AppID — always use enhanced PalworldStrategy (pak rules + folder_copy fallback).
PALWORLD_APP_ID = 1623730

_STRATEGIES: dict[str, DeployStrategy] = {
    DEPLOY_TYPE_FOLDER_COPY: FolderCopyStrategy(),
    DEPLOY_TYPE_PAK_MOD_PATH: PakModPathStrategy(),
    DEPLOY_TYPE_PALWORLD_PAK: PalworldStrategy(),
    DEPLOY_TYPE_ANNO_1800: Anno1800Strategy(),
    DEPLOY_TYPE_SLAY_THE_SPIRE: SlayTheSpireStrategy(),
    DEPLOY_TYPE_STARDEW_VALLEY: StardewValleyStrategy(),
    DEPLOY_TYPE_DUCKOV: DuckovStrategy(),
    DEPLOY_TYPE_WARHAMMER3: Warhammer3Strategy(),
    DEPLOY_TYPE_STELLARIS: StellarisStrategy(),
    DEPLOY_TYPE_CUSTOM_PATH: CustomPathStrategy(),
}


def resolve_deploy_type(app_id: int | str, deploy_type: str | None) -> str:
    """
    Pick effective deploy type.

    Palworld (1623730) always uses the enhanced ``palworld_pak`` strategy
    (special pak rules with folder_copy fallback).
    Anno 1800 (916440) always deploys into ``<install>/mods/``.
    Slay the Spire (646570) always uses jar → mods/ (+ ModTheSpire root).
    Stardew Valley (413150) always uses SMAPI ``manifest.json`` → Mods/.
    Civilization VI (289070) always uses generic ``folder_copy``.
    Total War: WARHAMMER III (1142710) always uses library activation
    (``warhammer3_pack``): confirm local ``.pack`` files, never flatten-copy
    into ``game.mod_path``.
    Stellaris (281990) always uses launcher enable/order sync
    (``stellaris_launcher``): never copy Workshop content.
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
    if aid == SLAY_THE_SPIRE_APP_ID:
        return DEPLOY_TYPE_SLAY_THE_SPIRE
    if aid == STARDEW_VALLEY_APP_ID:
        return DEPLOY_TYPE_STARDEW_VALLEY
    if aid == DUCKOV_APP_ID:
        return DEPLOY_TYPE_DUCKOV
    if aid == CIVILIZATION_VI_APP_ID:
        return DEPLOY_TYPE_FOLDER_COPY
    if aid == WARHAMMER3_APP_ID:
        return DEPLOY_TYPE_WARHAMMER3
    if aid == STELLARIS_APP_ID:
        return DEPLOY_TYPE_STELLARIS
    key = (deploy_type or DEPLOY_TYPE_FOLDER_COPY).strip() or DEPLOY_TYPE_FOLDER_COPY
    return key


def get_strategy(
    deploy_type: str,
    *,
    app_id: int | str = 0,
) -> DeployStrategy | None:
    key = resolve_deploy_type(app_id, deploy_type)
    return _STRATEGIES.get(key)


def resolve_strategy(ctx: DeployContext) -> DeployStrategy | None:
    """
    Pick deploy strategy for *ctx*.

    Priority:
    1. ``custom_deploy_path`` → CustomPathStrategy
    2. Content includes ``*.pak`` on a ``folder_copy`` game → PakModPathStrategy
    3. Game / configured deploy type
    """
    if str(ctx.custom_deploy_path or "").strip():
        return CustomPathStrategy()
    effective = resolve_deploy_type(ctx.app_id, ctx.deploy_type)
    if effective == DEPLOY_TYPE_FOLDER_COPY and content_has_pak_files(ctx):
        return PakModPathStrategy()
    return get_strategy(effective, app_id=ctx.app_id)


def supported_deploy_types() -> tuple[str, ...]:
    return tuple(_STRATEGIES.keys())


__all__ = [
    "ANNO_1800_APP_ID",
    "CIVILIZATION_VI_APP_ID",
    "DEPLOY_TYPE_ANNO_1800",
    "DEPLOY_TYPE_CUSTOM_PATH",
    "DEPLOY_TYPE_FOLDER_COPY",
    "DEPLOY_TYPE_PAK_MOD_PATH",
    "DEPLOY_TYPE_PALWORLD_PAK",
    "DEPLOY_TYPE_SLAY_THE_SPIRE",
    "DEPLOY_TYPE_DUCKOV",
    "DEPLOY_TYPE_STARDEW_VALLEY",
    "DEPLOY_TYPE_STELLARIS",
    "DEPLOY_TYPE_WARHAMMER3",
    "DUCKOV_APP_ID",
    "STELLARIS_APP_ID",
    "StellarisStrategy",
    "DuckovStrategy",
    "PALWORLD_APP_ID",
    "PakModPathStrategy",
    "SLAY_THE_SPIRE_APP_ID",
    "STARDEW_VALLEY_APP_ID",
    "WARHAMMER3_APP_ID",
    "Anno1800Strategy",
    "CustomPathStrategy",
    "deploy_folder_name",
    "deploy_wrapper_folder",
    "contains_han_characters",
    "DeployContext",
    "DeployManifest",
    "DeployStrategy",
    "MANIFEST_FILENAME",
    "ManifestFileEntry",
    "PalworldPakStrategy",
    "PalworldStrategy",
    "SlayTheSpireStrategy",
    "StardewValleyStrategy",
    "Warhammer3Strategy",
    "StrategyResult",
    "delete_manifest",
    "content_has_pak_files",
    "get_strategy",
    "load_manifest",
    "resolve_deploy_type",
    "resolve_strategy",
    "save_manifest",
    "supported_deploy_types",
]
