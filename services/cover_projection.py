"""Canonical cover reference for Library projection (LIVE vs MISS).

ARCHITECTURE RULE
-----------------
Layer-1 / ModCardData.cover must already be the final display reference.
Cards consume projection only — they must not open Backup metadata or
rediscover covers when ``folder_absent``.

Contract:
  LIVE  → local ``mods.cover_path`` (relative or absolute), else Backup
  MISS  → Backup ``mods.backup_cover_path`` only (never a dead local relative)

Detail uses :func:`services.mod_metadata_resolver.resolve_cover_path`, which
for MISS resolves to the same Backup cover file. Do not invent a second
MISS cover system on the card.
"""

from __future__ import annotations


def projection_cover_ref(
    *,
    folder_present: bool,
    cover_path: str | None,
    backup_cover_path: str | None,
) -> str:
    """Return the cover reference string for Library Layer-1 / ModCardData.

    No filesystem I/O. Callers pass SQLite column values only.
    """
    live = str(cover_path or "").strip()
    backup = str(backup_cover_path or "").strip()
    if not folder_present:
        return backup
    return live or backup
