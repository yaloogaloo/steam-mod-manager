"""Registration-only entity removal — never deletes filesystem content.

Steam Mod Manager principle: deleting a registration entity ≠ deleting files.

This service removes SQLite / in-memory registration bindings only:
mods row, tags, relations, deployment items, games row, categories,
library projection cache, cover RAM cache.

It must never call ``shutil.rmtree``, ``os.remove``, ``ModRemover``,
deploy-remove, reconcile cleanup, or any filesystem repair.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.db_manager import DatabaseManager, get_db
from core.paths import project_root

logger = logging.getLogger(__name__)

__all__ = [
    "ModRegistrationRemovalService",
    "RegistrationRemovalResult",
]


@dataclass
class RegistrationRemovalResult:
    success: bool
    entity: str = ""
    id: str = ""
    db_removed: bool = False
    last_known_path: str = ""
    path_was_application_root: bool = False
    filesystem_touched: bool = False
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "entity": self.entity,
            "id": self.id,
            "db_removed": self.db_removed,
            "last_known_path": self.last_known_path,
            "path_was_application_root": self.path_was_application_root,
            "filesystem_touched": self.filesystem_touched,
            "error": self.error,
            "details": dict(self.details),
        }


class ModRegistrationRemovalService:
    """
    Delete system registration entities only.

    Responsibilities: DB rows + projection / cover cache invalidation.
    Non-responsibilities: Mod folders, project tree, backup disk trees,
    deploy undeploy, reconcile.
    """

    def __init__(self, *, db: DatabaseManager | None = None) -> None:
        self._db = db

    def _database(self) -> DatabaseManager:
        return self._db if self._db is not None else get_db()

    def remove_mod_registration(
        self,
        mod_id: int | str,
        *,
        workspace_id: str | int | None = None,
    ) -> RegistrationRemovalResult:
        """Remove one Mod registration. Never deletes disk content."""
        mid = str(mod_id or "").strip()
        if not mid.isdigit() and workspace_id is not None:
            from services.path_lifecycle import resolve_mod_id

            mid = resolve_mod_id(mid or None, workspace_id=workspace_id, db=self._database())
        if not mid.isdigit():
            return RegistrationRemovalResult(
                success=False,
                entity="mod",
                id=mid,
                error="invalid mod_id",
            )

        db = self._database()
        row = db.get_mod_backup_row(mid) or {}
        lkp = str(row.get("last_known_path") or "").strip()
        cover = str(row.get("cover_path") or row.get("backup_cover_path") or "").strip()
        was_app_root = False
        if lkp:
            try:
                was_app_root = Path(lkp).resolve() == project_root().resolve()
            except OSError:
                was_app_root = False

        # Registration delete only — never ModRemover / rmtree / undeploy.
        removed = db.delete_mod_record(mid)
        self._invalidate_projection(mid)
        if cover:
            self._invalidate_cover(cover)

        return RegistrationRemovalResult(
            success=bool(removed),
            entity="mod",
            id=mid,
            db_removed=bool(removed),
            last_known_path=lkp,
            path_was_application_root=was_app_root,
            filesystem_touched=False,
            error="" if removed else "mod registration not found",
            details={
                "workspace_id": str(row.get("workspace_id") or ""),
                "app_id": int(row.get("app_id") or 0),
            },
        )

    def remove_game_registration(
        self,
        app_id: int | str,
        *,
        detach_mods: bool = True,
    ) -> RegistrationRemovalResult:
        """
        Remove one Game registration (``games`` + categories).

        By default detaches mods (``app_id → 0``) instead of deleting them,
        so real Mod registrations are preserved. Pass ``detach_mods=False``
        only when callers already removed associated Mod registrations.

        Refuses to delete the schema sentinel ``app_id=0``.
        """
        try:
            aid = int(str(app_id).strip())
        except (TypeError, ValueError):
            return RegistrationRemovalResult(
                success=False,
                entity="game",
                id=str(app_id),
                error="invalid app_id",
            )
        if aid == 0:
            return RegistrationRemovalResult(
                success=False,
                entity="game",
                id="0",
                error="refusing to delete schema sentinel app_id=0",
            )

        db = self._database()
        existing = db.get_game(aid)

        detached = 0
        if detach_mods:
            detached = db.detach_mods_from_game(aid)

        removed = db.delete_game_record(aid)
        self._invalidate_projection(None)

        return RegistrationRemovalResult(
            success=bool(removed),
            entity="game",
            id=str(aid),
            db_removed=bool(removed),
            filesystem_touched=False,
            error="" if removed else "game registration not found",
            details={"detached_mods": detached, "had_game_info": existing is not None},
        )

    def remove_mods_bound_to_application_root(self) -> list[RegistrationRemovalResult]:
        """Remove every Mod whose ``last_known_path`` is the application root."""
        db = self._database()
        root = str(project_root().resolve())
        targets: list[str] = []
        for row in db.iter_mod_backup_rows():
            lkp = str(row.get("last_known_path") or "").strip()
            if not lkp:
                continue
            try:
                if Path(lkp).resolve() == Path(root).resolve():
                    targets.append(str(row.get("mod_id") or "").strip())
            except OSError:
                continue
        return [self.remove_mod_registration(mid) for mid in targets if mid.isdigit()]

    def _invalidate_projection(self, mod_id: str | None) -> None:
        try:
            from services.mod_library_cache import (
                ModLibraryCache,
                reset_library_cache,
            )

            if mod_id:
                ModLibraryCache.instance().invalidate(mod_id)
            else:
                reset_library_cache()
        except Exception as exc:  # noqa: BLE001
            logger.debug("projection invalidate skipped: %s", exc)

    def _invalidate_cover(self, path: str) -> None:
        try:
            from services.cover_cache import invalidate_cover

            invalidate_cover(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("cover invalidate skipped: %s", exc)
