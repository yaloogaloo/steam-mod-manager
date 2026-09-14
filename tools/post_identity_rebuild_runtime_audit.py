#!/usr/bin/env python3
"""Phase 1 — read-only audit of Runtime impact after Identity full rebuild.

Never mutates DB / .info / lifecycle contracts.

Usage::

    python tools/post_identity_rebuild_runtime_audit.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "tools" / "_audit_out"
DEFAULT_DB = ROOT / "data" / "mod_manager.db"
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


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def _cols(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def _count(con: sqlite3.Connection, table: str) -> int:
    if not _table_exists(con, table):
        return -1
    return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _sample_index_html(library: Path, limit: int = 200) -> dict[str, int]:
    has = 0
    missing = 0
    scanned = 0
    if not library.is_dir():
        return {"scanned": 0, "has_index": 0, "missing_index": 0}
    for game in library.iterdir():
        if not game.is_dir() or game.name.startswith("."):
            continue
        for mod in game.iterdir():
            if not mod.is_dir() or mod.name.startswith("."):
                continue
            scanned += 1
            if scanned > limit:
                break
            ok = (mod / ".info" / "index.html").is_file() or (
                mod / ".info" / "offline" / "index.html"
            ).is_file()
            if ok:
                has += 1
            else:
                missing += 1
        if scanned > limit:
            break
    return {"scanned": scanned, "has_index": has, "missing_index": missing}


def build_audit(*, db_path: Path, library: Path) -> dict[str, Any]:
    backup = _latest_backup(db_path.parent)
    live = _connect(db_path)
    bak = _connect(backup) if backup and backup.is_file() else None

    tables_of_interest = [
        "mods",
        "deployment_records",
        "deployment_record_items",
        "mod_tags",
        "game_categories",
        "mod_relations",
        "mod_relationships",
        "identity_audit_log",
        "games",
    ]

    live_tables: dict[str, Any] = {}
    for t in tables_of_interest:
        live_tables[t] = {
            "exists": _table_exists(live, t),
            "count": _count(live, t),
            "columns": _cols(live, t) if _table_exists(live, t) else [],
        }

    bak_tables: dict[str, Any] = {}
    if bak is not None:
        for t in tables_of_interest:
            bak_tables[t] = {
                "exists": _table_exists(bak, t),
                "count": _count(bak, t),
                "columns": _cols(bak, t) if _table_exists(bak, t) else [],
            }

    # Which tables reference old integer mod_id vs UUID internal_id.
    refs_old_mod_id = []
    refs_internal_id = []
    for t, meta in live_tables.items():
        cols = set(meta.get("columns") or [])
        if "mod_id" in cols or "source_mod_id" in cols or "target_mod_id" in cols:
            refs_old_mod_id.append(
                {
                    "table": t,
                    "columns": sorted(
                        c
                        for c in cols
                        if c in {"mod_id", "source_mod_id", "target_mod_id"}
                    ),
                }
            )
        if "internal_id" in cols:
            refs_internal_id.append({"table": t, "columns": ["internal_id"]})

    # Lost associations vs backup.
    lost: dict[str, Any] = {}
    live_fav = int(
        live.execute("SELECT COUNT(*) FROM mods WHERE favorite=1").fetchone()[0]
    )
    live_enabled0 = int(
        live.execute("SELECT COUNT(*) FROM mods WHERE enabled=0").fetchone()[0]
    )
    if bak is not None:
        for t in (
            "deployment_records",
            "deployment_record_items",
            "mod_tags",
            "game_categories",
            "mod_relationships",
            "mod_relations",
        ):
            lost[t] = {
                "backup_count": bak_tables.get(t, {}).get("count", 0),
                "live_count": live_tables.get(t, {}).get("count", 0),
                "lost": max(
                    0,
                    int(bak_tables.get(t, {}).get("count") or 0)
                    - int(live_tables.get(t, {}).get("count") or 0),
                ),
            }

        bak_fav = int(
            bak.execute("SELECT COUNT(*) FROM mods WHERE favorite=1").fetchone()[0]
        )
        lost["favorites"] = {
            "backup_count": bak_fav,
            "live_count": live_fav,
            "lost": max(0, bak_fav - live_fav),
        }

        bak_enabled0 = int(
            bak.execute("SELECT COUNT(*) FROM mods WHERE enabled=0").fetchone()[0]
        )
        lost["disabled"] = {
            "backup_count": bak_enabled0,
            "live_count": live_enabled0,
            "lost": max(0, bak_enabled0 - live_enabled0),
        }

    # Wrongly initialized status fields on live.
    offline_dist = [
        {"offline_status": r[0], "count": r[1]}
        for r in live.execute(
            "SELECT offline_status, COUNT(*) FROM mods GROUP BY offline_status"
        )
    ]
    deploy_dist = [
        {"deploy_status": r[0], "count": r[1]}
        for r in live.execute(
            "SELECT deploy_status, COUNT(*) FROM mods GROUP BY deploy_status"
        )
    ]
    cover_empty = int(
        live.execute(
            "SELECT COUNT(*) FROM mods WHERE TRIM(COALESCE(cover_path,''))=''"
        ).fetchone()[0]
    )
    uuid_ok = int(
        live.execute(
            "SELECT COUNT(*) FROM mods WHERE TRIM(COALESCE(internal_id,'')) LIKE '%-%-%-%-%'"
        ).fetchone()[0]
    )
    pollution = int(
        live.execute(
            """
            SELECT COUNT(*) FROM mods
            WHERE CAST(mod_id AS TEXT) LIKE '900000000000%'
               OR TRIM(COALESCE(internal_id,'')) LIKE '900000000000%'
            """
        ).fetchone()[0]
    )

    index_sample = _sample_index_html(library)

    # Path lifecycle notes (static).
    path_lifecycle = {
        "primary_resolver": "services.path_lifecycle.resolve_managed_folder",
        "deploy_entry": "services.deploy_paths.resolve_deploy_managed_path",
        "discovery_by_info": "services.path_lifecycle.discover_folder_by_internal_id",
        "last_known_path_role": "cache/hint — must be proven by .info/entity_key",
        "risk": (
            "Deploy/Library still pass integer mods.mod_id as runtime token; "
            "UUID lives in mods.internal_id; filesystem proof is .info/entity_key "
            "(same value — not a third Mod ID). "
            "Stale last_known_path without successful .info scan fails resolution."
        ),
    }

    answers = {
        "1_tables_still_reference_old_mod_id": refs_old_mod_id,
        "2_tables_reference_internal_id_uuid_column": refs_internal_id,
        "3_tables_lost_association_after_rebuild": lost,
        "4_status_fields_wrongly_initialized": {
            "offline_status_distribution": offline_dist,
            "deploy_status_distribution": deploy_dist,
            "favorites_live": live_fav if bak else None,
            "cover_path_empty": cover_empty,
            "note": (
                "Rebuild INSERT defaulted offline_status='none', favorite=0, "
                "deploy_status='not_deployed', cleared tags/deploy items/categories. "
                "UI Offline badge shows when offline_status is none/failed and "
                "has_offline is false — even if .info/index.html exists on disk."
            ),
            "index_html_sample": index_sample,
        },
    }

    report = {
        "generated_at": _now(),
        "tool": "post_identity_rebuild_runtime_audit",
        "live_db": str(db_path.resolve()),
        "backup_db": str(backup.resolve()) if backup else "",
        "library_root": str(library.resolve()),
        "live_mods": live_tables.get("mods", {}).get("count"),
        "uuid_internal_id_count": uuid_ok,
        "pollution_9000_count": pollution,
        "live_tables": live_tables,
        "backup_tables": bak_tables,
        "path_lifecycle": path_lifecycle,
        "answers": answers,
    }

    live.close()
    if bak is not None:
        bak.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT_DIR / "post_identity_rebuild_runtime_audit.json",
    )
    args = parser.parse_args()
    report = build_audit(db_path=args.db, library=args.library)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote={args.out}")
    lost = report["answers"]["3_tables_lost_association_after_rebuild"]
    print(f"lost={ {k: v.get('lost') for k, v in lost.items()} }")
    print(f"offline={report['answers']['4_status_fields_wrongly_initialized']['offline_status_distribution']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
