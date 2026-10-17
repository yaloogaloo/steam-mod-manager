"""Authoritative Content Missing evaluator — sole writer of content_missing.

ARCHITECTURE RULE
-----------------
Content Missing is a **system derived state**. It may only be produced from
authoritative content validation (live managed-folder payload via
``read_is_missing_content`` / ``has_local_mod_payload``).

Legal callers (must invoke this module — never hardcode content_missing):
- Refresh (``mod_refresh.reconcile_local_state``)
- Sync entity registration
- Import materialize
- Reconcile (present-folder bind — authoritative only)
- Status Recovery

Identity facts are **never** written here — use ``identity_status``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from services.file_ops import (
    read_is_missing_content,
    set_is_missing_content,
)
from services.library_status import (
    CONTENT_CONTENT_MISSING,
    compute_content_status,
    content_status_to_library_status,
)

logger = logging.getLogger(__name__)


def evaluate_missing_content(
    managed_path: str | Path | None,
    *,
    internal_id: int | str | None = None,
    db: Any = None,
) -> bool:
    """True when the managed folder has no local Mod payload (authoritative)."""
    from services.file_ops import is_missing_mod_content

    if managed_path is None:
        return True
    root = Path(managed_path)
    if not root.is_dir():
        return True
    mid = str(internal_id or "").strip() or None
    if mid is not None:
        return bool(is_missing_mod_content(root, mod_id=mid, db=db))
    return bool(read_is_missing_content(root))


def evaluate_content_status(
    *,
    folder_present: bool,
    managed_path: str | Path | None = None,
    internal_id: int | str | None = None,
    db: Any = None,
    backup_status: str = "",
    metadata_missing: bool = False,
) -> str:
    """Compute content_status ∈ {healthy, content_missing} (no identity/backup)."""
    del backup_status, metadata_missing
    if not folder_present:
        return CONTENT_CONTENT_MISSING
    missing = evaluate_missing_content(
        managed_path, internal_id=internal_id, db=db
    )
    return compute_content_status(
        folder_present=True,
        missing_content=missing,
    )


def persist_evaluated_content_status(
    internal_id: int | str,
    managed_path: str | Path | None,
    *,
    db: Any = None,
    folder_present: bool | None = None,
    backup_status: str = "",
    metadata_missing: bool = False,
    sync_sticky_marker: bool = True,
    touch_updated_at: bool = False,
    notify_projection: bool = True,
) -> str:
    """
    Persist content_status from authoritative evaluation.

    This is the **only** legal writer of ``content_status=content_missing``.
    Never writes ``identity_status`` or ``conflict_status``.

    Never bumps ``mods.updated_at`` — content eval is a forbidden reason.
    ``touch_updated_at`` must remain False (DatabaseManager rejects True).

    When the written content columns change and *notify_projection* is True,
    Library projection is invalidated (immediate or deferred via fs observer).
    """
    from core.db_manager import get_db

    database = db if db is not None else get_db()
    mid = str(internal_id or "").strip()
    present = bool(folder_present)
    if folder_present is None:
        present = bool(
            managed_path is not None and Path(managed_path).is_dir()
        )

    cs = evaluate_content_status(
        folder_present=present,
        managed_path=managed_path,
        internal_id=mid,
        db=database,
        backup_status=backup_status,
        metadata_missing=metadata_missing,
    )

    if sync_sticky_marker and present and managed_path is not None:
        try:
            set_is_missing_content(
                managed_path, cs == CONTENT_CONTENT_MISSING
            )
        except OSError:
            logger.debug(
                "sticky missing marker sync failed for %s", mid, exc_info=True
            )

    try:
        changed = bool(
            database.update_mod_content_status(
                mid,
                content_status=cs,
                library_status=content_status_to_library_status(cs),
                folder_present=present,
                last_known_path=(
                    str(Path(managed_path).resolve())
                    if present and managed_path is not None
                    else None
                ),
                touch_updated_at=touch_updated_at,
            )
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "persist_evaluated_content_status failed for %s", mid, exc_info=True
        )
        return cs
    if changed and notify_projection:
        try:
            from services.mod_fs_observer import note_content_status_projection_touch

            note_content_status_projection_touch(mid)
        except Exception:  # noqa: BLE001
            pass
    return cs


def is_content_missing_status(value: str | None) -> bool:
    return str(value or "").strip() == CONTENT_CONTENT_MISSING
