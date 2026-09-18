"""Orphan discovery notes — bind existing entities only, never create.

ARCHITECTURE RULE
-----------------
Reconcile may note unbound folders. Entity create is Import / Steam Sync
exclusively. This module must not call ``create_mod_identity``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
