#!/usr/bin/env python3
"""Phase 3+4 — migrate Runtime associations + rescan offline from disk.

One-shot. Never creates Mod entities. Never changes new internal_id / workspace_id.

Restores from backup via identity_rebuild_mapping.json:
- deployment_records / deployment_record_items
- game_categories / mod_tags
- mod_relationships (when both ends mapped)
- favorite / enabled / cover / title / description / notes
- offline_status rescanned from .info/index.html (not backup)

Usage::

    python tools/identity_rebuild_runtime_migrate.py --dry-run
    python tools/identity_rebuild_runtime_migrate.py --apply --confirm
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.mod_platform import (  # noqa: E402
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_NONE,
)

OUT_DIR = ROOT / "tools" / "_audit_out"
DEFAULT_DB = ROOT / "data" / "mod_manager.db"
DEFAULT_MAPPING = OUT_DIR / "identity_rebuild_mapping.json"
DEFAULT_LIBRARY = ROOT / "mod"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(v: Any) -> str:
    return str(v or "").strip()


def _latest_backup(data_dir: Path) -> Path | None:
    cands = sorted(data_dir.glob("mod_manager.db.pre_identity_rebuild_*"))
    return cands[-1] if cands else None


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


def _has_offline_index(folder: Path) -> bool:
    return (folder / ".info" / "index.html").is_file() or (
        folder / ".info" / "offline" / "index.html"
    ).is_file()


def _load_mapping(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    by_old_mod: dict[str, dict[str, Any]] = {}
    for item in data.get("mappings") or []:
        if _text(item.get("confidence")) not in {"verified", "auxiliary"}:
            continue
        if not _text(item.get("new_mod_id")):
            continue
        old_mod = _text(item.get("old_mod_id"))
        if old_mod:
            by_old_mod[old_mod] = item
    return by_old_mod


def migrate(
    *,
    live_db: Path,
    backup_db: Path,
    mapping_path: Path,
    library: Path,
    apply: bool,
) -> dict[str, Any]:
    by_old = _load_mapping(mapping_path)
    bak = _connect(backup_db)
    live = _connect(live_db)

    result: dict[str, Any] = {
        "generated_at": _now(),
        "tool": "identity_rebuild_runtime_migrate",
        "apply": apply,
        "mapping_path": str(mapping_path.resolve()),
        "backup_db": str(backup_db.resolve()),
        "live_db": str(live_db.resolve()),
        "mapped_entities": len(by_old),
        "restored": {
            "deployment_records": 0,
            "deployment_record_items": 0,
            "game_categories": 0,
            "mod_tags": 0,
            "mod_relationships": 0,
            "favorites": 0,
            "enabled": 0,
            "metadata_fields": 0,
            "offline_rescanned": 0,
            "offline_archived": 0,
            "offline_none": 0,
        },
        "skipped": [],
        "errors": [],
    }

    def _run(sql: str, params: tuple[Any, ...] = ()) -> None:
        if apply:
            live.execute(sql, params)

    # --- game_categories (app-scoped names, no mod_id) ---
    if True:
        cats = bak.execute("SELECT app_id, name, created_at FROM game_categories").fetchall()
        for row in cats:
            try:
                _run(
                    """
                    INSERT OR IGNORE INTO game_categories (app_id, name, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (int(row["app_id"] or 0), _text(row["name"]), _text(row["created_at"]) or _now()),
                )
                result["restored"]["game_categories"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append({"game_categories": str(exc)})

    # --- deployment_records ---
    old_record_to_new: dict[int, int] = {}
    records = bak.execute(
        "SELECT id, app_id, name, created_at, updated_at FROM deployment_records"
    ).fetchall()
    for row in records:
        old_id = int(row["id"])
        app_id = int(row["app_id"] or 0)
        name = _text(row["name"])
        existing = live.execute(
            """
            SELECT id FROM deployment_records
            WHERE app_id = ? AND name = ? COLLATE NOCASE
            LIMIT 1
            """,
            (app_id, name),
        ).fetchone()
        if existing is not None:
            old_record_to_new[old_id] = int(existing["id"])
            continue
        if apply:
            cur = live.execute(
                """
                INSERT INTO deployment_records (app_id, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    app_id,
                    name,
                    _text(row["created_at"]) or _now(),
                    _text(row["updated_at"]) or _now(),
                ),
            )
            old_record_to_new[old_id] = int(cur.lastrowid)
        else:
            old_record_to_new[old_id] = -old_id  # dry-run placeholder
        result["restored"]["deployment_records"] += 1

    # --- deployment_record_items ---
    items = bak.execute(
        "SELECT record_id, mod_id FROM deployment_record_items"
    ).fetchall()
    for row in items:
        old_rec = int(row["record_id"])
        old_mod = _text(row["mod_id"])
        mapped = by_old.get(old_mod)
        if mapped is None:
            result["skipped"].append(
                {"kind": "deployment_item", "old_mod_id": old_mod, "reason": "unmapped"}
            )
            continue
        new_rec = old_record_to_new.get(old_rec)
        if new_rec is None:
            result["skipped"].append(
                {
                    "kind": "deployment_item",
                    "old_mod_id": old_mod,
                    "reason": "record_missing",
                }
            )
            continue
        new_mod = int(mapped["new_mod_id"])
        if apply and new_rec > 0:
            _run(
                """
                INSERT OR IGNORE INTO deployment_record_items (record_id, mod_id)
                VALUES (?, ?)
                """,
                (new_rec, new_mod),
            )
        result["restored"]["deployment_record_items"] += 1

    # --- mod_tags ---
    tags = bak.execute(
        "SELECT mod_id, tag_type, tag_value, created_at, updated_at FROM mod_tags"
    ).fetchall()
    for row in tags:
        old_mod = _text(row["mod_id"])
        mapped = by_old.get(old_mod)
        if mapped is None:
            result["skipped"].append(
                {"kind": "mod_tag", "old_mod_id": old_mod, "reason": "unmapped"}
            )
            continue
        new_mod = int(mapped["new_mod_id"])
        _run(
            """
            INSERT INTO mod_tags (mod_id, tag_type, tag_value, created_at, updated_at)
            SELECT ?, ?, ?, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM mod_tags
                WHERE mod_id = ? AND tag_type = ? AND tag_value = ?
            )
            """,
            (
                new_mod,
                _text(row["tag_type"]),
                _text(row["tag_value"]),
                _text(row["created_at"]) or _now(),
                _text(row["updated_at"]) or _now(),
                new_mod,
                _text(row["tag_type"]),
                _text(row["tag_value"]),
            ),
        )
        result["restored"]["mod_tags"] += 1

    # --- mod_relationships ---
    if True:
        rels = bak.execute(
            """
            SELECT source_mod_id, target_mod_id, relationship_type, created_at
            FROM mod_relationships
            """
        ).fetchall()
        for row in rels:
            src = by_old.get(_text(row["source_mod_id"]))
            tgt = by_old.get(_text(row["target_mod_id"]))
            if src is None or tgt is None:
                result["skipped"].append(
                    {
                        "kind": "relationship",
                        "source": _text(row["source_mod_id"]),
                        "target": _text(row["target_mod_id"]),
                        "reason": "unmapped_end",
                    }
                )
                continue
            _run(
                """
                INSERT OR IGNORE INTO mod_relationships
                    (source_mod_id, target_mod_id, relationship_type, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    int(src["new_mod_id"]),
                    int(tgt["new_mod_id"]),
                    _text(row["relationship_type"]),
                    _text(row["created_at"]) or _now(),
                ),
            )
            result["restored"]["mod_relationships"] += 1

    # --- per-mod user/metadata fields from backup ---
    bak_mods = bak.execute(
        """
        SELECT mod_id, favorite, enabled, title, display_name, description,
               custom_description, user_notes, cover_path, preview_url,
               custom_deploy_path, offline_provider
        FROM mods
        """
    ).fetchall()
    for row in bak_mods:
        mapped = by_old.get(_text(row["mod_id"]))
        if mapped is None:
            continue
        new_mod = int(mapped["new_mod_id"])
        sets: list[str] = []
        params: list[Any] = []
        if int(row["favorite"] or 0) == 1:
            sets.append("favorite = 1")
            result["restored"]["favorites"] += 1
        if int(row["enabled"] if row["enabled"] is not None else 1) == 0:
            sets.append("enabled = 0")
            result["restored"]["enabled"] += 1
        # Metadata: fill empty live fields only (do not clobber rebuild titles if set).
        for col in (
            "title",
            "display_name",
            "description",
            "custom_description",
            "user_notes",
            "cover_path",
            "preview_url",
            "custom_deploy_path",
            "offline_provider",
        ):
            val = _text(row[col]) if col in row.keys() else ""
            if not val:
                continue
            sets.append(
                f"{col} = CASE WHEN TRIM(COALESCE({col}, '')) = '' THEN ? ELSE {col} END"
            )
            params.append(val)
            result["restored"]["metadata_fields"] += 1
        if not sets:
            continue
        params.append(new_mod)
        _run(f"UPDATE mods SET {', '.join(sets)} WHERE mod_id = ?", tuple(params))

    # --- Phase 4: offline rescan from disk (authority = filesystem) ---
    live_rows = live.execute(
        "SELECT mod_id, internal_id, last_known_path FROM mods"
    ).fetchall()
    for row in live_rows:
        path = _text(row["last_known_path"])
        folder = Path(path) if path else None
        has = bool(folder and folder.is_dir() and _has_offline_index(folder))
        status = OFFLINE_STATUS_ARCHIVED if has else OFFLINE_STATUS_NONE
        _run(
            "UPDATE mods SET offline_status = ? WHERE mod_id = ?",
            (status, int(row["mod_id"])),
        )
        result["restored"]["offline_rescanned"] += 1
        if has:
            result["restored"]["offline_archived"] += 1
        else:
            result["restored"]["offline_none"] += 1

    if apply:
        live.commit()

    # verify snapshot counts
    result["live_after"] = {
        "mods": int(live.execute("SELECT COUNT(*) FROM mods").fetchone()[0]),
        "deployment_records": int(
            live.execute("SELECT COUNT(*) FROM deployment_records").fetchone()[0]
        ),
        "deployment_record_items": int(
            live.execute("SELECT COUNT(*) FROM deployment_record_items").fetchone()[0]
        ),
        "mod_tags": int(live.execute("SELECT COUNT(*) FROM mod_tags").fetchone()[0]),
        "game_categories": int(
            live.execute("SELECT COUNT(*) FROM game_categories").fetchone()[0]
        ),
        "favorites": int(
            live.execute("SELECT COUNT(*) FROM mods WHERE favorite=1").fetchone()[0]
        ),
        "offline_archived": int(
            live.execute(
                "SELECT COUNT(*) FROM mods WHERE offline_status = ?",
                (OFFLINE_STATUS_ARCHIVED,),
            ).fetchone()[0]
        ),
    }

    bak.close()
    live.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backup", type=Path, default=None)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT_DIR / "identity_rebuild_runtime_migrate_report.json",
    )
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--apply", action="store_true", default=False)
    parser.add_argument("--confirm", action="store_true", default=False)
    args = parser.parse_args()

    backup = args.backup or _latest_backup(args.db.parent)
    if backup is None or not Path(backup).is_file():
        print("backup DB not found", file=sys.stderr)
        return 2
    if not args.mapping.is_file():
        print("mapping JSON not found — run identity_rebuild_mapping.py first", file=sys.stderr)
        return 2

    apply = bool(args.apply and args.confirm) and not args.dry_run
    report = migrate(
        live_db=args.db,
        backup_db=Path(backup),
        mapping_path=args.mapping,
        library=args.library,
        apply=apply,
    )
    report["dry_run"] = not apply
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote={args.out}")
    print(f"apply={apply}")
    print(f"restored={report['restored']}")
    print(f"live_after={report['live_after']}")
    print(f"skipped={len(report['skipped'])} errors={len(report['errors'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
