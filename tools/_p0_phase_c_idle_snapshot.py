"""Read-only Phase C idle + plan-match check. No writes."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(r"D:\project\steam-mod-manager")
DB = ROOT / "data" / "mod_manager.db"
LIB = ROOT / "mod"
IDS = (
    "9000000000003054",
    "9000000000000354",
    "9000000000000360",
    "9000000000000361",
    "9000000000000362",
    "9000000000003031",
    "9000000000003225",
)


def main() -> None:
    conn = sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    print("mods_count", conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0])
    print("max_updated", conn.execute("SELECT MAX(updated_at) FROM mods").fetchone()[0])
    print("steam_10782", conn.execute("SELECT 1 FROM mods WHERE mod_id=10782").fetchone())
    for mid in ("9000000000003456", "9000000000003460", *IDS):
        row = conn.execute(
            """
            SELECT mod_id, platform, app_id, workspace_id, external_id, source_url,
                   last_known_path, updated_at
            FROM mods WHERE CAST(mod_id AS TEXT)=?
            """,
            (mid,),
        ).fetchone()
        print("ROW", mid, dict(row) if row else None)
        if row and row["last_known_path"]:
            folder = Path(row["last_known_path"])
            meta = folder / ".info" / "metadata.json"
            if meta.is_file():
                data = json.loads(meta.read_text(encoding="utf-8"))
                print(
                    "SIDECAR",
                    mid,
                    {
                        "url": data.get("url") or data.get("source_url") or "",
                        "workspace_id": data.get("workspace_id"),
                        "platform": data.get("platform") or data.get("source_type"),
                        "path": str(meta),
                    },
                )
            else:
                print("SIDECAR_MISSING", mid, str(meta))
    conn.close()


if __name__ == "__main__":
    main()
