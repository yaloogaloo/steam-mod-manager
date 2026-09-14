"""Stellaris (AppID 281990) deployment membership / load order / launcher sync.

Source of truth:

- B: ``mods.deploy_status = deployed`` — Sort Mode membership
- Order: ``config/load_order/stellaris.json`` — full user sequence
- A: ``dlc_load.json`` ``enabled_mods`` — mixed list; SMM rewrites only its
  managed Mod subset (B + order). DLC / unknown / non-SMM entries are kept.

Never copies Workshop content. Never mints Launcher Mods. Never uses SMM
``internal_id`` as a Launcher identifier. Never adopts A into B.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.db_manager import DatabaseManager, get_db
from core.mod_platform import (
    PLATFORM_STEAM,
    STELLARIS_APP_IDS,
    is_internal_mod_id,
    is_stellaris_game,
)
from services.wh3_activation import (
    canon_internal_id,
    display_numbers,
    merge_deployed_order,
    move_in_order,
)

logger = logging.getLogger(__name__)

STELLARIS_APP_ID = next(iter(STELLARIS_APP_IDS))
STELLARIS_ORDER_FILENAME = "stellaris.json"
DLC_LOAD_FILENAME = "dlc_load.json"
LAUNCHER_DB_FILENAME = "launcher-v2.sqlite"
USER_DIR_REL = Path("Documents") / "Paradox Interactive" / "Stellaris"
LAUNCHER_ID_PREFIX = "mod/"
WORKSHOP_LAUNCHER_PREFIX = "mod/ugc_"
WORKSHOP_LAUNCHER_SUFFIX = ".mod"

_DESCRIPTOR_KV_RE = re.compile(r'^([A-Za-z0-9_]+)\s*=\s*"(.*)"\s*$')


def is_stellaris_activation_app(app_id: int | str = 0, game_name: str = "") -> bool:
    """True when this game uses Stellaris launcher activation (App ID 281990)."""
    try:
        aid = int(str(app_id or 0).strip() or 0)
    except (TypeError, ValueError):
        aid = 0
    if aid in STELLARIS_APP_IDS:
        return True
    return bool(game_name) and is_stellaris_game(game_name, aid)


def default_stellaris_user_dir() -> Path:
    return Path.home() / USER_DIR_REL


def _order_path() -> Path:
    from core.paths import load_order_dir

    return load_order_dir() / STELLARIS_ORDER_FILENAME


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


def load_saved_order() -> list[str]:
    """Persisted SMM load-order tokens (may include stale / disabled ids)."""
    path = _order_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    return _parse_order_payload(raw)


def save_saved_order(tokens: list[str]) -> None:
    path = _order_path()
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
class StellarisWorkshopHit:
    workspace_id: str
    path: str
    name: str = ""
    remote_file_id: str = ""
    descriptor_path: str = ""


def workshop_content_root(workshop_path: str | Path | None) -> Path | None:
    """Stellaris Workshop content root: ``.../workshop/content/281990``."""
    raw = str(workshop_path or "").strip()
    if not raw:
        return None
    base = Path(raw).expanduser()
    app = str(STELLARIS_APP_ID)
    try:
        name = base.name
    except OSError:
        return None
    if name == app:
        return base
    if name.lower() == "content":
        return base / app
    return base / "content" / app


def discover_workshop_mods(workshop_path: str | Path | None) -> list[StellarisWorkshopHit]:
    """One-shot Workshop folder listing. Never call from UI refresh."""
    root = workshop_content_root(workshop_path)
    if root is None:
        return []
    try:
        if not root.is_dir():
            return []
        children = list(root.iterdir())
    except OSError:
        return []
    hits: list[StellarisWorkshopHit] = []
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
                parsed = parse_mod_descriptor(desc_path.read_text(encoding="utf-8", errors="replace"))
                title = str(parsed.get("name") or "").strip()
                remote = str(parsed.get("remote_file_id") or remote).strip() or child.name
        except OSError:
            pass
        hits.append(
            StellarisWorkshopHit(
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
class StellarisModRef:
    token: str
    workspace_id: str
    enabled: bool
    deployed: bool
    last_known_path: str
    platform: str = ""
    external_id: str = ""
    title: str = ""
    entity_internal_id: str = ""


@dataclass
class StellarisMapping:
    token: str
    launcher_id: str
    available: bool
    reason: str = ""


@dataclass
class StellarisSyncReport:
    written: bool = False
    enabled_launcher_ids: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _row_to_ref(row: dict[str, Any]) -> StellarisModRef | None:
    token = canon_internal_id(row.get("internal_id") or row.get("mod_id"))
    if not token:
        return None
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
    return StellarisModRef(
        token=token,
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


def list_installed_stellaris_mods(
    db: DatabaseManager | None = None,
) -> list[StellarisModRef]:
    """SMM Stellaris Mods already in the library. No Workshop rescan."""
    database = db if db is not None else get_db()
    out: list[StellarisModRef] = []
    seen: set[str] = set()
    for row in database.list_mod_list_items(app_id=STELLARIS_APP_ID):
        ref = _row_to_ref(row)
        if ref is None or ref.token in seen:
            continue
        seen.add(ref.token)
        out.append(ref)
    return out


def resolved_load_order(db: DatabaseManager | None = None) -> list[str]:
    installed = [m.token for m in list_installed_stellaris_mods(db)]
    return merge_deployed_order(load_saved_order(), installed)


def persist_load_order(
    tokens: list[str],
    db: DatabaseManager | None = None,
) -> list[str]:
    installed = [m.token for m in list_installed_stellaris_mods(db)]
    merged = merge_deployed_order(tokens, installed)
    save_saved_order(merged)
    return merged


def apply_card_drop(
    source_id: str,
    target_id: str,
    db: DatabaseManager | None = None,
) -> list[str]:
    current = resolved_load_order(db)
    next_order = move_in_order(current, source_id, target_id)
    save_saved_order(next_order)
    return next_order


def set_stellaris_enabled(
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


def _steam_workspace_id(ref: StellarisModRef) -> str:
    for cand in (ref.workspace_id, ref.external_id):
        text = str(cand or "").strip()
        if text.isdigit() and not is_internal_mod_id(text):
            return text
    return ""


def _local_descriptor_launcher_id(ref: StellarisModRef, user_dir: Path) -> str:
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
    ref: StellarisModRef,
    *,
    user_dir: Path,
) -> StellarisMapping:
    """SMM token → Paradox ``gameRegistryId`` / ``dlc_load.json`` entry."""
    steam_ws = _steam_workspace_id(ref)
    launcher_id = ""
    if steam_ws:
        launcher_id = workshop_launcher_id(steam_ws)
    elif str(ref.platform or "").strip().lower() != PLATFORM_STEAM:
        launcher_id = _local_descriptor_launcher_id(ref, user_dir)
    if not launcher_id:
        return StellarisMapping(
            token=ref.token,
            launcher_id="",
            available=False,
            reason="unresolved_launcher_id",
        )
    if not _descriptor_exists(user_dir, launcher_id):
        return StellarisMapping(
            token=ref.token,
            launcher_id=launcher_id,
            available=False,
            reason="missing_launcher_descriptor",
        )
    return StellarisMapping(
        token=ref.token,
        launcher_id=launcher_id,
        available=True,
    )


def _launcher_id_for_ref(ref: StellarisModRef, *, user_dir: Path) -> str:
    steam_ws = _steam_workspace_id(ref)
    if steam_ws:
        return workshop_launcher_id(steam_ws)
    return map_to_launcher_id(ref, user_dir=user_dir).launcher_id


def deployed_load_order_tokens(db: DatabaseManager | None = None) -> list[str]:
    """SMM tokens with ``deploy_status=deployed``, in saved load-order sequence.

    This is Sort Mode membership (B). Never reads ``dlc_load.json``.
    Undeployed ids stay in ``stellaris.json`` but are omitted here.
    """
    refs = {ref.token: ref for ref in list_installed_stellaris_mods(db)}
    order = merge_deployed_order(load_saved_order(), list(refs))
    return [tok for tok in order if refs.get(tok) is not None and refs[tok].deployed]


def enabled_load_order_tokens(
    db: DatabaseManager | None = None,
    *,
    user_dir: str | Path | None = None,
) -> list[str]:
    """Read-only map of launcher ``enabled_mods`` (A) to SMM tokens.

    External execution state only. Never Sort Mode membership. Never writes
    ``deploy_status``. Empty ``enabled_mods`` returns ``[]``.
    """
    database = db if db is not None else get_db()
    docs = resolve_stellaris_user_dir(database, user_dir=user_dir)
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
    for ref in list_installed_stellaris_mods(database):
        lid = normalize_launcher_id(_launcher_id_for_ref(ref, user_dir=docs))
        if lid and lid not in by_launcher:
            by_launcher[lid] = ref.token
    out: list[str] = []
    seen_tok: set[str] = set()
    for lid in wanted:
        tok = canon_internal_id(by_launcher.get(lid, ""))
        if not tok or tok in seen_tok:
            continue
        seen_tok.add(tok)
        out.append(tok)
    return out


def resolve_stellaris_user_dir(
    db: DatabaseManager | None = None,
    *,
    user_dir: str | Path | None = None,
) -> Path:
    if user_dir is not None and str(user_dir).strip():
        return Path(user_dir).expanduser()
    database = db if db is not None else get_db()
    try:
        cfg = database.get_game_deploy_config(STELLARIS_APP_ID)
    except Exception:  # noqa: BLE001
        cfg = None
    extra = ""
    if cfg is not None:
        extra = str(getattr(cfg, "mod_path", "") or "").strip()
    if extra:
        candidate = Path(extra).expanduser()
        try:
            if (candidate / DLC_LOAD_FILENAME).is_file() or candidate.name == "Stellaris":
                return candidate
        except OSError:
            pass
    return default_stellaris_user_dir()


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, TypeError):
        logger.warning("Stellaris launcher JSON unreadable: %s", path, exc_info=True)
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


def merge_stellaris_enabled_mods(
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


def merge_enabled_mods(
    current: list[object],
    *,
    managed_ids: set[str],
    enabled_ordered: list[str],
) -> list[object]:
    """Alias for :func:`merge_stellaris_enabled_mods`."""
    return merge_stellaris_enabled_mods(
        current,
        managed_ids=managed_ids,
        enabled_ordered=enabled_ordered,
    )


def _sync_playsets_mods(
    sqlite_path: Path,
    *,
    enabled_ordered: list[str],
    managed_ids: set[str],
) -> None:
    """Update enabled/position on existing playset rows only. Never INSERT."""
    if not sqlite_path.is_file():
        return
    enabled_index = {ident: i for i, ident in enumerate(enabled_ordered)}
    con = sqlite3.connect(str(sqlite_path))
    try:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        playset = cur.execute(
            "SELECT id FROM playsets WHERE isActive = 1 ORDER BY createdOn DESC LIMIT 1"
        ).fetchone()
        if playset is None:
            playset = cur.execute(
                "SELECT id FROM playsets ORDER BY createdOn DESC LIMIT 1"
            ).fetchone()
        if playset is None:
            return
        playset_id = playset["id"]
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


def sync_stellaris_launcher(
    db: DatabaseManager | None = None,
    *,
    user_dir: str | Path | None = None,
) -> StellarisSyncReport:
    """Sync SMM-managed Mod subset into ``dlc_load.json``; playset flags.

    B + ``stellaris.json`` order replace only SMM-owned ``mod/ugc_*.mod``
    entries. DLC / unknown / non-SMM items are never deleted or reordered.
    Does not adopt A into B.
    """
    report = StellarisSyncReport()
    database = db if db is not None else get_db()
    docs = resolve_stellaris_user_dir(database, user_dir=user_dir)
    refs = {ref.token: ref for ref in list_installed_stellaris_mods(database)}
    order = merge_deployed_order(load_saved_order(), list(refs))
    managed_ids: set[str] = set()
    enabled_ordered: list[str] = []
    for token in order:
        ref = refs.get(token)
        if ref is None:
            continue
        owned_id = normalize_launcher_id(_launcher_id_for_ref(ref, user_dir=docs))
        if owned_id:
            managed_ids.add(owned_id)
        mapping = map_to_launcher_id(ref, user_dir=docs)
        if not mapping.available:
            report.unresolved.append(token)
            continue
        managed_ids.add(mapping.launcher_id)
        if ref.deployed:
            enabled_ordered.append(mapping.launcher_id)
    dlc_path = docs / DLC_LOAD_FILENAME
    payload = _load_json_object(dlc_path)
    current_enabled = payload.get("enabled_mods")
    if not isinstance(current_enabled, list):
        current_enabled = []
    new_enabled = merge_stellaris_enabled_mods(
        list(current_enabled),
        managed_ids=managed_ids,
        enabled_ordered=enabled_ordered,
    )
    payload["enabled_mods"] = new_enabled
    try:
        _write_json_object(dlc_path, payload)
        report.written = True
        report.enabled_launcher_ids = list(enabled_ordered)
    except OSError as exc:
        report.errors.append(f"dlc_load.json: {exc}")
        logger.warning("Stellaris dlc_load.json write failed", exc_info=True)
        return report
    try:
        _sync_playsets_mods(
            docs / LAUNCHER_DB_FILENAME,
            enabled_ordered=enabled_ordered,
            managed_ids=managed_ids,
        )
    except sqlite3.Error as exc:
        report.errors.append(f"launcher-v2.sqlite: {exc}")
        logger.warning("Stellaris playsets_mods sync failed", exc_info=True)
    return report
