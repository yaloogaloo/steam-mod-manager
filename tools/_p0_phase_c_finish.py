#!/usr/bin/env python3
"""Phase C remaining verification after approved URL scrubs.

Does not re-scrub. Runs 3054 sidecar reload, identity snapshots, static guards.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(r"D:\project\steam-mod-manager")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROD_DB = ROOT / "data" / "mod_manager.db"
BACKUP = ROOT / "data" / "identity_lifecycle_production_backup" / "20260903T053206Z"
APPROVED = (
    "9000000000003054",
    "9000000000000354",
    "9000000000000360",
    "9000000000000361",
    "9000000000000362",
    "9000000000003031",
    "9000000000003225",
)
KEEP = (
    "9000000000003456",
    "9000000000003460",
    "9000000000000394",
    "9000000000003381",
    "9000000000003091",
    "9000000000003222",
    "9000000000003204",
    "9000000000003331",
)


def embed(url: str, mid: str) -> bool:
    compact = (url or "").replace(" ", "")
    return (
        "steamcommunity.com/sharedfiles/filedetails" in (url or "").lower()
        and f"id={mid}" in compact
    )


def identity_tuple(row: sqlite3.Row) -> tuple:
    return (
        str(row["mod_id"]),
        str(row["platform"] or ""),
        int(row["app_id"] or 0),
        str(row["workspace_id"] or ""),
        str(row["external_id"] or ""),
    )


def fetch(conn: sqlite3.Connection, mid: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT mod_id, platform, app_id, workspace_id, external_id, source_url,
               last_known_path, title
        FROM mods WHERE CAST(mod_id AS TEXT)=?
        """,
        (mid,),
    ).fetchone()


def snapshot_all(conn: sqlite3.Connection) -> dict[str, tuple]:
    out: dict[str, tuple] = {}
    for mid in (*APPROVED, *KEEP):
        row = fetch(conn, mid)
        if row is None:
            raise SystemExit(f"STOP missing {mid}")
        out[mid] = identity_tuple(row)
    return out


