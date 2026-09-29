"""Baldur's Gate 3 load-order backend: canonical ``internal_id[]`` + lsx projection.

Canonical source is ``config/load_order/bg3.json`` (Frozen ``internal_id`` only).
Projection onto Patch 8 ``modsettings.lsx`` uses Stage 1 resolver + projector.
This module does not implement a second LSPK reader or splice XML itself.

Undeploy cleanup is ``apply_bg3_undeploy_order``: resolve UUID from the still-
present deploy manifest / pak, then ``_commit_order(..., remove_uuids=)``.
It does not guess stale UUIDs, touch unmanaged nodes, or delete GustavX.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager, get_db
from core.mod_platform import is_baldurs_gate_3_game
from core.paths import load_order_dir
from services.bg3_modsettings import (
    GUSTAVX_UUID,
    Bg3ModsettingsError,
    MissingInsertion,
    ModuleShortDescValues,
    parse_bg3_modsettings,
    project_bg3_modsettings,
    short_desc_from_metadata,
)
from services.bg3_pak import (
    BG3ModMetadata,
    Bg3PakError,
    deployed_pak_paths_from_manifest,
    resolve_bg3_mod_metadata,
)
from services.deploy_rules.manifest import load_manifest
from services.info_sidecar import load_info_sidecar
from services.paradox_activation import (
    ORDER_MOVE_BOTTOM,
    ORDER_MOVE_DOWN,
    ORDER_MOVE_TOP,
    ORDER_MOVE_UP,
    _move_order_token,
)
from services.wh3_activation import canon_internal_id, merge_deployed_order, move_in_order

logger = logging.getLogger(__name__)

BG3_APP_ID = 1086940
BG3_ORDER_FILENAME = "bg3.json"
CAPABILITY_NATIVE_MODSETTINGS_ORDER = "native_modsettings_order"

_ENV_ORDER = "SMM_BG3_ORDER"
_ENV_MODSETTINGS = "SMM_BG3_MODSETTINGS"


class Bg3OrderError(Exception):
    """Structured BG3 order failure. Never a guessed UUID / identity."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.code = str(code)
        self.fields = dict(fields)

    def __str__(self) -> str:
        extra = " ".join(f"{k}={v}" for k, v in self.fields.items())
        if extra:
            return f"{self.code}: {self.args[0]} ({extra})"
        return f"{self.code}: {self.args[0]}"


@dataclass(frozen=True)
class Bg3UnresolvedMod:
    internal_id: str
    code: str
    message: str
    fields: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Bg3SortableMod:
    internal_id: str
    uuids: tuple[str, ...]
    metas: tuple[BG3ModMetadata, ...]
    pak_paths: tuple[str, ...]


@dataclass(frozen=True)
class Bg3MembershipReport:
    sortable: tuple[Bg3SortableMod, ...]
    unresolved: tuple[Bg3UnresolvedMod, ...]
    skipped_custom: tuple[str, ...]


def is_bg3_order_app(app_id: int | str = 0, game_name: str = "") -> bool:
    if is_baldurs_gate_3_game(game_name, app_id):
        return True
    from services.deploy_rules.game_capabilities import supports_game_capability

    return supports_game_capability(app_id, CAPABILITY_NATIVE_MODSETTINGS_ORDER)


def bg3_order_path() -> Path:
    override = str(os.environ.get(_ENV_ORDER) or "").strip()
    if override:
        return Path(override)
    return load_order_dir() / BG3_ORDER_FILENAME


def default_bg3_modsettings_path() -> Path:
    local = str(os.environ.get("LOCALAPPDATA") or "").strip()
    return (
        Path(local)
        / "Larian Studios"
        / "Baldur's Gate 3"
        / "PlayerProfiles"
        / "Public"
        / "modsettings.lsx"
    )


def bg3_modsettings_path() -> Path:
    override = str(os.environ.get(_ENV_MODSETTINGS) or "").strip()
    if override:
        return Path(override)
    return default_bg3_modsettings_path()


