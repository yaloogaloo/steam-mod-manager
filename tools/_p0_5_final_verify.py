#!/usr/bin/env python3
"""P0-5 production final verification. Read-only except optional 3054 sidecar reload."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"D:\project\steam-mod-manager")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROD_DB = ROOT / "data" / "mod_manager.db"
BACKUP_DB = ROOT / "data" / "identity_lifecycle_production_backup" / "20260903T053206Z" / "mod_manager.db"
LIB = ROOT / "mod"
APPROVED = (
    "9000000000003054",
    "9000000000000354",
    "9000000000000360",
    "9000000000000361",
    "9000000000000362",
    "9000000000003031",
    "9000000000003225",
)
KNOWN_EMPTY = (
    "6dec21cd",
    "9ec8fa2e",
    "fe039b69",
    "40938d22",
    "96215ee0",
    "bb554d94",
    "1702b3d1",
    "236caa23",
)
COLLISION_WS = ("1720", "6183", "97")
IDENT_FIELDS = ("platform", "app_id", "workspace_id", "external_id")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def embed(url: str, mid: str) -> bool:
    compact = (url or "").replace(" ", "")
    return (
        "steamcommunity.com/sharedfiles/filedetails" in (url or "").lower()
        and f"id={mid}" in compact
    )


def ident_tuple(row: sqlite3.Row) -> tuple:
    return (
        str(row["mod_id"]),
        str(row["platform"] or ""),
        int(row["app_id"] or 0),
        str(row["workspace_id"] or ""),
        str(row["external_id"] or ""),
    )


def snap_mods(path: Path) -> dict[str, dict]:
    conn = connect(path)
    rows = {}
    for r in conn.execute(
        """
        SELECT mod_id, platform, app_id, workspace_id, external_id, source_url,
               title, last_known_path, conflict_status, updated_at
        FROM mods
        """
    ):
        rows[str(r["mod_id"])] = dict(r)
    conn.close()
    return rows


def sidecar_url(folder: Path) -> str:
    meta = folder / ".info" / "metadata.json"
    if not meta.is_file():
        return ""
    data = json.loads(meta.read_text(encoding="utf-8"))
    return str(data.get("url") or data.get("source_url") or "")


def main() -> int:
    ts = utc_now()
    conn = connect(PROD_DB)
    count = conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    max_id = conn.execute("SELECT MAX(mod_id) FROM mods").fetchone()[0]
    max_updated = conn.execute("SELECT MAX(updated_at) FROM mods").fetchone()[0]
    platforms = dict(
        conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(platform), ''), '(empty)') p, COUNT(*) "
            "FROM mods GROUP BY 1 ORDER BY COUNT(*) DESC"
        ).fetchall()
    )
    app_ids = dict(
        conn.execute(
            "SELECT app_id, COUNT(*) FROM mods GROUP BY app_id ORDER BY COUNT(*) DESC"
        ).fetchall()
    )
    conflict_dist = dict(
        conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(conflict_status), ''), '(empty)') s, COUNT(*) "
            "FROM mods GROUP BY 1 ORDER BY COUNT(*) DESC"
        ).fetchall()
    )
    dup_internal = conn.execute(
        "SELECT COUNT(*) FROM (SELECT mod_id FROM mods GROUP BY mod_id HAVING COUNT(*)>1)"
    ).fetchone()[0]
    ws_dups = []
    for r in conn.execute(
        """
        SELECT workspace_id, COUNT(*) n,
               GROUP_CONCAT(CAST(mod_id AS TEXT)) ids,
               GROUP_CONCAT(DISTINCT platform) plats,
               GROUP_CONCAT(DISTINCT CAST(app_id AS TEXT)) apps
        FROM mods
        WHERE TRIM(COALESCE(workspace_id,'')) != ''
        GROUP BY workspace_id
        HAVING COUNT(*) > 1
        ORDER BY n DESC
        """
    ):
        ws_dups.append(dict(r))
    empty_ws = conn.execute(
        "SELECT COUNT(*) FROM mods WHERE TRIM(COALESCE(workspace_id,''))=''"
    ).fetchone()[0]
    empty_url = conn.execute(
        "SELECT COUNT(*) FROM mods WHERE TRIM(COALESCE(source_url,''))= ''"
    ).fetchone()[0]
    steam_internal_urls = []
    for r in conn.execute(
        "SELECT CAST(mod_id AS TEXT) mid, platform, source_url FROM mods "
        "WHERE source_url LIKE '%steamcommunity.com/sharedfiles/filedetails%'"
    ):
        if embed(str(r["source_url"] or ""), str(r["mid"])):
            steam_internal_urls.append(
                {"internal_id": r["mid"], "platform": r["platform"], "source_url": r["source_url"]}
            )
    audit_n = conn.execute(
        "SELECT COUNT(*) FROM identity_audit_log WHERE source=?",
        ("p0_identity_lifecycle_phase_c",),
    ).fetchone()[0]
    key_ids = (
        "9000000000003456",
        "9000000000003460",
        "9000000000003462",
        "9000000000003054",
        "9000000000000360",
        "9000000000000362",
        *APPROVED,
        "9000000000000394",
        "9000000000003381",
        "9000000000003091",
        "9000000000003222",
        "9000000000003204",
        "9000000000003331",
    )
    key_rows = {}
    for mid in key_ids:
        row = conn.execute(
            """
            SELECT mod_id, platform, app_id, workspace_id, external_id, source_url,
                   title, last_known_path, conflict_status, updated_at
            FROM mods WHERE CAST(mod_id AS TEXT)=?
            """,
            (mid,),
        ).fetchone()
        key_rows[mid] = dict(row) if row else None
    steam_10782 = conn.execute("SELECT 1 FROM mods WHERE mod_id=10782").fetchone()
    ws_10782 = [
        dict(r)
        for r in conn.execute(
            "SELECT mod_id, platform, app_id, workspace_id, external_id, source_url "
            "FROM mods WHERE TRIM(workspace_id)='10782'"
        )
    ]
    collisions = {}
    for ws in COLLISION_WS:
        collisions[ws] = [
            dict(r)
            for r in conn.execute(
                """
                SELECT mod_id, platform, app_id, workspace_id, external_id, source_url,
                       title, last_known_path
                FROM mods WHERE TRIM(workspace_id)=?
                """,
                (ws,),
            )
        ]
    conn.close()

    folders = []
    empty_mod_folders = []
    if LIB.is_dir():
        for game in sorted(LIB.iterdir()):
            if not game.is_dir() or game.name.startswith("."):
                continue
            for folder in sorted(game.iterdir()):
                if not folder.is_dir() or folder.name.startswith("."):
                    continue
                folders.append(str(folder))
                if folder.name.startswith("Empty Mod "):
                    suffix = folder.name.replace("Empty Mod ", "", 1)
                    empty_mod_folders.append(
                        {
                            "name": folder.name,
                            "suffix": suffix,
                            "path": str(folder),
                            "sidecar": (folder / ".info" / "metadata.json").is_file(),
                        }
                    )

    live = snap_mods(PROD_DB)
    bak = snap_mods(BACKUP_DB) if BACKUP_DB.is_file() else {}
    created = sorted(set(live) - set(bak), key=lambda x: int(x))
    deleted = sorted(set(bak) - set(live), key=lambda x: int(x))
    ident_chg = []
    url_chg = []
    for mid in set(live) & set(bak):
        a, b = bak[mid], live[mid]
        if tuple(str(a[f] or "") for f in IDENT_FIELDS) != tuple(
            str(b[f] or "") for f in IDENT_FIELDS
        ):
            ident_chg.append(
                {
                    "internal_id": mid,
                    "before": {f: a[f] for f in IDENT_FIELDS},
                    "after": {f: b[f] for f in IDENT_FIELDS},
                }
            )
        if str(a["source_url"] or "") != str(b["source_url"] or ""):
            url_chg.append(
                {
                    "internal_id": mid,
                    "before": a["source_url"],
                    "after": b["source_url"],
                }
            )
    approved_url = [u for u in url_chg if u["internal_id"] in APPROVED]
    other_url = [u for u in url_chg if u["internal_id"] not in APPROVED]

    scrub = []
    for mid in APPROVED:
        row = key_rows.get(mid)
        folder = Path(row["last_known_path"]) if row and row["last_known_path"] else None
        su = sidecar_url(folder) if folder else ""
        db_url = str(row["source_url"] or "") if row else None
        scrub.append(
            {
                "internal_id": mid,
                "db_source_url": db_url,
                "sidecar_url": su,
                "db_empty": db_url == "",
                "sidecar_empty": su == "",
                "identity": ident_tuple(
                    sqlite3.Row(
                        # placeholder unused
                    )
                )
                if False
                else (
                    None
                    if row is None
                    else (
                        str(row["mod_id"]),
                        str(row["platform"] or ""),
                        int(row["app_id"] or 0),
                        str(row["workspace_id"] or ""),
                        str(row["external_id"] or ""),
                    )
                ),
            }
        )

    row_3054 = key_rows["9000000000003054"]
    folder_3054 = Path(row_3054["last_known_path"]) if row_3054 else None
    sidecar_3054_before = sidecar_url(folder_3054) if folder_3054 else ""

    from services.identity_invariants import (
        INTERNAL_ID_USED_AS_EXTERNAL_ID,
        INTERNAL_ID_USED_AS_WORKSPACE_ID,
        INTERNAL_ID_USED_IN_PLATFORM_URL,
        scan_conflict_scheme_b,
        scan_id_architecture_source,
        scan_invalid_entities,
        scan_reconcile_identity_lifecycle,
    )

    inv = scan_invalid_entities(library_root=LIB, db_path=PROD_DB)
    leak_ws = [f for f in inv.findings if f.violation_code == INTERNAL_ID_USED_AS_WORKSPACE_ID]
    leak_ext = [f for f in inv.findings if f.violation_code == INTERNAL_ID_USED_AS_EXTERNAL_ID]
    leak_url = [f for f in inv.findings if f.violation_code == INTERNAL_ID_USED_IN_PLATFORM_URL]
    life = scan_reconcile_identity_lifecycle(ROOT)
    arch = scan_id_architecture_source(ROOT)
    scheme = scan_conflict_scheme_b(ROOT)

    created_detail = []
    for mid in created:
        d = live[mid]
        created_detail.append(
            {
                "internal_id": mid,
                "platform": d["platform"],
                "app_id": d["app_id"],
                "workspace_id": d["workspace_id"],
                "external_id": d["external_id"],
                "source_url": d["source_url"],
                "title": d["title"],
                "last_known_path": d["last_known_path"],
            }
        )

    anno = {
        "0360": key_rows["9000000000000360"],
        "0362": key_rows["9000000000000362"],
        "relationships": [],
    }
    conn = connect(PROD_DB)
    anno["relationships"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT source_mod_id, target_mod_id, relationship_type
            FROM mod_relationships
            WHERE CAST(source_mod_id AS TEXT) IN (?, ?)
               OR CAST(target_mod_id AS TEXT) IN (?, ?)
            """,
            ("9000000000000360", "9000000000000362", "9000000000000360", "9000000000000362"),
        )
    ]
    conn.close()

    report = {
        "timestamp": ts,
        "mods_count": count,
        "max_internal_id": max_id,
        "max_updated_at": max_updated,
        "platforms": platforms,
        "app_ids": {str(k): v for k, v in app_ids.items()},
        "conflict_status": conflict_dist,
        "duplicate_internal_id_groups": dup_internal,
        "workspace_duplicates": ws_dups,
        "empty_workspace_id": empty_ws,
        "empty_source_url": empty_url,
        "internal_id_steam_urls": steam_internal_urls,
        "phase_c_audit_count": audit_n,
        "managed_folders": len(folders),
        "empty_mod_folders": empty_mod_folders,
        "key_rows": key_rows,
        "steam_pk_10782": steam_10782 is not None,
        "workspace_10782_rows": ws_10782,
        "collisions": collisions,
        "scrub": scrub,
        "sidecar_3054_before": sidecar_3054_before,
        "invariants": {
            "internal_id_as_workspace": [
                {"entity_id": f.entity_id, "evidence": f.evidence} for f in leak_ws
            ],
            "internal_id_as_external": [
                {"entity_id": f.entity_id, "evidence": f.evidence} for f in leak_ext
            ],
            "internal_id_in_url": [
                {"entity_id": f.entity_id, "evidence": f.evidence} for f in leak_url
            ],
            "finding_counts": dict(Counter(f.violation_code for f in inv.findings)),
            "scanned_db_rows": inv.scanned_db_rows,
            "scanned_folders": inv.scanned_folders,
        },
        "static": {
            "lifecycle": len(life),
            "arch": len(arch),
            "scheme_b": len(scheme),
        },
        "backup_comparison": {
            "created": created_detail,
            "deleted": deleted,
            "identity_changes": ident_chg,
            "source_url_changes_approved": approved_url,
            "source_url_changes_other": other_url,
        },
        "anno": anno,
        "0798b9fd_present": any("0798b9fd" in e["name"] for e in empty_mod_folders),
    }
    out = ROOT / "docs" / "_p0_5_live_snapshot.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in report if k not in ("key_rows",)}, ensure_ascii=False, indent=2, default=str)[:12000])
    print("WROTE", out)
    print("KEY_ROWS")
    print(json.dumps(key_rows, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
