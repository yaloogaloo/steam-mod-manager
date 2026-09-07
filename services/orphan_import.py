"""Orphan discovery notes — bind existing entities only, never create.

ARCHITECTURE RULE
-----------------
Reconcile may note unbound folders. Entity create is Import / Steam Sync
exclusively. This module must not call ``create_mod_identity``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrphanCandidate:
    """Filesystem / backup discovery that must not auto-create an entity."""

    path: str
    platform: str = ""
    external_id: str = ""
    workspace_id: str = ""
    source_url: str = ""
    title: str = ""
    app_id: int = 0
    game_name: str = ""
    origin: str = "filesystem"  # filesystem | backup
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrphanImportResult:
    imported: int = 0
    bound: int = 0
    failed: int = 0
    mod_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def import_orphan_candidates(
    candidates: Sequence[OrphanCandidate],
    *,
    db: Any | None = None,
) -> OrphanImportResult:
    """
    Bind orphans to existing DB entities only — never create.

    Unbound folders stay unresolved notes.
    """
    from core.db_manager import get_db
    from services.mod_identity import ensure_mod_identity

    database = db if db is not None else get_db()
    result = OrphanImportResult()
    if not candidates:
        return result

    for cand in candidates:
        folder = Path(str(cand.path or "")).expanduser()
        try:
            payload = dict(cand.payload or {})
            if folder.is_dir():
                payload.setdefault("_managed_path", str(folder.resolve()))
                payload.setdefault("_folder_name", folder.name)
                mid, _payload, _changed = ensure_mod_identity(
                    folder, payload, db=database
                )
                if mid.isdigit():
                    result.bound += 1
                    result.mod_ids.append(mid)
                    continue
            result.notes.append(f"ORPHAN_IGNORED_NO_CREATE: {folder}")
            result.failed += 1
        except Exception as exc:  # noqa: BLE001
            result.failed += 1
            result.notes.append(f"ORPHAN_FAILED: {folder}: {exc}")
            logger.warning("orphan bind failed for %s: %s", folder, exc)
    return result
