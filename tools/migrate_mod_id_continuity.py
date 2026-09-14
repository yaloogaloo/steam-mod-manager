#!/usr/bin/env python3
"""Remap mods.mod_id → contiguous 1..N while preserving internal_id / workspace_id.

Atomic SQLite remapping + data/mod_backup/<mod_id>/ rename.

Usage::

    python tools/migrate_mod_id_continuity.py --dry-run
    python tools/migrate_mod_id_continuity.py --apply --confirm

Identity Contract:
  - mods.mod_id may change (DB PK / storage key only)
  - internal_id MUST NOT change
  - workspace_id / external_id / source_url MUST NOT change
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "data" / "mod_manager.db"
DEFAULT_BACKUP_ROOT = ROOT / "data" / "mod_backup"
OUT_DIR = ROOT / "_tmp" / "dumps" / "mod_id_continuity"

CHILD_REMAPS: tuple[tuple[str, str], ...] = (
    ("collection_mods", "mod_id"),
    ("mod_tags", "mod_id"),
    ("deployment_record_items", "mod_id"),
    ("mod_relationships", "source_mod_id"),
    ("mod_relationships", "target_mod_id"),
    ("mod_relations", "source_mod_id"),
    ("mod_relations", "target_mod_id"),
)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def build_mapping(con: sqlite3.Connection) -> list[tuple[int, int]]:
    """ORDER BY mod_id → new ids 1..N."""
    rows = con.execute("SELECT mod_id FROM mods ORDER BY mod_id").fetchall()
    return [(int(r["mod_id"]), i + 1) for i, r in enumerate(rows)]


def snapshot_before(
    con: sqlite3.Connection, backup_root: Path, out: Path
) -> dict[str, Any]:
    mapping = build_mapping(con)
    ids = [o for o, _ in mapping]
    gaps = 0
    if len(ids) >= 2:
        # Contiguous expected span is len(ids) only when min==1 and max==len;
        # otherwise count holes relative to sorted unique ids without allocating
        # a giant range up to max(mod_id).
        for i in range(1, len(ids)):
            delta = ids[i] - ids[i - 1]
            if delta > 1:
                gaps += delta - 1
    high = sum(1 for o in ids if o > len(ids))
    entity_rows = con.execute(
        """
        SELECT mod_id, internal_id, workspace_id, external_id, platform, app_id
        FROM mods ORDER BY mod_id
        """
    ).fetchall()
    md = dict(mapping)
    entities = [
        {
            "old_mod_id": int(r["mod_id"]),
            "new_mod_id": md[int(r["mod_id"])],
            "internal_id": str(r["internal_id"] or ""),
            "workspace_id": str(r["workspace_id"] or ""),
            "external_id": str(r["external_id"] or ""),
            "platform": str(r["platform"] or ""),
            "app_id": int(r["app_id"] or 0),
        }
        for r in entity_rows
    ]
    fk_counts: dict[str, int] = {}
    for table, _col in CHILD_REMAPS:
        if _table_exists(con, table):
            fk_counts[table] = int(
                con.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            )
    if _table_exists(con, "identity_audit_log"):
        fk_counts["identity_audit_log"] = int(
            con.execute("SELECT COUNT(*) AS n FROM identity_audit_log").fetchone()["n"]
        )
    dirs = sorted(
        p.name for p in backup_root.iterdir() if p.is_dir()
    ) if backup_root.is_dir() else []
    snap = {
        "generated_at": _utc(),
        "mods": len(ids),
        "min_mod_id": ids[0] if ids else None,
        "max_mod_id": ids[-1] if ids else None,
        "gap_count": gaps,
        "duplicate_count": len(ids) - len(set(ids)),
        "high_id_rows": high,
        "fk_counts": fk_counts,
        "backup_dirs": len(dirs),
        "backup_dir_names_sample": dirs[:20],
        "backup_orphan_vs_db": sorted(
            set(int(x) for x in dirs if x.isdigit()) - set(ids)
        ),
        "db_missing_backup": sorted(
            set(ids) - set(int(x) for x in dirs if x.isdigit())
        ),
        "mapping_count": len(mapping),
        "entities": entities,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "snapshot_before.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "mapping.json").write_text(
        json.dumps(
            {
                "generated_at": _utc(),
                "order": "ORDER BY mod_id ASC → new 1..N",
                "pairs": [{"old": o, "new": n} for o, n in mapping],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return snap


def _rewrite_backup_path(path: str, old: int, new: int) -> str:
    if not path:
        return path
    old_s, new_s = str(old), str(new)
    out = path
    for sep in ("/", "\\"):
        token_old = f"{sep}mod_backup{sep}{old_s}{sep}"
        token_new = f"{sep}mod_backup{sep}{new_s}{sep}"
        out = out.replace(token_old, token_new)
        # trailing without final sep
        tail_old = f"{sep}mod_backup{sep}{old_s}"
        tail_new = f"{sep}mod_backup{sep}{new_s}"
        if out.endswith(tail_old):
            out = out[: -len(tail_old)] + tail_new
    return out


def migrate_db(con: sqlite3.Connection, mapping: list[tuple[int, int]]) -> dict[str, Any]:
    """Atomic remapping. Uses foreign_keys=OFF only for the PK rewrite window."""
    md = dict(mapping)
    n = len(mapping)
    stats: dict[str, Any] = {"remapped_mods": n, "child_updates": {}}

    # Reason for OFF: SQLite cannot UPDATE INTEGER PRIMARY KEY under live FKs
    # without cascading support; we rebuild mods and rewrite children, then
    # validate with foreign_key_check before re-enabling.
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DROP TABLE IF EXISTS _mod_id_map")
        con.execute(
            """
            CREATE TABLE _mod_id_map (
                old_id INTEGER PRIMARY KEY,
                new_id INTEGER NOT NULL UNIQUE
            )
            """
        )
        con.executemany(
            "INSERT INTO _mod_id_map(old_id, new_id) VALUES (?, ?)",
            mapping,
        )

        create_sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='mods'"
        ).fetchone()["sql"]
        create_new = create_sql.replace("CREATE TABLE mods", "CREATE TABLE mods_new", 1)
        if "CREATE TABLE mods_new" not in create_new:
            raise RuntimeError("failed to derive mods_new DDL")
        con.execute("DROP TABLE IF EXISTS mods_new")
        con.execute(create_new)

        cols = [
            str(r[1])
            for r in con.execute("PRAGMA table_info(mods)").fetchall()
        ]
        if "mod_id" not in cols:
            raise RuntimeError("mods.mod_id missing")
        other = [c for c in cols if c != "mod_id"]
        col_list = ", ".join(other)
        con.execute(
            f"""
            INSERT INTO mods_new (mod_id, {col_list})
            SELECT m.new_id, {", ".join("mods." + c for c in other)}
            FROM mods
            JOIN _mod_id_map m ON m.old_id = mods.mod_id
            """
        )
        inserted = con.execute("SELECT COUNT(*) AS n FROM mods_new").fetchone()["n"]
        if int(inserted) != n:
            raise RuntimeError(f"mods_new row count {inserted} != {n}")

        for table, col in CHILD_REMAPS:
            if not _table_exists(con, table):
                continue
            before = int(
                con.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            )
            con.execute(
                f"""
                UPDATE {table}
                SET {col} = (
                    SELECT new_id FROM _mod_id_map WHERE old_id = {table}.{col}
                )
                WHERE {col} IN (SELECT old_id FROM _mod_id_map)
                """
            )
            # rows whose FK pointed at missing old id stay unchanged — catch below
            after = int(
                con.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            )
            if after != before:
                raise RuntimeError(f"{table} row count changed {before}→{after}")
            stats["child_updates"][f"{table}.{col}"] = before

        if _table_exists(con, "identity_audit_log"):
            # TEXT mod_id — remap digit strings that match old PKs
            con.execute(
                """
                UPDATE identity_audit_log
                SET mod_id = CAST(
                    (SELECT new_id FROM _mod_id_map
                     WHERE old_id = CAST(identity_audit_log.mod_id AS INTEGER))
                    AS TEXT
                )
                WHERE TRIM(mod_id) GLOB '[0-9]*'
                  AND CAST(mod_id AS INTEGER) IN (SELECT old_id FROM _mod_id_map)
                """
            )
            stats["child_updates"]["identity_audit_log.mod_id"] = "remapped_digits"

        indexes = list(
            con.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE type='index' AND tbl_name='mods' AND sql IS NOT NULL
                """
            ).fetchall()
        )
        con.execute("DROP TABLE mods")
        con.execute("ALTER TABLE mods_new RENAME TO mods")
        for idx in indexes:
            sql = str(idx["sql"] or "")
            if sql:
                con.execute(sql)

        # Rewrite backup_* path columns for remapped ids
        path_cols = [
            c
            for c in ("backup_cover_path", "backup_offline_path", "cover_path")
            if c in cols
        ]
        for old, new in mapping:
            if old == new:
                continue
            for col in path_cols:
                rows = con.execute(
                    f"SELECT mod_id, {col} AS p FROM mods "
                    f"WHERE {col} LIKE ? OR {col} LIKE ?",
                    (
                        f"%mod_backup/{old}/%",
                        f"%mod_backup\\{old}\\%",
                    ),
                ).fetchall()
                for row in rows:
                    mid = int(row["mod_id"])
                    rewritten = _rewrite_backup_path(str(row["p"] or ""), old, new)
                    if rewritten != str(row["p"] or ""):
                        con.execute(
                            f"UPDATE mods SET {col} = ? WHERE mod_id = ?",
                            (rewritten, mid),
                        )

        # Also rewrite paths on the row itself when new id folder segment must match
        for old, new in mapping:
            if old == new:
                continue
            for col in path_cols:
                row = con.execute(
                    f"SELECT {col} AS p FROM mods WHERE mod_id = ?", (new,)
                ).fetchone()
                if row is None:
                    continue
                rewritten = _rewrite_backup_path(str(row["p"] or ""), old, new)
                if rewritten != str(row["p"] or ""):
                    con.execute(
                        f"UPDATE mods SET {col} = ? WHERE mod_id = ?",
                        (rewritten, new),
                    )

        con.execute("DROP TABLE _mod_id_map")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.execute("PRAGMA foreign_keys = ON")
        raise

    con.execute("PRAGMA foreign_keys = ON")
    return stats


