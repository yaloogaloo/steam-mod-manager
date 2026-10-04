"""Paradox Launcher / Clausewitz Workshop activation + load order (Stellaris, CK3).

Source of truth:

- Membership: ``mods.deploy_status = deployed`` — the only Sort Mode membership
- Order: ``config/load_order/<order_filename>`` — sequence of that membership
  (internal_id). A deployed Mod missing from the file is appended. An
  undeployed Mod is not a member, even if an older file still names it.
- Launcher files (``ugc_*.mod``, playsets, ``dlc_load.json``) are external
  projection. ``unresolved`` does not remove Sort Mode membership.
- Direct EXE: ``dlc_load.json`` ``enabled_mods`` — mixed list; SMM rewrites
  only its managed Mod subset (B + order) whether or not a playset is active.
  DLC / unknown / non-SMM entries are kept. Order matters.
- Playset rows: the same subsequence is applied only to the active playset.
  No active playset means those rows stay as they are.
- Launcher effective export is the active playset plus that playset's
  enabled members. ``dlc_load.json`` is the Direct EXE list, not that export.
  Playset activation stays Launcher-owned.

Never copies Workshop content. Never mints Launcher Mods. Never uses SMM
``internal_id`` as a Launcher identifier. Never adopts A into B.

CK3 first launch verification = CONFIRMED (2026-09-19): Launcher writes
``Documents/Paradox Interactive/Crusader Kings III/mod/ugc_<WorkshopID>.mod``
with ``path=`` at ``workshop/content/1158310/<WorkshopID>``. Missing launcher
entries stay unresolved. SMM must not mint them.

Sort direction (Stellaris + CK3, same Paradox Launcher):

- SMM ``order[0]`` / display #1 = playset TOP = ``playsets_mods.position`` 0
  = ``dlc_load.json`` SMM subsequence [0]
- Playset TOP loads first (lower identical-file priority)
- Playset BOTTOM loads last and overwrites identical files from above

Per-game configuration lives in ``PARADOX_LAUNCHER_GAMES``. Generic helpers
must look up that table — they must not branch on ``if app_id == 281990``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.db_manager import DatabaseManager, get_db
from core.mod_platform import (
    CK3_APP_IDS,
    PLATFORM_STEAM,
    STELLARIS_APP_IDS,
    is_ck3_game,
    is_internal_mod_id,
    is_stellaris_game,
)
from services.wh3_activation import (
    canon_internal_id,
    merge_deployed_order,
    move_in_order,
)

logger = logging.getLogger(__name__)

STELLARIS_APP_ID = next(iter(STELLARIS_APP_IDS))
CK3_APP_ID = next(iter(CK3_APP_IDS))

DLC_LOAD_FILENAME = "dlc_load.json"
LAUNCHER_DB_FILENAME = "launcher-v2.sqlite"
LAUNCHER_ID_PREFIX = "mod/"
WORKSHOP_LAUNCHER_PREFIX = "mod/ugc_"
WORKSHOP_LAUNCHER_SUFFIX = ".mod"
PARADOX_INTERACTIVE_REL = Path("Documents") / "Paradox Interactive"

STELLARIS_ORDER_FILENAME = "stellaris.json"
CK3_ORDER_FILENAME = "ck3.json"

_DESCRIPTOR_KV_RE = re.compile(r'^([A-Za-z0-9_]+)\s*=\s*"(.*)"\s*$')


@dataclass(frozen=True)
class ParadoxGameConfig:
    """Per-game Paradox Launcher activation settings."""

    app_id: int
    game_id: str
    user_dir_name: str
    order_filename: str

    @property
    def user_dir_rel(self) -> Path:
        return PARADOX_INTERACTIVE_REL / self.user_dir_name


PARADOX_LAUNCHER_GAMES: tuple[ParadoxGameConfig, ...] = (
    ParadoxGameConfig(
        app_id=STELLARIS_APP_ID,
        game_id="stellaris",
        user_dir_name="Stellaris",
        order_filename=STELLARIS_ORDER_FILENAME,
    ),
    ParadoxGameConfig(
        app_id=CK3_APP_ID,
        game_id="ck3",
        user_dir_name="Crusader Kings III",
        order_filename=CK3_ORDER_FILENAME,
    ),
)

_GAMES_BY_APP_ID: dict[int, ParadoxGameConfig] = {
    game.app_id: game for game in PARADOX_LAUNCHER_GAMES
}


def _coerce_app_id(app_id: int | str = 0) -> int:
    try:
        return int(str(app_id or 0).strip() or 0)
    except (TypeError, ValueError):
        return 0


def paradox_game_for_app_id(app_id: int | str = 0) -> ParadoxGameConfig | None:
    """Look up Paradox Launcher config. None when *app_id* is not in the table."""
    aid = _coerce_app_id(app_id)
    if aid <= 0:
        return None
    return _GAMES_BY_APP_ID.get(aid)


def paradox_game_for_name(game_name: str = "") -> ParadoxGameConfig | None:
    if is_stellaris_game(game_name):
        return _GAMES_BY_APP_ID[STELLARIS_APP_ID]
    if is_ck3_game(game_name):
        return _GAMES_BY_APP_ID[CK3_APP_ID]
    return None


def paradox_launcher_app_ids() -> frozenset[int]:
    return frozenset(_GAMES_BY_APP_ID)


def is_paradox_activation_app(app_id: int | str = 0, game_name: str = "") -> bool:
    """True when this game uses Paradox Launcher Workshop activation."""
    if paradox_game_for_app_id(app_id) is not None:
        return True
    return bool(game_name) and paradox_game_for_name(game_name) is not None


def is_stellaris_activation_app(app_id: int | str = 0, game_name: str = "") -> bool:
    """True only for Stellaris. CK3 is Paradox activation, not Stellaris."""
    aid = _coerce_app_id(app_id)
    if aid in STELLARIS_APP_IDS:
        return True
    return bool(game_name) and is_stellaris_game(game_name, aid)


def is_ck3_activation_app(app_id: int | str = 0, game_name: str = "") -> bool:
    """True only for Crusader Kings III."""
    aid = _coerce_app_id(app_id)
    if aid in CK3_APP_IDS:
        return True
    return bool(game_name) and is_ck3_game(game_name, aid)


def default_paradox_user_dir(app_id: int | str) -> Path:
    game = paradox_game_for_app_id(app_id)
    if game is None:
        return Path.home() / PARADOX_INTERACTIVE_REL
    return Path.home() / game.user_dir_rel


def default_stellaris_user_dir() -> Path:
    return default_paradox_user_dir(STELLARIS_APP_ID)


def _order_path(app_id: int | str) -> Path | None:
    from core.paths import load_order_dir

    game = paradox_game_for_app_id(app_id)
    if game is None:
        return None
    return load_order_dir() / game.order_filename


def _parse_order_payload(raw: object) -> list[str]:
    if isinstance(raw, dict):
        items = raw.get("order") or []
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        mid = canon_internal_id(item)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out


def load_saved_order(*, app_id: int | str) -> list[str]:
    """Persisted SMM load-order tokens (may include stale / disabled ids)."""
    path = _order_path(app_id)
    if path is None or not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    return _parse_order_payload(raw)


def save_saved_order(tokens: list[str], *, app_id: int | str) -> None:
    path = _order_path(app_id)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in tokens:
        mid = canon_internal_id(raw)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        ordered.append(mid)
    path.write_text(
        json.dumps({"order": ordered}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def workshop_launcher_id(workspace_id: str) -> str:
    wid = str(workspace_id or "").strip()
    return f"{WORKSHOP_LAUNCHER_PREFIX}{wid}{WORKSHOP_LAUNCHER_SUFFIX}" if wid else ""


def normalize_launcher_id(raw: object) -> str:
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return ""
    if text.startswith(LAUNCHER_ID_PREFIX):
        return text
    name = Path(text).name
    if name.endswith(".mod"):
        return f"{LAUNCHER_ID_PREFIX}{name}"
    return ""


def parse_mod_descriptor(text: str) -> dict[str, str]:
    """Parse scalar quoted Paradox ``.mod`` fields actually present on disk."""
    out: dict[str, str] = {}
    for line in str(text or "").splitlines():
        match = _DESCRIPTOR_KV_RE.match(line.strip())
        if match is None:
            continue
        out[match.group(1)] = match.group(2)
    return out


@dataclass(frozen=True)
class ParadoxWorkshopHit:
    workspace_id: str
    path: str
    name: str = ""
    remote_file_id: str = ""
    descriptor_path: str = ""


StellarisWorkshopHit = ParadoxWorkshopHit


def workshop_content_root(
    workshop_path: str | Path | None,
    *,
    app_id: int | str,
) -> Path | None:
    """Workshop content root: ``.../workshop/content/<app_id>``."""
    raw = str(workshop_path or "").strip()
    if not raw:
        return None
    aid = _coerce_app_id(app_id)
    if aid <= 0:
        return None
    base = Path(raw).expanduser()
    app = str(aid)
    try:
        name = base.name
    except OSError:
        return None
    if name == app:
        return base
    if name.lower() == "content":
        return base / app
    return base / "content" / app


def discover_workshop_mods(
    workshop_path: str | Path | None,
    *,
    app_id: int | str,
) -> list[ParadoxWorkshopHit]:
    """One-shot Workshop folder listing. Never call from UI refresh."""
    root = workshop_content_root(workshop_path, app_id=app_id)
    if root is None:
        return []
    try:
        if not root.is_dir():
            return []
        children = list(root.iterdir())
    except OSError:
        return []
    hits: list[ParadoxWorkshopHit] = []
    for child in children:
        try:
            if not child.is_dir() or not child.name.isdigit():
                continue
        except OSError:
            continue
        remote = child.name
        title = ""
        descriptor = ""
        desc_path = child / "descriptor.mod"
        try:
            if desc_path.is_file():
                descriptor = str(desc_path)
                parsed = parse_mod_descriptor(
                    desc_path.read_text(encoding="utf-8", errors="replace")
                )
                title = str(parsed.get("name") or "").strip()
                remote = str(parsed.get("remote_file_id") or remote).strip() or child.name
        except OSError:
            pass
        hits.append(
            ParadoxWorkshopHit(
                workspace_id=child.name,
                path=str(child),
                name=title,
                remote_file_id=remote,
                descriptor_path=descriptor,
            )
        )
    hits.sort(key=lambda hit: hit.workspace_id)
    return hits


@dataclass(frozen=True)
class ParadoxModRef:
    token: str
    workspace_id: str
    enabled: bool
    deployed: bool
    last_known_path: str
    platform: str = ""
    external_id: str = ""
    title: str = ""
    entity_internal_id: str = ""
    # Frozen business identity. Empty when the row has no Frozen UUID.
    # Never a SQLite PK, workspace id, or launcher id.
    internal_id: str = ""


StellarisModRef = ParadoxModRef


@dataclass
class ParadoxMapping:
    token: str
    launcher_id: str
    available: bool
    reason: str = ""


StellarisMapping = ParadoxMapping


RESILIENCE_DEPLOYED_EFFECTIVE = "DEPLOYED_EFFECTIVE"
RESILIENCE_DEPLOYED_NOT_EFFECTIVE = "DEPLOYED_NOT_EFFECTIVE"
RESILIENCE_NOT_DEPLOYED = "NOT_DEPLOYED"
NO_ACTIVE_PLAYSET_SENTINEL = "<no-active-playset>"
PARADOX_PLAYSET_NOTICE_FILENAME = "paradox_playset_notice.json"
PARADOX_INACTIVE_PLAYSET_MESSAGE = (
    "SMM 已完成 Mod 部署，并已生成直接启动游戏所需的 Mod 加载列表。\n\n"
    "但 Paradox Launcher 当前没有启用任何 Playset。\n"
    "如果通过 Launcher 启动游戏，请先在 Launcher 中激活一个 Playset。"
)


@dataclass(frozen=True)
class ParadoxEffectiveState:
    """Read-only Launcher effective export. Not the SMM projection file."""

    active_playset_id: str | None = None
    effective_launcher_ids: list[str] = field(default_factory=list)
    read_error: str = ""


@dataclass
class ParadoxPlaysetNotice:
    should_prompt: bool = False
    message: str = ""


@dataclass
class ParadoxSyncReport:
    written: bool = False
    enabled_launcher_ids: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    active_playset_id: str | None = None
    effective_launcher_ids: list[str] = field(default_factory=list)
    is_effective: bool = False
    resilience_status: str = RESILIENCE_NOT_DEPLOYED
    playset_written: bool = False
    direct_dlc_load_written: bool = False


StellarisSyncReport = ParadoxSyncReport


def _row_to_ref(row: dict[str, Any]) -> ParadoxModRef | None:
    """Build a ref. Order token is the Frozen UUID, never a SQLite PK."""
    from services.deploy_identity import is_frozen_internal_uuid

    raw_iid = str(row.get("internal_id") or "").strip()
    internal_id = raw_iid if is_frozen_internal_uuid(raw_iid) else ""
    if not internal_id:
        logger.warning(
            "Paradox ref has no Frozen internal_id; order token left empty"
        )
    enabled = True
    raw_enabled = row.get("enabled", True)
    try:
        enabled = bool(int(raw_enabled)) if raw_enabled is not None else True
    except (TypeError, ValueError):
        enabled = bool(raw_enabled)
    raw_deployed = row.get("deployed")
    if raw_deployed is None:
        deployed = str(row.get("deploy_status") or "").strip().lower() == "deployed"
    else:
        deployed = bool(raw_deployed)
    return ParadoxModRef(
        token=internal_id,
        internal_id=internal_id,
        workspace_id=str(row.get("workspace_id") or "").strip(),
        enabled=enabled,
        deployed=deployed,
        last_known_path=str(
            row.get("managed_path") or row.get("last_known_path") or ""
        ).strip(),
        platform=str(row.get("platform") or "").strip(),
        external_id=str(row.get("external_id") or "").strip(),
        title=str(row.get("steam_name") or row.get("name") or "").strip(),
        entity_internal_id=str(row.get("entity_internal_id") or "").strip(),
    )


def list_installed_paradox_mods(
    db: DatabaseManager | None = None,
    *,
    app_id: int | str,
) -> list[ParadoxModRef]:
    """SMM Mods already in the library for *app_id*. No Workshop rescan."""
    game = paradox_game_for_app_id(app_id)
    if game is None:
        return []
    database = db if db is not None else get_db()
    out: list[ParadoxModRef] = []
    seen: set[str] = set()
    for row in database.list_mod_list_items(app_id=game.app_id):
        ref = _row_to_ref(row)
        if ref is None:
            continue
        # Dedup valid entities by Frozen UUID. A missing UUID is not a PK key.
        key = ref.internal_id or f"missing:{row.get('mod_id')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def list_installed_stellaris_mods(
    db: DatabaseManager | None = None,
) -> list[ParadoxModRef]:
    return list_installed_paradox_mods(db, app_id=STELLARIS_APP_ID)


def _deployed_order_tokens(
    db: DatabaseManager | None = None,
    *,
    app_id: int | str,
) -> list[str]:
    """Deployed Frozen UUIDs for this Paradox game. Not all installed mods."""
    from services.canonical_membership import canonical_deployed_internal_ids

    return canonical_deployed_internal_ids(
        list_installed_paradox_mods(db, app_id=app_id)
    )


def resolved_load_order(
    db: DatabaseManager | None = None,
    *,
    app_id: int | str,
) -> list[str]:
    return merge_deployed_order(
        load_saved_order(app_id=app_id),
        _deployed_order_tokens(db, app_id=app_id),
    )


def _order_handle_map(
    db: DatabaseManager | None,
    *,
    app_id: int | str,
) -> dict[str, str]:
    """Map a Frozen UUID or a SQLite PK handle onto the Frozen UUID.

    The stored value is always the Frozen UUID. A PK with no UUID is not
    written back as an order token.
    """
    from services.deploy_identity import is_frozen_internal_uuid

    game = paradox_game_for_app_id(app_id)
    if game is None:
        return {}
    database = db if db is not None else get_db()
    mapping: dict[str, str] = {}
    for row in database.list_mod_list_items(app_id=game.app_id):
        raw_iid = str(row.get("internal_id") or "").strip()
        iid = raw_iid if is_frozen_internal_uuid(raw_iid) else ""
        pk = str(row.get("mod_id") or "").strip()
        if pk.isdigit():
            pk = str(int(pk))
        else:
            pk = ""
        if not iid:
            continue
        mapping[iid] = iid
        if pk:
            mapping[pk] = iid
    return mapping


def _order_token_from_handle(raw: object, mapping: dict[str, str]) -> str:
    """Inbound handle → Frozen UUID. Unknown digits and other tokens are dropped."""
    from services.deploy_identity import is_frozen_internal_uuid

    text = str(raw or "").strip()
    if text.isdigit():
        text = str(int(text))
    mapped = mapping.get(text, "")
    if is_frozen_internal_uuid(mapped):
        return mapped
    if is_frozen_internal_uuid(text):
        return text
    return ""


def persist_load_order(
    tokens: list[str],
    db: DatabaseManager | None = None,
    *,
    app_id: int | str,
) -> list[str]:
    mapping = _order_handle_map(db, app_id=app_id)
    canon = [_order_token_from_handle(item, mapping) for item in tokens]
    deployed = _deployed_order_tokens(db, app_id=app_id)
    merged = merge_deployed_order([item for item in canon if item], deployed)
    save_saved_order(merged, app_id=app_id)
    return merged


def apply_card_drop(
    source_id: str,
    target_id: str,
    db: DatabaseManager | None = None,
    *,
    app_id: int | str,
) -> list[str]:
    mapping = _order_handle_map(db, app_id=app_id)
    current = resolved_load_order(db, app_id=app_id)
    next_order = move_in_order(
        current,
        _order_token_from_handle(source_id, mapping),
        _order_token_from_handle(target_id, mapping),
    )
    save_saved_order(next_order, app_id=app_id)
    return next_order


ORDER_MOVE_UP = "up"
ORDER_MOVE_DOWN = "down"
ORDER_MOVE_TOP = "top"
ORDER_MOVE_BOTTOM = "bottom"


def _move_order_token(order: list[str], token: str, action: str) -> list[str]:
    """Mutate an internal_id order list. Index 0 is TOP (display #1)."""
    ids = [canon_internal_id(item) for item in order if canon_internal_id(item)]
    mid = canon_internal_id(token)
    if not mid or mid not in ids:
        return ids
    index = ids.index(mid)
    if action == ORDER_MOVE_UP:
        if index <= 0:
            return ids
        ids[index - 1], ids[index] = ids[index], ids[index - 1]
        return ids
    if action == ORDER_MOVE_DOWN:
        if index >= len(ids) - 1:
            return ids
        ids[index], ids[index + 1] = ids[index + 1], ids[index]
        return ids
    if action == ORDER_MOVE_TOP:
        if index <= 0:
            return ids
        ids.insert(0, ids.pop(index))
        return ids
    if action == ORDER_MOVE_BOTTOM:
        if index >= len(ids) - 1:
            return ids
        ids.append(ids.pop(index))
        return ids
    return ids


def apply_order_move(
    token: str,
    action: str,
    db: DatabaseManager | None = None,
    *,
    app_id: int | str,
) -> list[str]:
    """Move one order token. Persists ``config/load_order/<game>.json`` only.

    Does not scan Workshop, hash payload, copy files, or mint ``ugc_*.mod``.
    Caller projects to the Launcher with ``sync_paradox_launcher``.
    """
    mapping = _order_handle_map(db, app_id=app_id)
    current = resolved_load_order(db, app_id=app_id)
    next_order = _move_order_token(
        current,
        _order_token_from_handle(token, mapping),
        action,
    )
    return persist_load_order(next_order, db, app_id=app_id)


def set_paradox_enabled(
    token: str,
    enabled: bool,
    db: DatabaseManager | None = None,
) -> bool:
    """Toggle ``mods.enabled`` only — never copies or deletes Mod files."""
    database = db if db is not None else get_db()
    mid = canon_internal_id(token)
    if not mid or not mid.isdigit():
        return False
    if enabled:
        database.enable_mod(mid)
    else:
        database.disable_mod(mid)
    return bool(database.is_mod_enabled(mid)) is bool(enabled)


set_stellaris_enabled = set_paradox_enabled


def _steam_workspace_id(ref: ParadoxModRef) -> str:
    for cand in (ref.workspace_id, ref.external_id):
        text = str(cand or "").strip()
        if text.isdigit() and not is_internal_mod_id(text):
            return text
    return ""


def _local_descriptor_launcher_id(ref: ParadoxModRef, user_dir: Path) -> str:
    managed = Path(str(ref.last_known_path or "").strip()).expanduser() if ref.last_known_path else None
    candidates: list[Path] = []
    if managed is not None:
        try:
            if managed.is_file() and managed.suffix.lower() == ".mod":
                return normalize_launcher_id(managed.name)
            if managed.is_dir():
                for child in managed.glob("*.mod"):
                    candidates.append(child)
        except OSError:
            pass
    mod_dir = user_dir / "mod"
    try:
        if mod_dir.is_dir():
            for child in mod_dir.iterdir():
                if child.suffix.lower() != ".mod" or child.name.startswith("ugc_"):
                    continue
                try:
                    parsed = parse_mod_descriptor(
                        child.read_text(encoding="utf-8", errors="replace")
                    )
                except OSError:
                    continue
                raw_path = str(parsed.get("path") or "").strip()
                if not raw_path or managed is None:
                    continue
                try:
                    desc_target = Path(raw_path)
                    if not desc_target.is_absolute():
                        desc_target = user_dir / raw_path
                    if desc_target.resolve() == managed.resolve():
                        return normalize_launcher_id(child.name)
                except OSError:
                    continue
    except OSError:
        pass
    if candidates:
        return normalize_launcher_id(candidates[0].name)
    return ""


def _descriptor_exists(user_dir: Path, launcher_id: str) -> bool:
    ident = normalize_launcher_id(launcher_id)
    if not ident:
        return False
    path = user_dir / ident
    try:
        return path.is_file()
    except OSError:
        return False


def map_to_launcher_id(
    ref: ParadoxModRef,
    *,
    user_dir: Path,
) -> ParadoxMapping:
    """SMM token → Paradox ``gameRegistryId`` / ``dlc_load.json`` entry."""
    steam_ws = _steam_workspace_id(ref)
    launcher_id = ""
    if steam_ws:
        launcher_id = workshop_launcher_id(steam_ws)
    elif str(ref.platform or "").strip().lower() != PLATFORM_STEAM:
        launcher_id = _local_descriptor_launcher_id(ref, user_dir)
    if not launcher_id:
        return ParadoxMapping(
            token=ref.token,
            launcher_id="",
            available=False,
            reason="unresolved_launcher_id",
        )
    if not _descriptor_exists(user_dir, launcher_id):
        return ParadoxMapping(
            token=ref.token,
            launcher_id=launcher_id,
            available=False,
            reason="missing_launcher_descriptor",
        )
    return ParadoxMapping(
        token=ref.token,
        launcher_id=launcher_id,
        available=True,
    )


def _launcher_id_for_ref(ref: ParadoxModRef, *, user_dir: Path) -> str:
    steam_ws = _steam_workspace_id(ref)
    if steam_ws:
        return workshop_launcher_id(steam_ws)
    return map_to_launcher_id(ref, user_dir=user_dir).launcher_id


def deployed_load_order_tokens(
    db: DatabaseManager | None = None,
    *,
    app_id: int | str,
) -> list[str]:
    """SMM tokens with ``deploy_status=deployed``, in canonical load-order sequence.

    This is Sort Mode membership. Never reads ``dlc_load.json`` or ``ugc_*.mod``.
    Undeployed ids are not members of the canonical order.
    """
    return resolved_load_order(db, app_id=app_id)


def enabled_load_order_tokens(
    db: DatabaseManager | None = None,
    *,
    app_id: int | str,
    user_dir: str | Path | None = None,
) -> list[str]:
    """Read-only map of launcher ``enabled_mods`` (A) to SMM tokens.

    External execution state only. Never Sort Mode membership. Never writes
    ``deploy_status``. Empty ``enabled_mods`` returns ``[]``.
    """
    database = db if db is not None else get_db()
    docs = resolve_paradox_user_dir(database, app_id=app_id, user_dir=user_dir)
    payload = _load_json_object(docs / DLC_LOAD_FILENAME)
    raw_enabled = payload.get("enabled_mods")
    if not isinstance(raw_enabled, list):
        return []
    wanted: list[str] = []
    seen_ids: set[str] = set()
    for item in raw_enabled:
        lid = normalize_launcher_id(item)
        if not lid or lid in seen_ids:
            continue
        seen_ids.add(lid)
        wanted.append(lid)
    if not wanted:
        return []
    by_launcher: dict[str, str] = {}
    for ref in list_installed_paradox_mods(database, app_id=app_id):
        if not ref.internal_id:
            continue
        lid = normalize_launcher_id(_launcher_id_for_ref(ref, user_dir=docs))
        if lid and lid not in by_launcher:
            by_launcher[lid] = ref.internal_id
    out: list[str] = []
    seen_tok: set[str] = set()
    for lid in wanted:
        tok = canon_internal_id(by_launcher.get(lid, ""))
        if not tok or tok in seen_tok:
            continue
        seen_tok.add(tok)
        out.append(tok)
    return out


def resolve_paradox_user_dir(
    db: DatabaseManager | None = None,
    *,
    app_id: int | str,
    user_dir: str | Path | None = None,
) -> Path:
    if user_dir is not None and str(user_dir).strip():
        return Path(user_dir).expanduser()
    game = paradox_game_for_app_id(app_id)
    database = db if db is not None else get_db()
    try:
        cfg = database.get_game_deploy_config(game.app_id if game is not None else _coerce_app_id(app_id))
    except Exception:  # noqa: BLE001
        cfg = None
    extra = ""
    if cfg is not None:
        extra = str(getattr(cfg, "mod_path", "") or "").strip()
    expected_name = game.user_dir_name if game is not None else ""
    if extra:
        candidate = Path(extra).expanduser()
        try:
            if (candidate / DLC_LOAD_FILENAME).is_file() or (
                expected_name and candidate.name == expected_name
            ):
                return candidate
        except OSError:
            pass
    if game is not None:
        return default_paradox_user_dir(game.app_id)
    return Path.home() / PARADOX_INTERACTIVE_REL


def resolve_stellaris_user_dir(
    db: DatabaseManager | None = None,
    *,
    user_dir: str | Path | None = None,
) -> Path:
    return resolve_paradox_user_dir(db, app_id=STELLARIS_APP_ID, user_dir=user_dir)


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, TypeError):
        logger.warning("Paradox launcher JSON unreadable: %s", path, exc_info=True)
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json_object(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def is_smm_managed_launcher_entry(raw: object, managed_ids: set[str]) -> bool:
    """True only when ``raw`` maps to a launcher id SMM already owns."""
    key = normalize_launcher_id(raw)
    if not key:
        return False
    owned = {normalize_launcher_id(item) for item in managed_ids}
    owned.discard("")
    return key in owned


def non_smm_enabled_entries(
    current: list[object],
    *,
    managed_ids: set[str],
) -> list[object]:
    """DLC / unknown / non-SMM entries in original relative order."""
    return [
        item
        for item in current
        if not is_smm_managed_launcher_entry(item, managed_ids)
    ]


def merge_paradox_enabled_mods(
    current: list[object],
    *,
    managed_ids: set[str],
    enabled_ordered: list[str],
) -> list[object]:
    """Replace only SMM-managed Mod entries; keep every other original item.

    Non-SMM values, count, and relative order are preserved. SMM Mods are
    emitted as one subsequence at the first SMM-managed slot (or appended
    when the original list has no SMM slot). Unknown ``mod/`` entries that
    are not in ``managed_ids`` are kept.
    """
    owned = {normalize_launcher_id(item) for item in managed_ids}
    owned.discard("")
    smm_subseq: list[str] = []
    seen: set[str] = set()
    for ident in enabled_ordered:
        key = normalize_launcher_id(ident)
        if not key or key in seen:
            continue
        seen.add(key)
        smm_subseq.append(key)

    out: list[object] = []
    inserted = False
    for raw in current:
        if is_smm_managed_launcher_entry(raw, owned):
            if not inserted:
                out.extend(smm_subseq)
                inserted = True
            continue
        out.append(raw)
    if not inserted:
        out.extend(smm_subseq)
    return out


merge_stellaris_enabled_mods = merge_paradox_enabled_mods
merge_enabled_mods = merge_paradox_enabled_mods


def _connect_launcher_db(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        uri = path.resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
    else:
        con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def _active_playset_id(con: sqlite3.Connection) -> str | None:
    """Current active playset only. No inactive fallback."""
    row = con.execute(
        "SELECT id FROM playsets WHERE isActive = 1 ORDER BY createdOn DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    token = str(row["id"] or "").strip()
    return token or None


def _enabled_registry_ids(con: sqlite3.Connection, playset_id: str) -> list[str]:
    rows = con.execute(
        """
        SELECT m.gameRegistryId
        FROM playsets_mods pm
        JOIN mods m ON m.id = pm.modId
        WHERE pm.playsetId = ? AND CAST(pm.enabled AS INTEGER) = 1
        ORDER BY pm.position
        """,
        (playset_id,),
    ).fetchall()
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        registry = normalize_launcher_id(row["gameRegistryId"])
        if not registry or registry in seen:
            continue
        seen.add(registry)
        out.append(registry)
    return out


def get_paradox_effective_state(
    db: DatabaseManager | None = None,
    *,
    app_id: int | str,
    user_dir: str | Path | None = None,
) -> ParadoxEffectiveState:
    """Launcher effective export from the active playset.

    Does not read ``dlc_load.json``. No active playset yields an empty list.
    """
    empty = ParadoxEffectiveState()
    game = paradox_game_for_app_id(app_id)
    if game is None and not (user_dir is not None and str(user_dir).strip()):
        return empty
    docs = resolve_paradox_user_dir(
        db,
        app_id=game.app_id if game is not None else app_id,
        user_dir=user_dir,
    )
    sqlite_path = docs / LAUNCHER_DB_FILENAME
    if not sqlite_path.is_file():
        return empty
    try:
        con = _connect_launcher_db(sqlite_path, readonly=True)
    except sqlite3.Error as exc:
        return ParadoxEffectiveState(read_error=f"launcher-v2.sqlite: {exc}")
    try:
        playset_id = _active_playset_id(con)
        if not playset_id:
            return ParadoxEffectiveState()
        return ParadoxEffectiveState(
            active_playset_id=playset_id,
            effective_launcher_ids=_enabled_registry_ids(con, playset_id),
        )
    except sqlite3.Error as exc:
        return ParadoxEffectiveState(read_error=f"launcher-v2.sqlite: {exc}")
    finally:
        con.close()


def paradox_resilience_status(*, deployed_count: int, is_effective: bool) -> str:
    """Derived label. Not a stored state machine."""
    if deployed_count <= 0:
        return RESILIENCE_NOT_DEPLOYED
    if is_effective:
        return RESILIENCE_DEPLOYED_EFFECTIVE
    return RESILIENCE_DEPLOYED_NOT_EFFECTIVE


def _projection_is_effective(
    *,
    intended_ids: list[str],
    unresolved: list[str],
    effective_ids: list[str],
    active_playset_id: str | None,
    deployed_count: int,
) -> bool:
    if deployed_count <= 0 or not active_playset_id or unresolved or not intended_ids:
        return False
    present = set(effective_ids)
    return all(item in present for item in intended_ids)


def paradox_playset_notice_path() -> Path:
    """SMM-owned prompt marker. Never the Launcher database."""
    override = os.environ.get("SMM_PARADOX_PLAYSET_NOTICE", "").strip()
    if override:
        return Path(override)
    if os.environ.get("PYTEST_CURRENT_TEST"):
        token = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            os.environ.get("PYTEST_CURRENT_TEST", "pytest"),
        )[:120]
        root = Path(tempfile.gettempdir()) / "smm-paradox-playset-notice"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{token}.json"
    from core.paths import config_dir

    return config_dir() / PARADOX_PLAYSET_NOTICE_FILENAME


def _load_notice_keys(path: Path) -> dict[str, bool]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): bool(value) for key, value in raw.items()}


