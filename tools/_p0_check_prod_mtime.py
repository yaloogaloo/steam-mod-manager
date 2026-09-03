import sqlite3
from pathlib import Path

p = Path(r"D:\project\steam-mod-manager\data\mod_manager.db")
uri = p.resolve().as_uri() + "?mode=ro"
c = sqlite3.connect(uri, uri=True)
c.row_factory = sqlite3.Row
n = c.execute(
    "SELECT COUNT(*) AS n FROM mods WHERE updated_at >= ?",
    ("2026-09-02T18:50:00+00:00",),
).fetchone()["n"]
print("rows_updated_after_1850", n)
rows = c.execute(
    """
    SELECT mod_id, platform, workspace_id, folder_present, updated_at
    FROM mods
    WHERE updated_at >= ?
    ORDER BY updated_at DESC
    LIMIT 20
    """,
    ("2026-09-02T18:50:00+00:00",),
).fetchall()
for r in rows:
    print(dict(r))
print(
    "3054",
    dict(
        c.execute(
            "SELECT source_url, updated_at, folder_present FROM mods WHERE mod_id=9000000000003054"
        ).fetchone()
    ),
)
print(
    "3456",
    dict(
        c.execute(
            "SELECT source_url, updated_at, folder_present, workspace_id, platform FROM mods WHERE mod_id=9000000000003456"
        ).fetchone()
    ),
)
print("steam_10782", c.execute("SELECT 1 FROM mods WHERE mod_id=10782").fetchone())
c.close()
