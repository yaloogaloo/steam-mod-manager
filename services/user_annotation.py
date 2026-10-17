"""User annotation writers — sole authority for ``mods.conflict_status``.

ARCHITECTURE RULE
-----------------
Conflict is a **pure user mark** (same class as invalid / abandoned).

Legal lifecycle::

    Detail Panel flag chip
        → services.user_annotation
        → mods.conflict_status

Forbidden: Sync, Refresh, Deploy, Reconcile, Identity, Repair, ConflictDetector,
Import, Archive, or any projection layer inventing conflict from content_status,
identity_conflict, filesystem, or relationship diagnostics.
"""

from __future__ import annotations

from core.db_manager import DatabaseManager, get_db
from core.mod_status import (
    CONFLICT_STATUS_CONFLICT,
    CONFLICT_STATUS_NONE,
    ModStatus,
)


def set_conflict_annotation(
    mod_id: int | str,
    *,
    note: str = "",
    db: DatabaseManager | None = None,
) -> ModStatus:
    """Mark Mod as user-conflict. Only Detail Panel may call this."""
    database = db if db is not None else get_db()
    return database.update_mod_conflict_annotation(
        mod_id,
        conflict=True,
        note=str(note or "").strip() or "已标记冲突",
    )


def clear_conflict_annotation(
    mod_id: int | str,
    *,
    db: DatabaseManager | None = None,
) -> ModStatus:
    """Clear user-conflict mark. Only Detail Panel may call this."""
    database = db if db is not None else get_db()
    return database.update_mod_conflict_annotation(
        mod_id,
        conflict=False,
        note="",
    )


def apply_conflict_annotation(
    mod_id: int | str,
    *,
    conflict: bool,
    note: str = "",
    db: DatabaseManager | None = None,
) -> ModStatus:
    """Toggle helper used by flag chips."""
    if conflict:
        return set_conflict_annotation(mod_id, note=note, db=db)
    return clear_conflict_annotation(mod_id, db=db)


__all__ = (
    "CONFLICT_STATUS_CONFLICT",
    "CONFLICT_STATUS_NONE",
    "ModStatus",
    "apply_conflict_annotation",
    "clear_conflict_annotation",
    "set_conflict_annotation",
)