def _save_notice_keys(path: Path, keys: dict[str, bool]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _clear_notice_keys(keys: dict[str, bool], app_id: int) -> None:
    prefix = f"{app_id}:"
    for key in list(keys):
        if key.startswith(prefix):
            del keys[key]


def deployed_paradox_mod_count(
    db: DatabaseManager | None = None,
    *,
    app_id: int | str,
) -> int:
    return sum(
        1
        for ref in list_installed_paradox_mods(db, app_id=app_id)
        if ref.deployed
    )


def consider_paradox_playset_notice(
    db: DatabaseManager | None = None,
    *,
    app_id: int | str,
    user_dir: str | Path | None = None,
    notice_path: str | Path | None = None,
) -> ParadoxPlaysetNotice:
    """Prompt once per inactive episode. Does not change Launcher rows.

    Sort must not call this. An active playset or an empty deployed set
    clears the marker. A later return to no-active prompts again.
    """
    aid = _coerce_app_id(app_id)
    path = Path(notice_path) if notice_path is not None else paradox_playset_notice_path()
    keys = _load_notice_keys(path)
    deployed = deployed_paradox_mod_count(db, app_id=aid)
    effective = get_paradox_effective_state(db, app_id=aid, user_dir=user_dir)
    if deployed <= 0 or effective.active_playset_id:
        _clear_notice_keys(keys, aid)
        _save_notice_keys(path, keys)
        return ParadoxPlaysetNotice()
    marker = f"{aid}:{NO_ACTIVE_PLAYSET_SENTINEL}"
    if keys.get(marker):
        return ParadoxPlaysetNotice()
    keys[marker] = True
    _save_notice_keys(path, keys)
    return ParadoxPlaysetNotice(
        should_prompt=True,
        message=PARADOX_INACTIVE_PLAYSET_MESSAGE,
    )


def collect_paradox_playset_notices(
    db: DatabaseManager | None = None,
    *,
    user_dir: str | Path | None = None,
    notice_path: str | Path | None = None,
) -> list[str]:
    """Startup read. One message per game that newly has no active playset."""
    messages: list[str] = []
    for game in PARADOX_LAUNCHER_GAMES:
        notice = consider_paradox_playset_notice(
            db,
            app_id=game.app_id,
            user_dir=user_dir,
            notice_path=notice_path,
        )
        if notice.should_prompt and notice.message:
            messages.append(notice.message)
    return messages


def _sync_playsets_mods(
    sqlite_path: Path,
    *,
    playset_id: str,
    enabled_ordered: list[str],
    managed_ids: set[str],
) -> None:
    """Update enabled/position on one active playset's existing rows. Never INSERT."""
    if not sqlite_path.is_file() or not playset_id:
        return
    enabled_index = {ident: i for i, ident in enumerate(enabled_ordered)}
    con = sqlite3.connect(str(sqlite_path))
    try:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        rows = cur.execute(
            """
            SELECT m.id AS mod_pk, m.gameRegistryId, pm.enabled, pm.position
            FROM playsets_mods pm
            JOIN mods m ON m.id = pm.modId
            WHERE pm.playsetId = ?
            """,
            (playset_id,),
        ).fetchall()
        for row in rows:
            registry = normalize_launcher_id(row["gameRegistryId"])
            if not registry or registry not in managed_ids:
                continue
            enabled = 1 if registry in enabled_index else 0
            position = int(enabled_index.get(registry, row["position"] or 0) or 0)
            cur.execute(
                """
                UPDATE playsets_mods
                SET enabled = ?, position = ?
                WHERE playsetId = ? AND modId = ?
                """,
                (enabled, position, playset_id, row["mod_pk"]),
            )
        con.commit()
    finally:
        con.close()


def sync_paradox_launcher(
    db: DatabaseManager | None = None,
    *,
    app_id: int | str,
    user_dir: str | Path | None = None,
) -> ParadoxSyncReport:
    """Write the Direct EXE list, then project onto the active playset only.

    ``dlc_load.json`` follows deployed mods and the order file. It does not
    consult playset activation. Playset rows change only when one playset
    is active. ``direct_dlc_load_written`` is the file write.
    ``playset_written`` is the active-playset update. ``is_effective`` is the
    Launcher export, which stays empty when no playset is active.
    B + order file replace only SMM-owned ``mod/ugc_*.mod`` entries.
    DLC / unknown / non-SMM items are never deleted or reordered.
    Does not adopt A into B. Never mints missing ``ugc_*.mod`` files.
    """
    report = ParadoxSyncReport()
    game = paradox_game_for_app_id(app_id)
    if game is None:
        return report
    database = db if db is not None else get_db()
    docs = resolve_paradox_user_dir(database, app_id=game.app_id, user_dir=user_dir)
    installed = list_installed_paradox_mods(database, app_id=game.app_id)
    refs = {ref.internal_id: ref for ref in installed if ref.internal_id}
    order = merge_deployed_order(
        load_saved_order(app_id=game.app_id),
        _deployed_order_tokens(database, app_id=game.app_id),
    )
    managed_ids: set[str] = set()
    # Every SMM launcher id, deployed or not, so an undeploy strips enabled_mods
    # without putting that Mod back into canonical membership.
    for ref in installed:
        owned_id = normalize_launcher_id(_launcher_id_for_ref(ref, user_dir=docs))
        if owned_id:
            managed_ids.add(owned_id)
    enabled_ordered: list[str] = []
    deployed_count = 0
    deployed_unresolved: list[str] = []
    for token in order:
        ref = refs.get(token)
        if ref is None or not ref.deployed:
            continue
        deployed_count += 1
        mapping = map_to_launcher_id(ref, user_dir=docs)
        if not mapping.available:
            report.unresolved.append(token)
            deployed_unresolved.append(token)
            continue
        managed_ids.add(mapping.launcher_id)
        enabled_ordered.append(mapping.launcher_id)

    def _finish(state: ParadoxEffectiveState) -> ParadoxSyncReport:
        report.active_playset_id = state.active_playset_id
        report.effective_launcher_ids = list(state.effective_launcher_ids)
        if state.read_error:
            report.errors.append(state.read_error)
        report.is_effective = _projection_is_effective(
            intended_ids=enabled_ordered,
            unresolved=deployed_unresolved,
            effective_ids=report.effective_launcher_ids,
            active_playset_id=report.active_playset_id,
            deployed_count=deployed_count,
        )
        report.resilience_status = paradox_resilience_status(
            deployed_count=deployed_count,
            is_effective=report.is_effective,
        )
        return report

    before = get_paradox_effective_state(
        database, app_id=game.app_id, user_dir=docs
    )
    dlc_path = docs / DLC_LOAD_FILENAME
    payload = _load_json_object(dlc_path)
    current_enabled = payload.get("enabled_mods")
    if not isinstance(current_enabled, list):
        current_enabled = []
    new_enabled = merge_paradox_enabled_mods(
        list(current_enabled),
        managed_ids=managed_ids,
        enabled_ordered=enabled_ordered,
    )
    payload["enabled_mods"] = new_enabled
    try:
        _write_json_object(dlc_path, payload)
        report.direct_dlc_load_written = True
        report.written = True
        report.enabled_launcher_ids = list(enabled_ordered)
    except OSError as exc:
        report.errors.append(f"dlc_load.json: {exc}")
        logger.warning(
            "Paradox dlc_load.json write failed game_id=%s",
            game.game_id,
            exc_info=True,
        )
    if before.active_playset_id and not before.read_error:
        try:
            _sync_playsets_mods(
                docs / LAUNCHER_DB_FILENAME,
                playset_id=before.active_playset_id,
                enabled_ordered=enabled_ordered,
                managed_ids=managed_ids,
            )
            report.playset_written = True
        except sqlite3.Error as exc:
            report.errors.append(f"launcher-v2.sqlite: {exc}")
            logger.warning(
                "Paradox playsets_mods sync failed game_id=%s",
                game.game_id,
                exc_info=True,
            )
        after = get_paradox_effective_state(
            database, app_id=game.app_id, user_dir=docs
        )
        return _finish(after)
    return _finish(before)


def sync_stellaris_launcher(
    db: DatabaseManager | None = None,
    *,
    user_dir: str | Path | None = None,
) -> ParadoxSyncReport:
    return sync_paradox_launcher(db, app_id=STELLARIS_APP_ID, user_dir=user_dir)