def _norm_uuid(raw: object) -> str:
    return str(raw or "").strip().lower()


def _attr(obj: object, *names: str) -> str:
    if obj is None:
        return ""
    if isinstance(obj, Mapping):
        for name in names:
            text = str(obj.get(name) or "").strip()
            if text:
                return text
    for name in names:
        text = str(getattr(obj, name, "") or "").strip()
        if text:
            return text
    return ""


def sort_token(obj: object) -> str:
    """Load-order token: Frozen ``internal_id`` only."""
    return canon_internal_id(_attr(obj, "internal_id", "id"))


def load_saved_order() -> list[str]:
    path = bg3_order_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    if not isinstance(raw, dict):
        return []
    items = raw.get("order")
    if not isinstance(items, list):
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


def save_saved_order(internal_ids: list[str]) -> None:
    path = bg3_order_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in internal_ids:
        mid = canon_internal_id(raw)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        ordered.append(mid)
    payload = json.dumps({"order": ordered}, ensure_ascii=False, indent=2) + "\n"
    path.write_text(payload, encoding="utf-8")


def _is_deployed_row(row: Mapping[str, Any]) -> bool:
    if bool(row.get("deployed")):
        return True
    return str(row.get("deploy_status") or "").strip() == DEPLOY_STATUS_DEPLOYED


