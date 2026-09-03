#!/usr/bin/env python3
"""Phase C APPLY: SCRUB_INVALID_SOURCE_URL for 7 approved internal IDs only."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"D:\project\steam-mod-manager")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PROD_DB = ROOT / "data" / "mod_manager.db"
APPROVED = (
    "9000000000003054",
    "9000000000000354",
    "9000000000000360",
    "9000000000000361",
    "9000000000000362",
    "9000000000003031",
    "9000000000003225",
)
KEEP = ("9000000000003456", "9000000000003460")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = ROOT / "data" / "identity_lifecycle_production_backup" / ts
    backup.mkdir(parents=True, exist_ok=False)
    sidecar_dir = backup / "sidecars"
    sidecar_dir.mkdir()

    shutil.copy2(PROD_DB, backup / "mod_manager.db")
    wal = Path(str(PROD_DB) + "-wal")
    shm = Path(str(PROD_DB) + "-shm")
    if wal.is_file():
        shutil.copy2(wal, backup / "mod_manager.db-wal")
    if shm.is_file():
        shutil.copy2(shm, backup / "mod_manager.db-shm")
    plan_src = ROOT / "docs" / "P0_IDENTITY_LIFECYCLE_PRODUCTION_REPAIR_PLAN.json"
    pre_src = ROOT / "docs" / "P0_IDENTITY_LIFECYCLE_PREFLIGHT.json"
    if plan_src.is_file():
        shutil.copy2(plan_src, backup / "repair_plan.json")
    else:
        print("STOP backup: repair_plan missing")
        return 2
    if pre_src.is_file():
        shutil.copy2(pre_src, backup / "preflight.json")
    else:
        print("STOP backup: preflight missing")
        return 2

    mutations: list[dict] = []
    conn = sqlite3.connect(str(PROD_DB))
    conn.row_factory = sqlite3.Row
    try:
        before_count = conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
        steam = conn.execute("SELECT 1 FROM mods WHERE mod_id=10782").fetchone()
        if steam is not None:
            print("STOP steam PK 10782 present")
            return 3
        keep_before = {mid: identity_tuple(fetch(conn, mid)) for mid in KEEP}
        approved_before = {}
        for mid in APPROVED:
            row = fetch(conn, mid)
            if row is None:
                print("STOP missing", mid)
                return 3
            url = str(row["source_url"] or "")
            plat = str(row["platform"] or "").lower()
            if plat == "steam" or not embed(url, mid):
                print("STOP plan mismatch", mid, plat, url)
                return 3
            approved_before[mid] = {
                "identity": identity_tuple(row),
                "url": url,
                "path": str(row["last_known_path"] or ""),
            }
            folder = Path(row["last_known_path"] or "")
            meta = folder / ".info" / "metadata.json"
            if not meta.is_file():
                print("STOP sidecar missing", mid)
                return 3
            dest = sidecar_dir / mid
            dest.mkdir()
            shutil.copy2(meta, dest / "metadata.json")

        now = utc_now()
        for mid in APPROVED:
            row = fetch(conn, mid)
            old = str(row["source_url"] or "")
            cur = conn.execute(
                """
                UPDATE mods SET source_url = ''
                WHERE CAST(mod_id AS TEXT)=? AND source_url=?
                """,
                (mid, old),
            )
            if cur.rowcount != 1:
                print("STOP DB scrub rowcount", mid, cur.rowcount)
                conn.rollback()
                return 4
            after_row = fetch(conn, mid)
            if str(after_row["source_url"] or "") != "":
                print("STOP DB scrub failed", mid)
                conn.rollback()
                return 4
            if identity_tuple(after_row) != approved_before[mid]["identity"]:
                print("STOP identity changed", mid)
                conn.rollback()
                return 4
            conn.execute(
                """
                INSERT INTO identity_audit_log (
                    mod_id, field_name, old_value, new_value, source, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mid,
                    "source_url",
                    old,
                    "",
                    "p0_identity_lifecycle_phase_c",
                    "SCRUB_INVALID_SOURCE_URL: Internal-ID Steam filedetails URL",
                    now,
                ),
            )
            meta = Path(approved_before[mid]["path"]) / ".info" / "metadata.json"
            data = json.loads(meta.read_text(encoding="utf-8"))
            sidecar_old = str(data.get("url") or data.get("source_url") or "")
            sidecar_changed = False
            if embed(str(data.get("url") or ""), mid):
                data["url"] = ""
                sidecar_changed = True
            if "source_url" in data and embed(str(data.get("source_url") or ""), mid):
                data["source_url"] = ""
                sidecar_changed = True
            if sidecar_changed:
                meta.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            mutations.append(
                {
                    "timestamp": now,
                    "internal_id": mid,
                    "field": "source_url",
                    "db_before": old,
                    "db_after": "",
                    "sidecar_path": str(meta),
                    "sidecar_before": sidecar_old,
                    "sidecar_after": "" if sidecar_changed else sidecar_old,
                    "sidecar_updated": sidecar_changed,
                    "reason": "SCRUB_INVALID_SOURCE_URL",
                }
            )

        conn.commit()
        after_count = conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
        if after_count != before_count:
            print("STOP count changed", before_count, after_count)
            return 5
        for mid in KEEP:
            row = fetch(conn, mid)
            if identity_tuple(row) != keep_before[mid]:
                print("STOP keep identity changed", mid)
                return 5
            if mid == "9000000000003456":
                if str(row["source_url"] or "") != "https://www.nexusmods.com/witcher3/mods/10782":
                    print("STOP 10782 url changed")
                    return 5
        remaining = []
        for mid in APPROVED:
            row = fetch(conn, mid)
            if embed(str(row["source_url"] or ""), mid):
                remaining.append(mid)
        if remaining:
            print("STOP remaining polluted", remaining)
            return 5
    finally:
        conn.close()

    (backup / "mutations.jsonl").write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in mutations) + "\n",
        encoding="utf-8",
    )

    # 3054 controlled sidecar reload — use production DatabaseManager, then verify.
    from core.db_manager import DatabaseManager
    from services.info_sidecar import apply_sidecar_to_db
    from services.mod_identity import source_url_embeds_internal

    folder_3054 = Path(approved_before["9000000000003054"]["path"])
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(PROD_DB)
    apply_ok = apply_sidecar_to_db(folder_3054, mod_id="9000000000003054", db=db)
    url_after = str(db.get_mod_display_info("9000000000003054").source_url or "")
    rehydrated = source_url_embeds_internal(url_after, internal_pk="9000000000003054")
    info_3456 = db.get_mod_display_info("9000000000003456")
    info_3460 = db.get_mod_display_info("9000000000003460")
    steam2 = db.get_mod("10782")
    DatabaseManager.reset_instance()

    # post sidecar json
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

    # leftover internal steam urls among approved
    conn = sqlite3.connect(PROD_DB.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
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
    conn.close()

    report = {
        "status": "STOPPED" if rehydrated or leftover or sidecar_dirty else "PRODUCTION_REPAIRED",
        "backup": str(backup),
        "apply_sidecar_ok": apply_ok,
        "rehydrated": rehydrated,
        "url_after_sidecar_reload": url_after,
        "sidecar_3054_url": sidecar_url,
        "leftover_db": leftover,
        "leftover_sidecar": sidecar_dirty,
        "mods_count_before": before_count,
        "mods_count_after": count_final,
        "steam_pk_10782": steam2 is not None,
        "3456": {
            "internal_id": str(info_3456.mod_id),
            "workspace_id": info_3456.workspace_id,
            "platform": info_3456.platform,
            "app_id": info_3456.app_id,
        },
        "3054": dict(row3054),
        "3460": dict(row3460),
        "static_lifecycle": len(life),
        "static_arch": len(arch),
        "mutations": mutations,
    }
    (backup / "apply_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(json.dumps({k: report[k] for k in report if k != "mutations"}, ensure_ascii=False, indent=2, default=str))
    if rehydrated or leftover or sidecar_dirty or count_final != before_count:
        return 6
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
