#!/usr/bin/env python3
"""Phase C idle snapshot + plan match. Read-only sqlite."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(r"D:\project\steam-mod-manager")
DB = ROOT / "data" / "mod_manager.db"
APPROVED = (
    "9000000000003054",
    "9000000000000354",
    "9000000000000360",
    "9000000000000361",
    "9000000000000362",
    "9000000000003031",
    "9000000000003225",
)


def embed(url: str, mid: str) -> bool:
    compact = (url or "").replace(" ", "")
    return (
        "steamcommunity.com/sharedfiles/filedetails" in (url or "").lower()
        and f"id={mid}" in compact
    )


def main() -> int:
    conn = sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    count = conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    mx = conn.execute("SELECT MAX(updated_at) AS m FROM mods").fetchone()["m"]
    steam = conn.execute("SELECT 1 FROM mods WHERE mod_id=10782").fetchone()
    print("mods_count", count)
    print("max_updated", mx)
    print("steam_10782", steam is not None)

    mismatch = []
    rows = {}
    for mid in ("9000000000003456", "9000000000003460", *APPROVED):
        row = conn.execute(
            """
            SELECT mod_id, platform, app_id, workspace_id, external_id, source_url,
                   last_known_path, updated_at, title
            FROM mods WHERE CAST(mod_id AS TEXT)=?
            """,
            (mid,),
        ).fetchone()
        d = dict(row) if row else None
        rows[mid] = d
        print("ROW", mid, d)
    conn.close()

    r3456 = rows["9000000000003456"]
    if not r3456 or str(r3456["workspace_id"]) != "10782" or str(r3456["platform"]) != "nexus":
        mismatch.append("10782 identity drift")
    if int(r3456["app_id"] or 0) != 292030:
        mismatch.append("10782 app_id drift")
    r3460 = rows["9000000000003460"]
    if not r3460 or str(r3460["workspace_id"]) != "10881" or str(r3460["platform"]) != "nexus":
        mismatch.append("3460 identity drift")
    if steam is not None:
        mismatch.append("steam PK 10782 present")

    for mid in APPROVED:
        d = rows[mid]
        if d is None:
            mismatch.append(f"{mid} missing")
            continue
        plat = str(d["platform"] or "").lower()
        url = str(d["source_url"] or "")
        if plat == "steam":
            mismatch.append(f"{mid} platform=steam")
        if not embed(url, mid):
            mismatch.append(f"{mid} URL no longer matches scrub rule: {url!r}")
        sidecar = Path(d["last_known_path"] or "") / ".info" / "metadata.json"
        if not sidecar.is_file():
            mismatch.append(f"{mid} sidecar missing")
            continue
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        surl = str(data.get("url") or data.get("source_url") or "")
        print("SIDECAR", mid, surl[:80], "ws", data.get("workspace_id"))
        if surl and not embed(surl, mid) and surl:
            # sidecar may already be clean while DB polluted, or vice versa
            if not embed(surl, mid):
                print("SIDECAR_NOTE", mid, "sidecar url not internal-steam", surl)
    print("MISMATCH", mismatch)
    Path(ROOT / "docs" / "_phase_c_live_snapshot.json").write_text(
        json.dumps(
            {"mods_count": count, "max_updated": mx, "rows": rows, "mismatch": mismatch},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    return 1 if mismatch else 0


if __name__ == "__main__":
    sys.exit(main())
