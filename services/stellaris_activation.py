"""Stellaris (AppID 281990) — compatibility aliases for Paradox Launcher activation.

Generic runtime: ``services.paradox_activation``. This module keeps Stellaris
callers (UI, existing tests) on the previous function names and signatures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.mod_platform import STELLARIS_APP_IDS
from services.paradox_activation import (
    DLC_LOAD_FILENAME,
    LAUNCHER_DB_FILENAME,
    LAUNCHER_ID_PREFIX,
    STELLARIS_APP_ID,
    STELLARIS_ORDER_FILENAME,
    WORKSHOP_LAUNCHER_PREFIX,
    WORKSHOP_LAUNCHER_SUFFIX,
    ParadoxMapping,
    ParadoxModRef,
    ParadoxSyncReport,
    ParadoxWorkshopHit,
    default_stellaris_user_dir,
    is_smm_managed_launcher_entry,
    is_stellaris_activation_app,
    map_to_launcher_id,
    merge_enabled_mods,
    merge_paradox_enabled_mods,
    merge_stellaris_enabled_mods,
    non_smm_enabled_entries,
    normalize_launcher_id,
    parse_mod_descriptor,
    set_paradox_enabled,
    set_stellaris_enabled,
    workshop_launcher_id,
)
from services.paradox_activation import (
    apply_card_drop as _apply_card_drop,
    apply_order_move as _apply_order_move,
    deployed_load_order_tokens as _deployed_load_order_tokens,
    discover_workshop_mods as _discover_workshop_mods,
    enabled_load_order_tokens as _enabled_load_order_tokens,
    list_installed_paradox_mods,
    load_saved_order as _load_saved_order,
    persist_load_order as _persist_load_order,
    resolve_paradox_user_dir,
    resolved_load_order as _resolved_load_order,
    save_saved_order as _save_saved_order,
    sync_paradox_launcher,
    workshop_content_root as _workshop_content_root,
)
from services.wh3_activation import canon_internal_id, display_numbers

USER_DIR_REL = Path("Documents") / "Paradox Interactive" / "Stellaris"

StellarisWorkshopHit = ParadoxWorkshopHit
StellarisModRef = ParadoxModRef
StellarisMapping = ParadoxMapping
StellarisSyncReport = ParadoxSyncReport


def workshop_content_root(workshop_path: str | Path | None) -> Path | None:
    """Stellaris Workshop content root: ``.../workshop/content/281990``."""
    return _workshop_content_root(workshop_path, app_id=STELLARIS_APP_ID)


def discover_workshop_mods(workshop_path: str | Path | None) -> list[ParadoxWorkshopHit]:
    return _discover_workshop_mods(workshop_path, app_id=STELLARIS_APP_ID)


def list_installed_stellaris_mods(db: Any | None = None) -> list[ParadoxModRef]:
    return list_installed_paradox_mods(db, app_id=STELLARIS_APP_ID)


def load_saved_order() -> list[str]:
    return _load_saved_order(app_id=STELLARIS_APP_ID)


def save_saved_order(tokens: list[str]) -> None:
    _save_saved_order(tokens, app_id=STELLARIS_APP_ID)


def resolved_load_order(db: Any | None = None) -> list[str]:
    return _resolved_load_order(db, app_id=STELLARIS_APP_ID)


def persist_load_order(tokens: list[str], db: Any | None = None) -> list[str]:
    return _persist_load_order(tokens, db, app_id=STELLARIS_APP_ID)


def apply_card_drop(
    source_id: str,
    target_id: str,
    db: Any | None = None,
) -> list[str]:
    return _apply_card_drop(source_id, target_id, db, app_id=STELLARIS_APP_ID)


def apply_order_move(token: str, action: str, db: Any | None = None) -> list[str]:
    return _apply_order_move(token, action, db, app_id=STELLARIS_APP_ID)


def deployed_load_order_tokens(db: Any | None = None) -> list[str]:
    return _deployed_load_order_tokens(db, app_id=STELLARIS_APP_ID)


def enabled_load_order_tokens(
    db: Any | None = None,
    *,
    user_dir: str | Path | None = None,
) -> list[str]:
    return _enabled_load_order_tokens(
        db, app_id=STELLARIS_APP_ID, user_dir=user_dir
    )


def resolve_stellaris_user_dir(
    db: Any | None = None,
    *,
    user_dir: str | Path | None = None,
) -> Path:
    return resolve_paradox_user_dir(db, app_id=STELLARIS_APP_ID, user_dir=user_dir)


def sync_stellaris_launcher(
    db: Any | None = None,
    *,
    user_dir: str | Path | None = None,
) -> ParadoxSyncReport:
    return sync_paradox_launcher(db, app_id=STELLARIS_APP_ID, user_dir=user_dir)


__all__ = [
    "DLC_LOAD_FILENAME",
    "LAUNCHER_DB_FILENAME",
    "LAUNCHER_ID_PREFIX",
    "STELLARIS_APP_ID",
    "STELLARIS_APP_IDS",
    "STELLARIS_ORDER_FILENAME",
    "USER_DIR_REL",
    "WORKSHOP_LAUNCHER_PREFIX",
    "WORKSHOP_LAUNCHER_SUFFIX",
    "ParadoxMapping",
    "ParadoxModRef",
    "ParadoxSyncReport",
    "ParadoxWorkshopHit",
    "StellarisMapping",
    "StellarisModRef",
    "StellarisSyncReport",
    "StellarisWorkshopHit",
    "apply_card_drop",
    "apply_order_move",
    "canon_internal_id",
    "default_stellaris_user_dir",
    "deployed_load_order_tokens",
    "display_numbers",
    "discover_workshop_mods",
    "enabled_load_order_tokens",
    "is_smm_managed_launcher_entry",
    "is_stellaris_activation_app",
    "list_installed_stellaris_mods",
    "load_saved_order",
    "map_to_launcher_id",
    "merge_enabled_mods",
    "merge_paradox_enabled_mods",
    "merge_stellaris_enabled_mods",
    "non_smm_enabled_entries",
    "normalize_launcher_id",
    "parse_mod_descriptor",
    "persist_load_order",
    "resolve_stellaris_user_dir",
    "resolved_load_order",
    "save_saved_order",
    "set_paradox_enabled",
    "set_stellaris_enabled",
    "sync_stellaris_launcher",
    "workshop_content_root",
    "workshop_launcher_id",
]
