#!/usr/bin/env python3
"""One-shot production Type Name → Type ID migration.

Reads ``game_categories`` / category ``mod_tags`` as legacy sources only.
Writes ``data/mod_types.json`` and NULL-only ``mods.type_id``.
Does not delete legacy rows. Does not touch Identity / Collection / Deploy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.db_manager import DatabaseManager, TAG_TYPE_CATEGORY  # noqa: E402
from core.paths import database_path, mod_types_path  # noqa: E402
from services.mod_type_catalog import reset_mod_type_catalog  # noqa: E402
from services.mod_type_legacy_migration import migrate_legacy_mod_types  # noqa: E402


def _snapshot(db: DatabaseManager) -> dict[str, object]:
    with db._lock:
        ident = db._conn.execute(
            """
            SELECT mod_id, internal_id, workspace_id, external_id
            FROM mods
            ORDER BY mod_id
            """
        ).fetchall()
        cats = db._conn.execute(
            "SELECT app_id, name FROM game_categories ORDER BY app_id, id"
        ).fetchall()
        tags = db._conn.execute(
            """
            SELECT mod_id, tag_type, tag_value
            FROM mod_tags
            WHERE tag_type = ?
            ORDER BY id
            """,
            (TAG_TYPE_CATEGORY,),
        ).fetchall()
        coll = db._conn.execute("SELECT COUNT(*) FROM collections").fetchone()[0]
        coll_mods = db._conn.execute("SELECT COUNT(*) FROM collection_mods").fetchone()[0]
        deploys = db._conn.execute("SELECT COUNT(*) FROM deployment_records").fetchone()[0]
        type_ids = db._conn.execute(
            """
            SELECT app_id, type_id, COUNT(*) AS n
            FROM mods
            WHERE type_id IS NOT NULL AND type_id > 0
            GROUP BY app_id, type_id
            ORDER BY app_id, type_id
            """
        ).fetchall()
        games = {
            int(r["app_id"]): str(r["name"] or "")
            for r in db._conn.execute("SELECT app_id, name FROM games").fetchall()
            if int(r["app_id"] or 0) > 0
        }
    return {
        "identity": [
            (int(r["mod_id"]), str(r["internal_id"]), str(r["workspace_id"]), str(r["external_id"]))
            for r in ident
        ],
        "categories": [(int(r["app_id"]), str(r["name"])) for r in cats],
        "tags": [(str(r["mod_id"]), str(r["tag_type"]), str(r["tag_value"])) for r in tags],
        "collections": int(coll),
        "collection_mods": int(coll_mods),
        "deployment_records": int(deploys),
        "type_counts": [
            (int(r["app_id"]), int(r["type_id"]), int(r["n"])) for r in type_ids
        ],
        "games": games,
    }


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    db_path = database_path()
    json_path = mod_types_path()
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(db_path)
    before = _snapshot(db)
    names_by_game = db.list_legacy_type_names_by_game()
    unique_legacy = sum(len(v) for v in names_by_game.values())
    print(f"PLAN unique_legacy_types={unique_legacy} category_rows={len(before['categories'])} category_tags={len(before['tags'])} mods={len(before['identity'])}")
    catalog = reset_mod_type_catalog(json_path)
    first = migrate_legacy_mod_types(catalog, db)
    after = _snapshot(db)
    second = migrate_legacy_mod_types(catalog, db)
    after_second = _snapshot(db)

    identity_ok = before["identity"] == after["identity"] == after_second["identity"]
    legacy_ok = (
        before["categories"] == after["categories"] == after_second["categories"]
        and before["tags"] == after["tags"] == after_second["tags"]
        and before["collections"] == after["collections"] == after_second["collections"]
        and before["collection_mods"] == after["collection_mods"] == after_second["collection_mods"]
        and before["deployment_records"]
        == after["deployment_records"]
        == after_second["deployment_records"]
    )
    ids_stable = after["type_counts"] == after_second["type_counts"]
    first_id_set = {(row.app_id, row.type_id) for row in first.per_game}
    second_id_set = {(row.app_id, row.type_id) for row in second.per_game}

    payload = {
        "db": str(db_path),
        "catalog": str(json_path),
        "legacy_category_rows": len(before["categories"]),
        "legacy_unique_names": unique_legacy,
        "new_type_count": (
            first.new_type_count if not first.already_migrated else second.reused_types
        ),
        "first": first.to_dict(),
        "second": second.to_dict(),
        "identity_unchanged": identity_ok,
        "legacy_tables_unchanged": legacy_ok,
        "idempotent_bindings": ids_stable,
        "idempotent_type_ids": first_id_set == second_id_set or first.already_migrated,
        "games": before["games"],
    }
    out = _ROOT / "_tmp" / "type_migration_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"DB={db_path}")
    print(f"CATALOG={json_path}")
    print(f"LEGACY_CATEGORY_ROWS={len(before['categories'])}")
    print(f"LEGACY_CATEGORY_TAGS={len(before['tags'])}")
    print(f"NEW_TYPE_COUNT={payload['new_type_count']}")
    print(f"MODS_BOUND={first.mods_bound if not first.already_migrated else 0}")
    print(f"MODS_LEFT_NULL={first.mods_left_null}")
    print(f"UNRESOLVED={len(first.unresolved)}")
    print(f"IDENTITY_UNCHANGED={identity_ok}")
    print(f"LEGACY_TABLES_UNCHANGED={legacy_ok}")
    print(f"IDEMPOTENT={ids_stable and second.already_migrated}")
    print(f"REPORT={out}")
    for row in first.per_game if not first.already_migrated else second.per_game:
        game = before["games"].get(row.app_id, "")
        print(
            f"  app_id={row.app_id} {game} name={row.legacy_name!r} "
            f"type_id={row.type_id} mods={row.affected_mods}"
        )
    ok = identity_ok and legacy_ok and ids_stable and not first.unresolved
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
