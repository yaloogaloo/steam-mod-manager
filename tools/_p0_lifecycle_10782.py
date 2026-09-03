"""Read-only: all 10782-related production rows."""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.paths import database_path
from services.identity_repair import open_readonly_sqlite

conn = open_readonly_sqlite(database_path())._conn
print("url/ws/ext 10782")
for r in conn.execute(
    """
    SELECT mod_id, platform, app_id, workspace_id, external_id, source_url,
           last_known_path, title, updated_at, folder_present
    FROM mods
    WHERE TRIM(workspace_id)='10782'
       OR TRIM(external_id)='10782'
       OR CAST(mod_id AS TEXT)='10782'
       OR source_url LIKE '%/mods/10782%'
       OR source_url LIKE '%id=10782%'
    """
):
    print(dict(r))
print("audit around 17:49")
for r in conn.execute(
    """
    SELECT created_at, mod_id, field_name, old_value, new_value, source, reason
    FROM identity_audit_log
    WHERE created_at >= '2026-09-02T17:40:00'
    ORDER BY id
    LIMIT 40
    """
):
    print(dict(r))
conn.close()
