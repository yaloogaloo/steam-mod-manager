"""One-shot legacy Type Name → Type ID migration.

Runtime Type Definition authority remains ``data/mod_types.json``.
``game_categories`` / ``mod_tags`` are read-only sources for this migration
and must not recreate types after ``legacy_migrated`` is set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from services.mod_type_catalog import ModTypeCatalog, ModTypeCatalogError

logger = logging.getLogger(__name__)


@dataclass
class GameTypeMigrationRow:
    app_id: int
    legacy_name: str
    type_id: int
    affected_mods: int
    created: bool


@dataclass
class LegacyTypeMigrationReport:
    already_migrated: bool
    created_types: int = 0
    reused_types: int = 0
    mods_bound: int = 0
    mods_left_null: int = 0
    mods_already_bound: int = 0
    unresolved: list[str] = field(default_factory=list)
    per_game: list[GameTypeMigrationRow] = field(default_factory=list)

    @property
    def new_type_count(self) -> int:
        return int(self.created_types) + int(self.reused_types)

    def to_dict(self) -> dict[str, Any]:
        return {
            "already_migrated": self.already_migrated,
            "created_types": self.created_types,
            "reused_types": self.reused_types,
            "new_type_count": self.new_type_count,
            "mods_bound": self.mods_bound,
            "mods_left_null": self.mods_left_null,
            "mods_already_bound": self.mods_already_bound,
            "unresolved": list(self.unresolved),
            "per_game": [
                {
                    "app_id": row.app_id,
                    "legacy_name": row.legacy_name,
                    "type_id": row.type_id,
                    "affected_mods": row.affected_mods,
                    "created": row.created,
                }
                for row in self.per_game
            ],
        }


def migrate_legacy_mod_types(
    catalog: ModTypeCatalog,
    db: Any,
) -> LegacyTypeMigrationReport:
    """
    Import legacy names into the Type Definition file and bind NULL ``type_id``.

    Idempotent: existing JSON Type IDs are reused. After ``legacy_migrated``,
    legacy tables are never used to recreate deleted types or re-bind Mods.
    Does not delete ``game_categories`` / ``mod_tags``. Does not touch Identity.
    """
    if not catalog.is_valid():
        return LegacyTypeMigrationReport(
            already_migrated=False,
            unresolved=["type catalog invalid; migration skipped"],
        )
    if catalog.is_legacy_migrated():
        return _report_current_state(catalog, db, already_migrated=True)

    names_by_game = db.list_legacy_type_names_by_game()
    created = 0
    reused = 0
    rows: list[GameTypeMigrationRow] = []
    id_by_key: dict[tuple[int, str], int] = {}
    for aid, names in sorted(names_by_game.items()):
        for label in names:  # exact strings; do not strip/merge whitespace variants
            try:
                typedef, was_created = catalog.ensure_named_type(aid, label)
            except ModTypeCatalogError as exc:
                logger.warning("skip illegal legacy type app_id=%s name=%r: %s", aid, label, exc)
                continue
            id_by_key[(aid, label)] = typedef.type_id
            if was_created:
                created += 1
            else:
                reused += 1
            rows.append(
                GameTypeMigrationRow(
                    app_id=aid,
                    legacy_name=label,
                    type_id=typedef.type_id,
                    affected_mods=0,
                    created=was_created,
                )
            )
    catalog.persist()

    tag_counts: dict[tuple[int, str], int] = {}
    assignments: dict[str, int] = {}
    unresolved: list[str] = []
    for mid, aid, label in db.list_legacy_mod_type_names():
        tag_counts[(aid, label)] = tag_counts.get((aid, label), 0) + 1
        tid = id_by_key.get((aid, label))
        if tid is None:
            found = catalog.find_type_by_name(aid, label)
            tid = found.type_id if found is not None else None
        if tid is None:
            unresolved.append(f"app_id={aid} name={label!r} mod_id={mid}")
            continue
        current = db.get_mod_type_id(mid)
        if current is not None:
            continue
        assignments[mid] = tid

    bound = db.bind_null_mod_type_ids(assignments)
    catalog.mark_legacy_migrated()

    for row in rows:
        row.affected_mods = int(tag_counts.get((row.app_id, row.legacy_name), 0))

    total_mods, already = db.count_mods_and_type_bindings()
    left_null = max(total_mods - already, 0)

    return LegacyTypeMigrationReport(
        already_migrated=False,
        created_types=created,
        reused_types=reused,
        mods_bound=int(bound),
        mods_left_null=left_null,
        mods_already_bound=max(already - int(bound), 0),
        unresolved=unresolved,
        per_game=rows,
    )


def _report_current_state(
    catalog: ModTypeCatalog,
    db: Any,
    *,
    already_migrated: bool,
) -> LegacyTypeMigrationReport:
    rows: list[GameTypeMigrationRow] = []
    reused = 0
    for aid in catalog.app_ids():
        for typedef in catalog.list_types(aid):
            reused += 1
            affected = len(db.list_mod_ids_with_type(aid, typedef.type_id))
            rows.append(
                GameTypeMigrationRow(
                    app_id=aid,
                    legacy_name=typedef.name,
                    type_id=typedef.type_id,
                    affected_mods=affected,
                    created=False,
                )
            )
    total_mods, already = db.count_mods_and_type_bindings()
    return LegacyTypeMigrationReport(
        already_migrated=already_migrated,
        created_types=0,
        reused_types=reused,
        mods_bound=0,
        mods_left_null=max(total_mods - already, 0),
        mods_already_bound=already,
        per_game=rows,
    )