def _list_deployed_rows(
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    database = db if db is not None else get_db()
    handles: list[str] = []
    seen: set[str] = set()
    for raw in list(database.list_deployed_mod_ids_for_app(BG3_APP_ID)) + list(
        database.list_deployed_mod_ids_for_library_game(
            BG3_APP_ID, library_root=library_root
        )
    ):
        key = str(raw or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        handles.append(key)
    by_pk: dict[str, dict[str, Any]] = {}
    by_iid: dict[str, dict[str, Any]] = {}
    for row in database.list_mod_list_items(app_id=BG3_APP_ID):
        pk = str(row.get("mod_id") or "").strip()
        iid = canon_internal_id(row.get("internal_id") or "")
        if pk:
            by_pk[pk] = row
        if iid:
            by_iid[iid] = row
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for handle in handles:
        row = by_pk.get(handle) or by_iid.get(handle)
        if row is None:
            extra = database.list_mod_list_items(mod_id=handle)
            row = extra[0] if extra else None
        if row is None or not _is_deployed_row(row):
            continue
        iid = canon_internal_id(row.get("internal_id") or "")
        if not iid or iid in used:
            continue
        used.add(iid)
        out.append(row)
    return out


def _managed_path(row: Mapping[str, Any]) -> Path | None:
    raw = str(row.get("managed_path") or row.get("last_known_path") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.exists() else path


def _duplicate_uuids_in_modsettings() -> set[str]:
    path = bg3_modsettings_path()
    if not path.is_file():
        return set()
    try:
        _version, nodes = parse_bg3_modsettings(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, Bg3ModsettingsError):
        return set()
    counts: dict[str, int] = {}
    for node in nodes:
        uuid = _norm_uuid(node.uuid)
        if uuid:
            counts[uuid] = counts.get(uuid, 0) + 1
    return {uuid for uuid, count in counts.items() if count > 1}


def inspect_bg3_membership(
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> Bg3MembershipReport:
    """Deployed pak Mods with a resolvable meta.lsx UUID. No library walk."""
    sortable: list[Bg3SortableMod] = []
    unresolved: list[Bg3UnresolvedMod] = []
    skipped_custom: list[str] = []
    uuid_owners: dict[str, list[str]] = {}
    pending: list[Bg3SortableMod] = []

    for row in _list_deployed_rows(db, library_root=library_root):
        iid = canon_internal_id(row.get("internal_id") or "")
        if not iid:
            continue
        managed = _managed_path(row)
        sidecar = load_info_sidecar(managed) if managed is not None else None
        if sidecar is not None and str(sidecar.custom_deploy_path or "").strip():
            skipped_custom.append(iid)
            continue
        if managed is None or not managed.exists():
            unresolved.append(
                Bg3UnresolvedMod(iid, "MissingPak", "managed folder is missing")
            )
            continue
        manifest = load_manifest(managed)
        if manifest is None:
            skipped_custom.append(iid)
            continue
        paks = deployed_pak_paths_from_manifest(manifest)
        if not paks:
            skipped_custom.append(iid)
            continue
        metas: list[BG3ModMetadata] = []
        paths: list[str] = []
        failed: Bg3UnresolvedMod | None = None
        for pak in paks:
            if not pak.is_file():
                continue
            try:
                meta = resolve_bg3_mod_metadata(pak)
            except Bg3PakError as exc:
                failed = Bg3UnresolvedMod(
                    iid,
                    exc.code,
                    str(exc.args[0] if exc.args else exc),
                    dict(exc.fields),
                )
                if exc.code == "UnsupportedCompression":
                    unresolved.append(failed)
                    failed = None
                    metas = []
                    break
                continue
            uuid = _norm_uuid(meta.uuid)
            if not uuid or uuid == _norm_uuid(GUSTAVX_UUID):
                continue
            metas.append(meta)
            paths.append(str(pak))
        if not metas:
            if failed is not None:
                unresolved.append(failed)
            elif any(exc.code == "UnsupportedCompression" for exc in unresolved if exc.internal_id == iid):
                pass
            else:
                unresolved.append(
                    Bg3UnresolvedMod(iid, "MissingPak", "no resolvable deployed pak")
                )
            continue
        uuids = tuple(_norm_uuid(m.uuid) for m in metas)
        pending.append(
            Bg3SortableMod(
                internal_id=iid,
                uuids=uuids,
                metas=tuple(metas),
                pak_paths=tuple(paths),
            )
        )
        for uuid in uuids:
            uuid_owners.setdefault(uuid, []).append(iid)

    ambiguous: set[str] = set()
    for uuid, owners in uuid_owners.items():
        unique = list(dict.fromkeys(owners))
        if len(unique) > 1:
            ambiguous.update(unique)
            for owner in unique:
                unresolved.append(
                    Bg3UnresolvedMod(
                        owner,
                        "DuplicateUuid",
                        "BG3 UUID maps to more than one SMM entity",
                        {"uuid": uuid},
                    )
                )
    for member in pending:
        if member.internal_id in ambiguous:
            continue
        sortable.append(member)
    lsx_dupes = _duplicate_uuids_in_modsettings()
    if lsx_dupes:
        kept: list[Bg3SortableMod] = []
        for member in sortable:
            hit = [uuid for uuid in member.uuids if uuid in lsx_dupes]
            if hit:
                unresolved.append(
                    Bg3UnresolvedMod(
                        member.internal_id,
                        "DuplicateUuid",
                        "BG3 UUID is duplicated in modsettings.lsx; refusing to guess a slot",
                        {"uuid": hit[0]},
                    )
                )
                continue
            kept.append(member)
        sortable = kept
    return Bg3MembershipReport(
        sortable=tuple(sortable),
        unresolved=tuple(unresolved),
        skipped_custom=tuple(skipped_custom),
    )


def list_sortable_bg3_mods(
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> list[Bg3SortableMod]:
    return list(inspect_bg3_membership(db, library_root=library_root).sortable)


def get_sortable_mods(
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> list[str]:
    return [m.internal_id for m in list_sortable_bg3_mods(db, library_root=library_root)]


def _members_by_id(
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> dict[str, Bg3SortableMod]:
    return {
        m.internal_id: m
        for m in list_sortable_bg3_mods(db, library_root=library_root)
    }


def initialize_order_from_modsettings(
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> list[str]:
    members = list_sortable_bg3_mods(db, library_root=library_root)
    uuid_to_iids: dict[str, list[str]] = {}
    for member in members:
        for uuid in member.uuids:
            uuid_to_iids.setdefault(uuid, []).append(member.internal_id)
    path = bg3_modsettings_path()
    if not path.is_file():
        return [m.internal_id for m in members]
    try:
        text = path.read_text(encoding="utf-8")
        _version, nodes = parse_bg3_modsettings(text)
    except (OSError, UnicodeDecodeError, Bg3ModsettingsError):
        logger.debug("BG3 initial order: cannot parse modsettings.lsx", exc_info=True)
        return [m.internal_id for m in members]
    order: list[str] = []
    seen: set[str] = set()
    ambiguous: set[str] = set()
    for node in nodes[1:]:
        owners = list(dict.fromkeys(uuid_to_iids.get(_norm_uuid(node.uuid), ())))
        if len(owners) != 1:
            if len(owners) > 1:
                ambiguous.update(owners)
            continue
        iid = owners[0]
        if iid in seen or iid in ambiguous:
            continue
        seen.add(iid)
        order.append(iid)
    for member in members:
        if member.internal_id not in seen and member.internal_id not in ambiguous:
            order.append(member.internal_id)
    return order


def resolved_load_order(
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> list[str]:
    sortable = get_sortable_mods(db, library_root=library_root)
    saved = load_saved_order()
    if not bg3_order_path().is_file():
        saved = initialize_order_from_modsettings(db, library_root=library_root)
    return merge_deployed_order(saved, sortable)


def persist_load_order(
    internal_ids: list[str] | None = None,
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> list[str]:
    sortable = get_sortable_mods(db, library_root=library_root)
    if not bg3_order_path().is_file():
        seeded = initialize_order_from_modsettings(db, library_root=library_root)
        base = seeded if not internal_ids else merge_deployed_order(internal_ids, seeded)
    else:
        base = list(internal_ids) if internal_ids is not None else load_saved_order()
    merged = merge_deployed_order(base, sortable)
    save_saved_order(merged)
    return merged


def resolve_uuid_sequence(
    internal_ids: list[str],
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> tuple[list[str], dict[str, ModuleShortDescValues]]:
    members = _members_by_id(db, library_root=library_root)
    order_uuids: list[str] = []
    missing_nodes: dict[str, ModuleShortDescValues] = {}
    seen: set[str] = set()
    for iid in internal_ids:
        member = members.get(canon_internal_id(iid))
        if member is None:
            raise Bg3OrderError(
                "MissingPak",
                "canonical internal_id is not a sortable deployed BG3 Mod",
                internal_id=iid,
            )
        for uuid, meta in zip(member.uuids, member.metas):
            if uuid in seen:
                raise Bg3OrderError(
                    "DuplicateUuid",
                    "canonical order maps two slots onto the same BG3 UUID",
                    uuid=uuid,
                )
            seen.add(uuid)
            order_uuids.append(uuid)
            missing_nodes[uuid] = short_desc_from_metadata(meta)
    return order_uuids, missing_nodes


def resolve_entity_bg3_uuids(
    internal_id: str,
    managed_path: str | Path,
) -> tuple[str, ...]:
    """Resolve SMM-owned BG3 UUIDs from this entity's deploy manifest paks.

    Requires the deployed ``.pak`` to still exist. Never invents a UUID and
    never reads ``workspace_id``. GustavX UUID is returned if present so the
    caller can refuse removal.
    """
    iid = canon_internal_id(internal_id)
    if not iid:
        raise Bg3OrderError("MissingIdentity", "internal_id is required")
    root = Path(managed_path)
    sidecar = load_info_sidecar(root)
    if sidecar is not None and str(sidecar.custom_deploy_path or "").strip():
        raise Bg3OrderError(
            "CustomDeploy",
            "custom deploy has no BG3 pak UUID",
            internal_id=iid,
        )
    manifest = load_manifest(root)
    if manifest is None:
        raise Bg3OrderError(
            "MissingPak",
            "no deploy manifest; cannot resolve BG3 UUID",
            internal_id=iid,
        )
    paks = deployed_pak_paths_from_manifest(manifest)
    if not paks:
        raise Bg3OrderError(
            "MissingPak",
            "deploy manifest has no pak targets",
            internal_id=iid,
        )
    uuids: list[str] = []
    seen: set[str] = set()
    last_pak_error: Bg3PakError | None = None
    for pak in paks:
        if not pak.is_file():
            continue
        try:
            meta = resolve_bg3_mod_metadata(pak)
        except Bg3PakError as exc:
            last_pak_error = exc
            continue
        uuid = _norm_uuid(meta.uuid)
        if not uuid:
            continue
        if uuid not in seen:
            seen.add(uuid)
            uuids.append(uuid)
    if not uuids:
        if last_pak_error is not None:
            raise Bg3OrderError(
                last_pak_error.code,
                str(last_pak_error.args[0] if last_pak_error.args else last_pak_error),
                internal_id=iid,
                **dict(last_pak_error.fields),
            )
        raise Bg3OrderError(
            "MissingPak",
            "no resolvable meta.lsx UUID in deployed paks",
            internal_id=iid,
        )
    return tuple(uuids)


def _duplicate_remove_uuids(remove_uuids: Sequence[str]) -> str:
    live = bg3_modsettings_path()
    if not live.is_file():
        return ""
    try:
        _version, nodes = parse_bg3_modsettings(live.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, Bg3ModsettingsError):
        return ""
    wanted = {_norm_uuid(uuid) for uuid in remove_uuids if _norm_uuid(uuid)}
    counts: dict[str, int] = {}
    for node in nodes:
        uuid = _norm_uuid(node.uuid)
        if uuid in wanted:
            counts[uuid] = counts.get(uuid, 0) + 1
    for uuid, count in counts.items():
        if count > 1:
            return uuid
    return ""


def _commit_order(
    internal_ids: list[str],
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
    project: bool,
    exclude_internal_ids: Sequence[str] = (),
    remove_uuids: Sequence[str] = (),
    create_missing: bool | None = None,
) -> list[str]:
    exclude = {
        canon_internal_id(item)
        for item in exclude_internal_ids
        if canon_internal_id(item)
    }
    sortable = [
        iid
        for iid in get_sortable_mods(db, library_root=library_root)
        if iid not in exclude
    ]
    requested = [
        canon_internal_id(item)
        for item in internal_ids
        if canon_internal_id(item) and canon_internal_id(item) not in exclude
    ]
    merged = merge_deployed_order(requested, sortable)
    if not project:
        save_saved_order(merged)
        return merged

    uuid_order, missing_nodes = resolve_uuid_sequence(
        merged, db, library_root=library_root
    )
    remove = [_norm_uuid(uuid) for uuid in remove_uuids if _norm_uuid(uuid)]
    if any(uuid == _norm_uuid(GUSTAVX_UUID) for uuid in remove):
        raise Bg3OrderError(
            "GustavXProtected",
            "refusing to remove GustavX from BG3 load order",
            uuid=GUSTAVX_UUID,
        )
    dupe = _duplicate_remove_uuids(remove)
    if dupe:
        raise Bg3OrderError(
            "DuplicateManagedUUID",
            "the same managed UUID appears more than once",
            uuid=dupe,
        )
    live = bg3_modsettings_path()
    if not live.is_file():
        raise Bg3OrderError("UnreadableSource", f"modsettings path is missing: {live}")

    order_file = bg3_order_path()
    prev_json = order_file.read_bytes() if order_file.is_file() else None
    tmp_path: Path | None = None
    create = True if create_missing is None else bool(create_missing)
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix="smm_bg3_order_",
            suffix=".lsx",
            dir=str(live.parent),
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        project_bg3_modsettings(
            live,
            tmp_path,
            managed_uuid_order=uuid_order,
            active_managed_uuids=uuid_order,
            create_missing=create,
            missing_insertion=MissingInsertion.AFTER_LAST_MANAGED_SLOT,
            missing_nodes=missing_nodes,
            remove_uuids=remove or None,
        )
        save_saved_order(merged)
        os.replace(tmp_path, live)
        tmp_path = None
    except Exception:
        if prev_json is None:
            if order_file.is_file():
                try:
                    order_file.unlink()
                except OSError:
                    logger.debug("BG3 order rollback: cannot remove json", exc_info=True)
        else:
            try:
                order_file.write_bytes(prev_json)
            except OSError:
                logger.debug("BG3 order rollback: cannot restore json", exc_info=True)
        raise
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                logger.debug("BG3 order: leftover temp lsx", exc_info=True)
    return merged


def apply_bg3_undeploy_order(
    internal_id: str,
    *,
    managed_path: str | Path,
    db: DatabaseManager | None = None,
    library_root: str | Path | None = None,
) -> dict[str, Any]:
    """Drop this entity from ``bg3.json`` and project ``remove_uuids``.

    UUID is resolved from the current deploy manifest / pak. If the entity is
    not in canonical order, unmanaged / missing lsx nodes are left alone.
    GustavX is always refused. Fail-closed when the UUID cannot be resolved
    while the entity is still in ``bg3.json``.
    """
    iid = canon_internal_id(internal_id)
    if not iid:
        raise Bg3OrderError(
            "MissingIdentity",
            "internal_id is required for BG3 undeploy order cleanup",
        )
    root = Path(managed_path)
    sidecar = load_info_sidecar(root) if root.exists() else None
    if sidecar is not None and str(sidecar.custom_deploy_path or "").strip():
        return {
            "skipped": True,
            "reason": "custom_deploy",
            "order": load_saved_order(),
        }

    saved = load_saved_order()
    in_order = iid in saved
    try:
        uuids = resolve_entity_bg3_uuids(iid, root)
    except Bg3OrderError:
        if not in_order:
            return {
                "skipped": True,
                "reason": "not_in_canonical_order",
                "order": saved,
            }
        raise

    if any(uuid == _norm_uuid(GUSTAVX_UUID) for uuid in uuids):
        raise Bg3OrderError(
            "GustavXProtected",
            "refusing to remove GustavX from BG3 load order",
            internal_id=iid,
            uuid=GUSTAVX_UUID,
        )

    if not in_order:
        return {
            "skipped": True,
            "reason": "not_in_canonical_order",
            "order": saved,
        }

    next_order = [item for item in saved if item != iid]
    committed = _commit_order(
        next_order,
        db,
        library_root=library_root,
        project=True,
        exclude_internal_ids=(iid,),
        remove_uuids=uuids,
        create_missing=False,
    )
    logger.info(
        "BG3 undeploy order removed internal_id=%s uuids=%s",
        iid,
        ",".join(uuids),
    )
    return {
        "skipped": False,
        "reason": "removed",
        "order": committed,
        "removed_uuids": list(uuids),
    }


def apply_order_move(
    token: str,
    action: str,
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> list[str]:
    current = resolved_load_order(db, library_root=library_root)
    next_order = _move_order_token(current, token, action)
    return _commit_order(next_order, db, library_root=library_root, project=True)


def apply_card_drop(
    source_id: str,
    target_id: str,
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> list[str]:
    current = resolved_load_order(db, library_root=library_root)
    next_order = move_in_order(current, source_id, target_id)
    return _commit_order(next_order, db, library_root=library_root, project=True)


def sync_projection(
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> None:
    current = resolved_load_order(db, library_root=library_root)
    _commit_order(current, db, library_root=library_root, project=True)


def is_sortable_member(
    index: object,
    db: DatabaseManager | None = None,
    *,
    library_root: str | Path | None = None,
) -> bool:
    token = sort_token(index)
    if not token:
        return False
    return token in set(get_sortable_mods(db, library_root=library_root))
