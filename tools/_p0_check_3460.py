import sqlite3
from pathlib import Path

p = Path(r"D:\project\steam-mod-manager\data\mod_manager.db")
c = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row
row = c.execute(
    """
    SELECT mod_id, app_id, title, platform, external_id, workspace_id, source_url,
           last_known_path, folder_present, updated_at, source_type
    FROM mods WHERE mod_id=9000000000003460
    """
).fetchone()
print("row3460", dict(row) if row else None)
print("mods_count", c.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"])
c.close()
