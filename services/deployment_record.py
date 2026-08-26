"""Deployment Record — named snapshot of deployed Mod IDs (record only).

Not a deploy system. Does not change ``mods.deploy_status`` or Mod rows.
Name is the user-facing unique key per game; ``id`` is internal only.

Architecture boundary (see docs/deployment_record_design.md):
- This service MUST NOT call services/deploy.py or update_mod_deploy_status.
- Snapshot source must match Library: deployed mods in the current game folder
  (including ``app_id=0``), not ``mods.app_id`` alone.
- Writes affect only deployment_records / deployment_record_items.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from core.db_manager import (
    DatabaseManager,
    DeploymentRecord,
    get_db,
)


def _db(db: DatabaseManager | None) -> DatabaseManager:
    return db if db is not None else get_db()


def create_or_update_record(
    app_id: int | str,
    name: str,
    *,
    mod_ids: Iterable[int | str] | None = None,
    game_folder: str | None = None,
    library_root: str | Path | None = None,
    db: DatabaseManager | None = None,
) -> DeploymentRecord:
    """
    Snapshot currently deployed Mods for ``app_id`` under ``name``.

    If ``mod_ids`` is omitted, IDs come from the Library-aligned deployed set
    (game-folder membership + ``deploy_status=deployed``), not ``app_id`` alone.

    If ``(app_id, name)`` already exists, replace items (update).
    Otherwise create a new record. Never creates a duplicate name per game.
    """
    manager = _db(db)
    aid = int(app_id)
    label = str(name or "").strip()
    if not label:
        raise ValueError("deployment record name must be non-empty")
    if mod_ids is None:
        ids = manager.list_deployed_mod_ids_for_library_game(
            aid,
            game_folder=game_folder,
            library_root=library_root,
        )
    else:
        ids = list(mod_ids)
    existing = manager.find_deployment_record_by_name(aid, label)
    if existing is not None:
        return manager.update_deployment_record(existing.id, mod_ids=ids)
    return manager.create_deployment_record(aid, label, ids)


def rename_record(
    record_id: int | str,
    new_name: str,
    *,
    db: DatabaseManager | None = None,
) -> DeploymentRecord:
    """
    Rename a record. Name must be unique within the same game;
    different games may share the same name.
    """
    manager = _db(db)
    label = str(new_name or "").strip()
    if not label:
        raise ValueError("deployment record name must be non-empty")
    existing = manager.get_deployment_record(record_id)
    if existing is None:
        raise LookupError(f"deployment record not found: {record_id}")
    return manager.update_deployment_record(record_id, name=label)


def delete_record(
    record_id: int | str,
    *,
    db: DatabaseManager | None = None,
) -> bool:
    """Delete a record and its items. Does not touch mods or deploy_status."""
    return _db(db).delete_deployment_record(record_id)


def list_records(
    app_id: int | str,
    *,
    db: DatabaseManager | None = None,
) -> list[DeploymentRecord]:
    """List deployment records for one game."""
    return _db(db).list_deployment_records(app_id)


def get_record_mod_ids(
    record_id: int | str,
    *,
    db: DatabaseManager | None = None,
) -> set[str]:
    """Return ``set(mod_id)`` for Filter Context."""
    return _db(db).get_deployment_record_mod_ids(record_id)


def find_record_by_name(
    app_id: int | str,
    name: str,
    *,
    db: DatabaseManager | None = None,
) -> DeploymentRecord | None:
    """Lookup by user-facing name within one game (internal id not exposed)."""
    label = str(name or "").strip()
    if not label:
        return None
    return _db(db).find_deployment_record_by_name(int(app_id), label)


__all__ = (
    "DeploymentRecord",
    "create_or_update_record",
    "rename_record",
    "delete_record",
    "list_records",
    "get_record_mod_ids",
    "find_record_by_name",
)
