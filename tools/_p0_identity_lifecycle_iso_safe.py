#!/usr/bin/env python3
"""Safe isolated rehydrate + 10782 bind test. Copy DB/folders only; rewrite paths."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.db_manager import DatabaseManager  # noqa: E402
from services.identity_repair import open_readonly_sqlite  # noqa: E402
from services.info_sidecar import apply_sidecar_to_db  # noqa: E402
from services.library_reconcile import reconcile_library  # noqa: E402
from services.mod_identity import source_url_embeds_internal  # noqa: E402
from services.mod_metadata_resolver import list_visible_mods  # noqa: E402

PROD_DB = ROOT / "data" / "mod_manager.db"
PROD_LIB = ROOT / "mod"
MID_3054 = "9000000000003054"
MID_3456 = "9000000000003456"
WS = "10782"


def _prod_snapshot() -> dict:
    db = open_readonly_sqlite(PROD_DB)
    conn = db._conn  # noqa: SLF001
    try:
        row3456 = conn.execute(
            "SELECT mod_id, platform, workspace_id, source_url, updated_at FROM mods WHERE mod_id=?",
            (int(MID_3456),),
        ).fetchone()
        row3054 = conn.execute(
            "SELECT mod_id, platform, workspace_id, source_url, updated_at FROM mods WHERE mod_id=?",
            (int(MID_3054),),
        ).fetchone()
        steam = conn.execute("SELECT 1 FROM mods WHERE mod_id=10782").fetchone()
        mx = conn.execute("SELECT MAX(updated_at) AS m FROM mods").fetchone()["m"]
        audit = conn.execute(
            "SELECT MAX(created_at) AS m FROM identity_audit_log"
        ).fetchone()["m"]
        return {
            "3456": dict(row3456) if row3456 else None,
            "3054": dict(row3054) if row3054 else None,
            "steam_10782": steam is not None,
            "max_mods_updated_at": mx,
            "max_audit_at": audit,
            "db_mtime": PROD_DB.stat().st_mtime,
        }
    finally:
        conn.close()


def main() -> int:
    snap = _prod_snapshot()
    tmp = Path(tempfile.mkdtemp(prefix="smm_p0_iso_safe_"))
    iso_data = tmp / "data"
    iso_lib = tmp / "mod"
    iso_data.mkdir()
    iso_db = iso_data / "mod_manager.db"
    shutil.copy2(PROD_DB, iso_db)

    db_ro = open_readonly_sqlite(PROD_DB)
    c = db_ro._conn  # noqa: SLF001
    p3054 = Path(
        c.execute(
            "SELECT last_known_path FROM mods WHERE mod_id=?", (int(MID_3054),)
        ).fetchone()["last_known_path"]
    )
    p3456 = Path(
        c.execute(
            "SELECT last_known_path FROM mods WHERE mod_id=?", (int(MID_3456),)
        ).fetchone()["last_known_path"]
    )
    c.close()

    dest_3054 = iso_lib / p3054.parent.name / p3054.name
    dest_3456 = iso_lib / p3456.parent.name / p3456.name
    dest_3054.parent.mkdir(parents=True, exist_ok=True)
    dest_3456.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(p3054, dest_3054)
    shutil.copytree(p3456, dest_3456)

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(iso_db)
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET last_known_path='', folder_present=0"
        )
        db._conn.execute(
            "UPDATE mods SET last_known_path=?, folder_present=1 WHERE mod_id=?",
            (str(dest_3054.resolve()), int(MID_3054)),
        )
        db._conn.execute(
            "UPDATE mods SET last_known_path=?, folder_present=1 WHERE mod_id=?",
            (str(dest_3456.resolve()), int(MID_3456)),
        )
        db._conn.execute(
            "UPDATE mods SET source_url='' WHERE mod_id=?",
            (int(MID_3054),),
        )
        db._conn.commit()

    sidecar_before = json.loads(
        (dest_3054 / ".info" / "metadata.json").read_text(encoding="utf-8")
    )
    assert "9000000000003054" in str(sidecar_before.get("url") or "")

    import core.paths as paths_mod
    import services.metadata_backup as bak_mod

    paths_mod.data_dir = lambda: iso_data
    paths_mod.default_mod_library = lambda: iso_lib
    bak_mod.data_dir = lambda: iso_data

    apply_ok = apply_sidecar_to_db(dest_3054, mod_id=MID_3054, db=db)
    url_after_sidecar = db._conn.execute(
        "SELECT source_url FROM mods WHERE mod_id=?", (int(MID_3054),)
    ).fetchone()["source_url"]

    r1 = reconcile_library(iso_lib)
    r2 = reconcile_library(iso_lib)

    row3054 = dict(
        db._conn.execute(
            "SELECT mod_id, platform, workspace_id, source_url FROM mods WHERE mod_id=?",
            (int(MID_3054),),
        ).fetchone()
    )
    row3456 = dict(
        db._conn.execute(
            "SELECT mod_id, platform, workspace_id, source_url FROM mods WHERE mod_id=?",
            (int(MID_3456),),
        ).fetchone()
    )
    steam = db._conn.execute("SELECT 1 FROM mods WHERE mod_id=10782").fetchone()
    count = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    vis_w3 = [
        {"id": str(m.published_file_id), "ws": str(m.workspace_id)}
        for m in list_visible_mods(iso_lib, p3456.parent.name)
    ]
    vis_bg3 = [
        {"id": str(m.published_file_id), "ws": str(m.workspace_id)}
        for m in list_visible_mods(iso_lib, p3054.parent.name)
    ]

    DatabaseManager.reset_instance()
    shutil.rmtree(tmp, ignore_errors=True)

    rehydrated = source_url_embeds_internal(
        str(row3054.get("source_url") or ""), internal_pk=MID_3054
    ) or source_url_embeds_internal(
        str(url_after_sidecar or ""), internal_pk=MID_3054
    )
    out = {
        "production_snapshot": {
            k: (dict(v) if hasattr(v, "keys") else v) for k, v in snap.items()
        },
        "apply_sidecar_ok": apply_ok,
        "url_after_sidecar_reload": url_after_sidecar,
        "url_after_two_reconciles": row3054.get("source_url"),
        "rehydrated": rehydrated,
        "steam_pk_10782": steam is not None,
        "3456_platform": row3456.get("platform"),
        "3456_workspace": row3456.get("workspace_id"),
        "imported": [r1.imported, r2.imported],
        "notes": (r1.notes + r2.notes)[:20],
        "mods_count": count,
        "visible_witcher3": vis_w3,
        "visible_bg3": vis_bg3,
        "PASS_REHYDRATE": not rehydrated,
        "PASS_10782": steam is None
        and str(row3456.get("workspace_id")) == WS
        and str(row3456.get("platform")) == "nexus"
        and str(row3456.get("mod_id")) == MID_3456
    }
    # fix PASS_10782
    out["PASS_10782"] = (
        steam is None
        and str(row3456.get("workspace_id")) == WS
        and str(row3456.get("platform")) == "nexus"
        and str(row3456.get("mod_id")) == MID_3456
    )
    out["PASS_VISIBLE_10782"] = [
        c for c in vis_w3 if c["ws"] == WS
    ] == [{"id": MID_3456, "ws": WS}]
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if out["PASS_REHYDRATE"] and out["PASS_10782"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