def validate_db(con: sqlite3.Connection, before: dict[str, Any]) -> dict[str, Any]:
    rows = con.execute(
        "SELECT mod_id, internal_id, workspace_id FROM mods ORDER BY mod_id"
    ).fetchall()
    ids = [int(r["mod_id"]) for r in rows]
    n = len(ids)
    expected = list(range(1, n + 1))
    result: dict[str, Any] = {
        "mods": n,
        "min": ids[0] if ids else None,
        "max": ids[-1] if ids else None,
        "set_equals_1_to_n": ids == expected,
        "duplicate_count": n - len(set(ids)),
        "gap_count": 0 if ids == expected else len(set(expected) - set(ids)),
    }
    before_iids = {e["internal_id"] for e in before["entities"]}
    after_iids = {str(r["internal_id"] or "") for r in rows}
    result["internal_id_set_equal"] = before_iids == after_iids
    result["internal_id_changed_count"] = len(before_iids.symmetric_difference(after_iids))

    before_ws = {
        (e["internal_id"], e["workspace_id"]) for e in before["entities"]
    }
    after_ws = {
        (str(r["internal_id"] or ""), str(r["workspace_id"] or "")) for r in rows
    }
    result["workspace_pairs_equal"] = before_ws == after_ws

    orphans: dict[str, int] = {}
    for table, col in CHILD_REMAPS:
        if not _table_exists(con, table):
            continue
        orphan = int(
            con.execute(
                f"""
                SELECT COUNT(*) AS n FROM {table} t
                WHERE NOT EXISTS (
                    SELECT 1 FROM mods m WHERE m.mod_id = t.{col}
                )
                """
            ).fetchone()["n"]
        )
        orphans[f"{table}.{col}"] = orphan
        count = int(con.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
        expected_count = before["fk_counts"].get(table)
        if expected_count is not None and count != expected_count:
            result[f"{table}_count_mismatch"] = {"before": expected_count, "after": count}
    result["fk_orphans"] = orphans
    result["fk_orphan_total"] = sum(orphans.values())

    integ = con.execute("PRAGMA integrity_check").fetchone()[0]
    fk_rows = list(con.execute("PRAGMA foreign_key_check"))
    result["integrity_check"] = integ
    result["foreign_key_check_rows"] = len(fk_rows)
    result["foreign_key_check"] = "PASS" if not fk_rows else "FAIL"
    return result


def quarantine_orphan_backups(
    backup_root: Path, valid_ids: set[int], quarantine: Path
) -> list[str]:
    moved: list[str] = []
    if not backup_root.is_dir():
        return moved
    quarantine.mkdir(parents=True, exist_ok=True)
    for child in list(backup_root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if not name.isdigit() or int(name) not in valid_ids:
            dest = quarantine / name
            if dest.exists():
                dest = quarantine / f"{name}_{_utc()}"
            shutil.move(str(child), str(dest))
            moved.append(name)
    return moved


def remap_backup_dirs(
    backup_root: Path, mapping: list[tuple[int, int]], *, apply: bool
) -> dict[str, Any]:
    """Two-phase rename to avoid collisions."""
    stats = {"renamed": 0, "unchanged": 0, "missing_src": 0, "phases": []}
    if not backup_root.is_dir():
        return stats
    tmp_prefix = "__mod_id_remap__"
    # Phase 1: old → temp
    for old, new in mapping:
        src = backup_root / str(old)
        if old == new:
            if src.is_dir():
                stats["unchanged"] += 1
            else:
                stats["missing_src"] += 1
            continue
        if not src.is_dir():
            stats["missing_src"] += 1
            continue
        tmp = backup_root / f"{tmp_prefix}{new}"
        stats["phases"].append({"phase": 1, "from": str(old), "to": tmp.name})
        if apply:
            if tmp.exists():
                raise RuntimeError(f"temp backup exists: {tmp}")
            src.rename(tmp)
        stats["renamed"] += 1
    # Phase 2: temp → final
    for old, new in mapping:
        if old == new:
            continue
        tmp = backup_root / f"{tmp_prefix}{new}"
        final = backup_root / str(new)
        if not tmp.is_dir():
            continue
        stats["phases"].append({"phase": 2, "from": tmp.name, "to": str(new)})
        if apply:
            if final.exists():
                raise RuntimeError(f"target backup exists: {final}")
            tmp.rename(final)
    return stats


def validate_backup(backup_root: Path, con: sqlite3.Connection) -> dict[str, Any]:
    ids = {int(r["mod_id"]) for r in con.execute("SELECT mod_id FROM mods")}
    dirs = {
        int(p.name)
        for p in backup_root.iterdir()
        if p.is_dir() and p.name.isdigit()
    } if backup_root.is_dir() else set()
    non = [
        p.name
        for p in backup_root.iterdir()
        if p.is_dir() and not p.name.isdigit()
    ] if backup_root.is_dir() else []
    return {
        "backup_dirs": len(dirs),
        "mods": len(ids),
        "orphan_dirs": sorted(dirs - ids),
        "missing_backup_for_mod": sorted(ids - dirs),
        "non_numeric": non,
        "set_equality": dirs == ids or (
            # allow mods without backup (missing) only if no orphans
            not (dirs - ids) and not non
        ),
        "strict_set_equality": dirs == ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    if args.apply and not args.confirm:
        print("Refusing --apply without --confirm", file=sys.stderr)
        return 2
    if not args.dry_run and not args.apply:
        print("Specify --dry-run or --apply --confirm", file=sys.stderr)
        return 2

    # Release app singleton if held
    try:
        from core.db_manager import DatabaseManager

        DatabaseManager.reset_instance()
    except Exception:
        pass

    args.out.mkdir(parents=True, exist_ok=True)
    con = _connect(args.db)
    before = snapshot_before(con, args.backup_root, args.out)
    mapping = build_mapping(con)
    print(
        f"BEFORE mods={before['mods']} min={before['min_mod_id']} "
        f"max={before['max_mod_id']} gaps={before['gap_count']} "
        f"high={before['high_id_rows']} backup_dirs={before['backup_dirs']} "
        f"orphans={before['backup_orphan_vs_db']}"
    )
    print(f"Mapping pairs={len(mapping)} changed={sum(1 for o,n in mapping if o!=n)}")

    if args.dry_run:
        print("DRY-RUN only — no DB/filesystem changes")
        con.close()
        return 0

    # File backup of DB
    db_bak = args.out / f"mod_manager.db.pre_continuity_{_utc()}"
    shutil.copy2(args.db, db_bak)
    print(f"DB backup → {db_bak}")

    # Quarantine orphan backup dirs (not in current mods)
    valid_old = {o for o, _ in mapping}
    moved = quarantine_orphan_backups(
        args.backup_root, valid_old, args.out / "orphan_backup"
    )
    print(f"Quarantined orphan backups: {moved}")

    stats = migrate_db(con, mapping)
    print("DB migrate:", json.dumps(stats, ensure_ascii=False))

    # Remap filesystem using OLD names still on disk
    fs_stats = remap_backup_dirs(args.backup_root, mapping, apply=True)
    print(
        f"Backup rename renamed={fs_stats['renamed']} "
        f"unchanged={fs_stats['unchanged']} missing={fs_stats['missing_src']}"
    )

    after = validate_db(con, before)
    bak = validate_backup(args.backup_root, con)
    report = {
        "generated_at": _utc(),
        "db_backup": str(db_bak),
        "quarantined_orphans": moved,
        "migrate_stats": stats,
        "backup_rename": {
            k: v for k, v in fs_stats.items() if k != "phases"
        },
        "validate_db": after,
        "validate_backup": bak,
    }
    (args.out / "snapshot_after.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("AFTER DB:", json.dumps(after, ensure_ascii=False))
    print("AFTER Backup:", json.dumps(bak, ensure_ascii=False))

    ok = (
        after.get("set_equals_1_to_n")
        and after.get("internal_id_changed_count") == 0
        and after.get("workspace_pairs_equal")
        and after.get("fk_orphan_total") == 0
        and after.get("integrity_check") == "ok"
        and after.get("foreign_key_check") == "PASS"
        and bak.get("orphan_dirs") == []
        and bak.get("non_numeric") == []
    )
    # Prefer strict backup equality; missing backups for some mods are allowed
    # only if documented — Gate asks set equality with current mods that have
    # backups. Strict: dirs ⊆ mods and no orphans. Full equality if before had
    # every mod backed up.
    if before["db_missing_backup"] == [] and before["backup_orphan_vs_db"] == []:
        # after quarantine + remap, expect exact equality
        ok = ok and bak.get("strict_set_equality")
    else:
        ok = ok and not bak.get("orphan_dirs") and bak["backup_dirs"] <= bak["mods"]

    con.close()
    print("Verdict:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