def main() -> int:
    conn = sqlite3.connect(str(PROD_DB))
    conn.row_factory = sqlite3.Row
    try:
        before_count = conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
        if conn.execute("SELECT 1 FROM mods WHERE mod_id=10782").fetchone() is not None:
            print("STOP steam PK 10782 present")
            return 3
        before_ident = snapshot_all(conn)
        leftover = []
        sidecar_dirty = []
        folder_3054 = None
        for mid in APPROVED:
            row = fetch(conn, mid)
            if embed(str(row["source_url"] or ""), mid):
                leftover.append(mid)
            folder = Path(row["last_known_path"] or "")
            if mid == "9000000000003054":
                folder_3054 = folder
            meta = folder / ".info" / "metadata.json"
            data = json.loads(meta.read_text(encoding="utf-8"))
            if embed(str(data.get("url") or data.get("source_url") or ""), mid):
                sidecar_dirty.append(mid)
        if leftover or sidecar_dirty:
            print("STOP leftover pollution", leftover, sidecar_dirty)
            return 4
        keep_urls = {
            "9000000000003456": "https://www.nexusmods.com/witcher3/mods/10782",
            "9000000000003460": "https://www.nexusmods.com/witcher3/mods/10881",
        }
        for mid, expected in keep_urls.items():
            row = fetch(conn, mid)
            if str(row["source_url"] or "") != expected:
                print("STOP keep url drift", mid, row["source_url"])
                return 4
    finally:
        conn.close()

    from core.db_manager import DatabaseManager
    from services.info_sidecar import apply_sidecar_to_db
    from services.mod_identity import source_url_embeds_internal

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(PROD_DB)
    apply_ok = apply_sidecar_to_db(folder_3054, mod_id="9000000000003054", db=db)
    url_after = str(db.get_mod_display_info("9000000000003054").source_url or "")
    rehydrated = source_url_embeds_internal(url_after, internal_pk="9000000000003054")
    info_3456 = db.get_mod_display_info("9000000000003456")
    info_3460 = db.get_mod_display_info("9000000000003460")
    steam2 = db.get_mod("10782")
    DatabaseManager.reset_instance()

    sidecar_3054 = json.loads(
        (folder_3054 / ".info" / "metadata.json").read_text(encoding="utf-8")
    )
    sidecar_url = str(sidecar_3054.get("url") or sidecar_3054.get("source_url") or "")

    from services.identity_invariants import (
        scan_id_architecture_source,
        scan_reconcile_identity_lifecycle,
    )

    life = scan_reconcile_identity_lifecycle(ROOT)
    arch = scan_id_architecture_source(ROOT)

    conn = sqlite3.connect(PROD_DB.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    after_ident = snapshot_all(conn)
    ident_drift = [mid for mid in before_ident if after_ident[mid] != before_ident[mid]]
    leftover = []
    sidecar_dirty = []
    for mid in APPROVED:
        row = fetch(conn, mid)
        if embed(str(row["source_url"] or ""), mid):
            leftover.append(mid)
        meta = Path(row["last_known_path"]) / ".info" / "metadata.json"
        data = json.loads(meta.read_text(encoding="utf-8"))
        if embed(str(data.get("url") or data.get("source_url") or ""), mid):
            sidecar_dirty.append(mid)
    count_final = conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    row3456 = dict(fetch(conn, "9000000000003456"))
    row3054 = dict(fetch(conn, "9000000000003054"))
    row3460 = dict(fetch(conn, "9000000000003460"))
    collisions = {
        "1720": [
            dict(fetch(conn, "9000000000000394")),
            dict(fetch(conn, "9000000000003381")),
        ],
        "6183": [
            dict(fetch(conn, "9000000000003091")),
            dict(fetch(conn, "9000000000003222")),
        ],
        "97": [
            dict(fetch(conn, "9000000000003204")),
            dict(fetch(conn, "9000000000003331")),
        ],
    }
    conn.close()

    stopped = bool(
        rehydrated
        or leftover
        or sidecar_dirty
        or ident_drift
        or count_final != before_count
        or steam2 is not None
        or len(life)
        or len(arch)
        or not apply_ok
        or embed(sidecar_url, "9000000000003054")
    )
    mutations = []
    mut_path = BACKUP / "mutations.jsonl"
    if mut_path.is_file():
        mutations = [
            json.loads(line)
            for line in mut_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    report = {
        "status": "STOPPED" if stopped else "PRODUCTION_REPAIRED",
        "backup": str(BACKUP),
        "apply_sidecar_ok": apply_ok,
        "rehydrated": rehydrated,
        "url_after_sidecar_reload": url_after,
        "sidecar_3054_url": sidecar_url,
        "leftover_db": leftover,
        "leftover_sidecar": sidecar_dirty,
        "identity_drift": ident_drift,
        "mods_count_before": before_count,
        "mods_count_after": count_final,
        "steam_pk_10782": steam2 is not None,
        "3456": {
            "internal_id": str(info_3456.mod_id),
            "workspace_id": info_3456.workspace_id,
            "platform": info_3456.platform,
            "app_id": info_3456.app_id,
            "external_id": info_3456.external_id,
            "source_url": row3456["source_url"],
        },
        "3054": {
            "internal_id": str(row3054["mod_id"]),
            "workspace_id": row3054["workspace_id"],
            "platform": row3054["platform"],
            "app_id": row3054["app_id"],
            "external_id": row3054["external_id"],
            "source_url": row3054["source_url"],
        },
        "3460": {
            "internal_id": str(info_3460.mod_id),
            "workspace_id": info_3460.workspace_id,
            "platform": info_3460.platform,
            "app_id": info_3460.app_id,
            "external_id": info_3460.external_id,
            "source_url": row3460["source_url"],
        },
        "collisions": collisions,
        "static_lifecycle": len(life),
        "static_arch": len(arch),
        "mutations": mutations,
    }
    BACKUP.mkdir(parents=True, exist_ok=True)
    (BACKUP / "apply_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    printable = {k: report[k] for k in report if k != "mutations"}
    print(json.dumps(printable, ensure_ascii=False, indent=2, default=str))
    if stopped:
        return 6
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
