"""Read-only production dump for workspace_id 10782. Never writes."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DB = Path(r"D:\project\steam-mod-manager\data\mod_manager.db")
ROOT = Path(r"D:\project\steam-mod-manager")


def main() -> None:
    print("db_exists", DB.exists(), "size", DB.stat().st_size if DB.exists() else 0)
    uri = DB.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute("PRAGMA table_info(mods)").fetchall()]
    print("cols", cols)

    wanted = [
        c
        for c in (
            "mod_id",
            "app_id",
            "title",
            "display_name",
            "platform",
            "external_id",
            "workspace_id",
            "source_url",
            "last_known_path",
            "folder_present",
            "source_type",
            "content_status",
            "library_status",
            "updated_at",
            "internal_id",
            "created_at",
        )
        if c in cols
    ]
    select = ", ".join(wanted)
    sql = f"""
    SELECT {select}
    FROM mods
    WHERE TRIM(COALESCE(workspace_id,'')) = '10782'
       OR CAST(mod_id AS TEXT) = '10782'
       OR TRIM(COALESCE(external_id,'')) = '10782'
    """
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    print("EXACT_MATCH_COUNT", len(rows))
    print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))

    like = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT {select}
            FROM mods
            WHERE CAST(mod_id AS TEXT) LIKE '%10782%'
               OR TRIM(COALESCE(workspace_id,'')) LIKE '%10782%'
               OR TRIM(COALESCE(external_id,'')) LIKE '%10782%'
               OR COALESCE(last_known_path,'') LIKE '%10782%'
               OR COALESCE(title,'') LIKE '%10782%'
               OR COALESCE(display_name,'') LIKE '%10782%'
            """
        ).fetchall()
    ]
    print("LIKE_COUNT", len(like))
    print(json.dumps(like, ensure_ascii=False, indent=2, default=str))

    ws_dups = [
        dict(r)
        for r in conn.execute(
            """
            SELECT workspace_id, COUNT(*) AS cnt,
                   GROUP_CONCAT(mod_id) AS internal_ids,
                   GROUP_CONCAT(platform) AS platforms,
                   GROUP_CONCAT(external_id) AS external_ids
            FROM mods
            WHERE TRIM(COALESCE(workspace_id,'')) != ''
            GROUP BY workspace_id
            HAVING COUNT(*) > 1
            ORDER BY cnt DESC
            LIMIT 50
            """
        ).fetchall()
    ]
    print("DUPLICATE_WORKSPACE_GROUPS", len(ws_dups))
    print(json.dumps(ws_dups, ensure_ascii=False, indent=2, default=str))

    witcher = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT {select}
            FROM mods
            WHERE COALESCE(last_known_path,'') LIKE '%巫师3%'
               OR COALESCE(title,'') LIKE '%巫师%'
               OR COALESCE(display_name,'') LIKE '%巫师%'
            LIMIT 50
            """
        ).fetchall()
    ]
    print("WITCHER_ROWS", len(witcher))
    print(json.dumps(witcher, ensure_ascii=False, indent=2, default=str))

    indexes = [dict(r) for r in conn.execute("PRAGMA index_list(mods)").fetchall()]
    print("INDEXES", json.dumps(indexes, ensure_ascii=False, indent=2))

    conn.close()

    lib = ROOT / "mod"
    print("library_exists", lib.exists())
    if lib.exists():
        games = [p.name for p in lib.iterdir() if p.is_dir()]
        print("games", games)
        for name in games:
            if "巫师" in name or "witcher" in name.lower() or "w3" in name.lower():
                mods = [p.name for p in (lib / name).iterdir() if p.is_dir()]
                print("witcher_game", name, "mod_count", len(mods))


if __name__ == "__main__":
    main()
