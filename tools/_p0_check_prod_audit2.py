import sqlite3
from pathlib import Path

p = Path(r"D:\project\steam-mod-manager\data\mod_manager.db")
c = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = c.execute(
    """
    SELECT created_at, mod_id, field_name, old_value, new_value, source, reason
    FROM identity_audit_log
    WHERE created_at>=?
    ORDER BY id DESC
    """,
    ("2026-09-02T18:50:00+00:00",),
).fetchall()
for r in rows:
    print(dict(r))
c.close()
