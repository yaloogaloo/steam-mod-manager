"""Reserved helpers for Mod relationship metadata (importer extension point).

Phase 8: relationships are user-declared only. Importers must not guess.
Future Nexus/GitHub metadata may call ``apply_declared_relationships``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from core.db_manager import (
    RELATIONSHIP_DEPENDENCY,
    SUPPORTED_RELATIONSHIP_TYPES,
    DatabaseManager,
    get_db,
)


def apply_declared_relationships(
    mod_id: int | str,
    declared: Iterable[Mapping[str, Any]] | None,
    *,
    db: DatabaseManager | None = None,
) -> list[dict[str, Any]]:
    """
    Apply importer-supplied relationship declarations when present.

    Each item: ``{"target_mod_id"|"external_id": ..., "relationship_type": ...}``.

    Currently a no-op when ``declared`` is empty — reserved for future importers.
    Does not modify importer modules in Phase 8.
    """
    if not declared:
        return []
    manager = db if db is not None else get_db()
    applied: list[dict[str, Any]] = []
    for raw in declared:
        if not isinstance(raw, Mapping):
            continue
        rtype = str(raw.get("relationship_type") or "").strip().lower()
        if rtype not in SUPPORTED_RELATIONSHIP_TYPES:
            continue
        tgt = raw.get("target_mod_id") or raw.get("mod_id")
        if tgt is None or not str(tgt).strip().isdigit():
            # external_id resolution reserved for future importer wiring
            continue
        rel = manager.add_mod_relationship(mod_id, tgt, rtype)
        applied.append(rel.as_dict())
    return applied


def add_dependency_by_workspace_id(
    owner_internal_id: int | str,
    workspace_id: str,
    *,
    db: DatabaseManager | None = None,
) -> str:
    """Resolve Workspace ID → Internal ID, then persist the dependency on PK.

    User input is Workspace ID only. The stored ``mod_relationships`` row
    uses Internal ID. Does not accept Internal ID as a lookup token.
    """
    from services.identity_service import resolve_internal_id_from_workspace_id

    manager = db if db is not None else get_db()
    owner = str(owner_internal_id or "").strip()
    wid = str(workspace_id or "").strip()
    if not owner.isdigit():
        raise ValueError("owner internal_id is required")
    if not wid:
        raise ValueError("Workspace ID is required")

    plat = ""
    aid = 0
    try:
        row = manager.get_mod_backup_row(owner) or {}
        plat = str(row.get("platform") or "").strip()
        try:
            aid = int(row.get("app_id") or 0)
        except (TypeError, ValueError):
            aid = 0
    except Exception:  # noqa: BLE001
        pass
    if not plat or aid <= 0:
        info = manager.get_mod_display_info(owner)
        if info is not None:
            plat = plat or str(info.platform or "").strip()
            if aid <= 0:
                try:
                    aid = int(info.app_id or 0)
                except (TypeError, ValueError):
                    aid = 0

    target = resolve_internal_id_from_workspace_id(
        wid, platform=plat, app_id=aid, db=manager
    )
    if not target:
        raise LookupError(f"本地库中未找到 Workspace ID：{wid}")
    if target == owner:
        raise ValueError("不能将自身设为依赖")
    manager.add_mod_relationship(owner, target, RELATIONSHIP_DEPENDENCY)
    return target
