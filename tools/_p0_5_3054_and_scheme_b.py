#!/usr/bin/env python3
"""P0-5: 3054 sidecar reload + Anno overlap diagnostic (persist=False) + identity snapshot."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(r"D:\project\steam-mod-manager")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROD_DB = ROOT / "data" / "mod_manager.db"
IDS = (
    "9000000000003054",
    "9000000000003456",
    "9000000000003460",
    "9000000000003461",
    "9000000000003462",
    "9000000000000360",
    "9000000000000362",
)


def ident(conn: sqlite3.Connection, mid: str) -> tuple:
    r = conn.execute(
        "SELECT mod_id, platform, app_id, workspace_id, external_id, source_url "
        "FROM mods WHERE CAST(mod_id AS TEXT)=?",
        (mid,),
    ).fetchone()
    return tuple(r)


def main() -> int:
    conn = sqlite3.connect(str(PROD_DB))
    conn.row_factory = sqlite3.Row
    before_count = conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    before = {mid: ident(conn, mid) for mid in IDS}
    row3054 = conn.execute(
        "SELECT last_known_path, source_url FROM mods WHERE CAST(mod_id AS TEXT)=?",
        ("9000000000003054",),
    ).fetchone()
    folder = Path(row3054["last_known_path"])
    conn.close()

    from core.db_manager import DatabaseManager
    from services.info_sidecar import apply_sidecar_to_db
    from services.mod_identity import source_url_embeds_internal

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(PROD_DB)
    ok = apply_sidecar_to_db(folder, mod_id="9000000000003054", db=db)
    info = db.get_mod_display_info("9000000000003054")
    url_after = str(info.source_url or "") if info else "MISSING"
    rehydrated = source_url_embeds_internal(url_after, internal_pk="9000000000003054")
    steam = db.get_mod("10782")
    DatabaseManager.reset_instance()

    sidecar = json.loads((folder / ".info" / "metadata.json").read_text(encoding="utf-8"))
    sidecar_url = str(sidecar.get("url") or sidecar.get("source_url") or "")

    conn = sqlite3.connect(PROD_DB.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    after_count = conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    after = {mid: ident(conn, mid) for mid in IDS}
    drift = [mid for mid in IDS if before[mid] != after[mid]]
    conn.close()

    from services.conflict import ConflictDetector, ConflictType
    from core.db_manager import DatabaseManager as DM

    DM.reset_instance()
    db2 = DM.instance(PROD_DB)
    report = ConflictDetector(ROOT / "mod", db=db2).check_mod(
        "9000000000000362", persist=False
    )
    ow = [c for c in report.conflicts if c.conflict_type == ConflictType.FILE_OVERWRITE.value]
    partners = sorted({m for c in ow for m in c.mods})
    DM.reset_instance()

    conn = sqlite3.connect(PROD_DB.resolve().as_uri() + "?mode=ro", uri=True)
    after2_count = conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    c0362 = conn.execute(
        "SELECT conflict_status FROM mods WHERE CAST(mod_id AS TEXT)=?",
        ("9000000000000362",),
    ).fetchone()[0]
    c0360 = conn.execute(
        "SELECT conflict_status FROM mods WHERE CAST(mod_id AS TEXT)=?",
        ("9000000000000360",),
    ).fetchone()[0]
    rel_n = conn.execute("SELECT COUNT(*) FROM mod_relationships").fetchone()[0]
    conn.close()

    out = {
        "apply_ok": ok,
        "rehydrated": rehydrated,
        "url_after": url_after,
        "sidecar_url": sidecar_url,
        "steam_10782": steam is not None,
        "mods_count": (before_count, after_count, after2_count),
        "identity_drift": drift,
        "anno_overwrite_count": len(ow),
        "anno_report_status": report.status,
        "anno_partners": partners,
        "db_conflict_0360": c0360,
        "db_conflict_0362": c0362,
        "mod_relationships_count": rel_n,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    if rehydrated or drift or after_count != before_count or after2_count != before_count:
        return 6
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
