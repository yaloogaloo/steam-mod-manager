"""Reserved helpers for Mod relationship metadata (importer extension point).

Phase 8: relationships are user-declared only. Importers must not guess.
"""

from __future__ import annotations

from core.db_manager import (
    RELATIONSHIP_DEPENDENCY,
    DatabaseManager,
    get_db,
)


def add_dependency_by_workspace_id(
    owner_mod_pk: int | str,
    workspace_id: str,
    *,
    db: DatabaseManager | None = None,
) -> str:
    """Resolve Workspace ID → Frozen UUID, persist the dependency on SQL PK.

    User input is Workspace ID only. The stored ``mod_relationships`` row
    uses SQLite PK (FK). Does not accept Internal ID as a lookup token.
    Returns the target SQLite PK for DAL callers.
    """
    from services.identity_service import (
        resolve_internal_id_from_workspace_id,
        resolve_mod_pk_from_internal_id,
    )

    manager = db if db is not None else get_db()
    owner = str(owner_mod_pk or "").strip()
    wid = str(workspace_id or "").strip()
    if not owner.isdigit():
        raise ValueError("owner mod_pk is required")
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

    target_uuid = resolve_internal_id_from_workspace_id(
        wid, platform=plat, app_id=aid, db=manager
    )
    if not target_uuid:
        raise LookupError(f"本地库中未找到 Workspace ID：{wid}")
    target = resolve_mod_pk_from_internal_id(target_uuid, db=manager)
    if not target:
        raise LookupError(f"本地库中未找到 Workspace ID：{wid}")
    if target == owner:
        raise ValueError("不能将自身设为依赖")
    manager.add_mod_relationship(owner, target, RELATIONSHIP_DEPENDENCY)
    return target
