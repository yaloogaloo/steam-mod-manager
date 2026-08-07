"""Reserved helpers for Mod relationship metadata (importer extension point).

Phase 8: relationships are user-declared only. Importers must not guess.
Future Nexus/GitHub metadata may call ``apply_declared_relationships``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from core.db_manager import (
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
