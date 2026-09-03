import sqlite3
from pathlib import Path

p = Path(r"D:\project\steam-mod-manager\data\mod_manager.db")
c = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row
print(
    "audit_after_1850",
    c.execute(
        "SELECT COUNT(*) AS n FROM identity_audit_log WHERE created_at>=?",
        ("2026-09-02T18:50:00+00:00",),
    ).fetchone()["n"],
)
print(
    "repair_audit_after_1850",
    c.execute(
        "SELECT COUNT(*) AS n FROM identity_repair_audit WHERE created_at>=?",
        ("2026-09-02T18:50:00+00:00",),
    ).fetchone()["n"]
    if c.execute(
        "SELECT name FROM sqlite_master WHERE name='identity_repair_audit'"
    ).fetchone()
    else "no_table",
)
# sample backup fields on a recently updated row
row = c.execute(
    """
    SELECT mod_id, folder_present, backup_status, library_status, content_status,
           last_known_path, source_url, workspace_id, platform, updated_at
    FROM mods WHERE mod_id=9000000000003048
    """
).fetchone()
print("sample_3048", dict(row) if row else None)
c.close()
