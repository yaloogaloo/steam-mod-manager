#!/usr/bin/env python3
"""Follow-up read-only queries. No production mutation."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(r"D:\project\steam-mod-manager")
sys.path.insert(0, str(ROOT))
from core.paths import database_path  # noqa: E402

db_path = database_path()
uri = db_path.resolve().as_uri() + "?mode=ro"
conn = sqlite3.connect(uri, uri=True)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA query_only = ON")

out: dict = {}

# Integer-range ghost query (avoid text cast issues)
out["ghost_range_count"] = conn.execute(
    "SELECT COUNT(*) FROM mods WHERE mod_id BETWEEN ? AND ?",
    (9000000000003438, 9000000000003450),
).fetchone()[0]
rows = conn.execute(
    "SELECT mod_id, title, last_known_path FROM mods WHERE mod_id BETWEEN ? AND ?",
    (9000000000003438, 9000000000003450),
).fetchall()
out["ghost_range_rows"] = [{k: r[k] for k in r.keys()} for r in rows]

out["unknown_title_count"] = conn.execute(
    "SELECT COUNT(*) FROM mods WHERE title LIKE 'Unknown%'"
).fetchone()[0]
unk = conn.execute(
    "SELECT mod_id, title, last_known_path, platform, external_id, workspace_id "
    "FROM mods WHERE title LIKE 'Unknown%' LIMIT 30"
).fetchall()
out["unknown_title_sample"] = [{k: r[k] for k in r.keys()} for r in unk]

dupes = [
    9000000000000349,
    9000000000000351,
    9000000000003226,
    9000000000003227,
    9000000000003228,
    9000000000003229,
    9000000000003230,
    9000000000003232,
    9000000000003251,
]
out["dupe_int_present"] = []
for mid in dupes:
    n = conn.execute("SELECT COUNT(*) FROM mods WHERE mod_id = ?", (mid,)).fetchone()[0]
    out["dupe_int_present"].append({"mod_id": mid, "count": n})

# workspace_id search
out["ws_17863521013284165"] = conn.execute(
    "SELECT COUNT(*) FROM mods WHERE workspace_id = ?",
    ("17863521013284165",),
).fetchone()[0]
out["ws_like"] = conn.execute(
    "SELECT mod_id, title, workspace_id FROM mods WHERE workspace_id LIKE '%17863521013284165%'"
).fetchall()
out["ws_like"] = [{k: r[k] for k in r.keys()} for r in out["ws_like"]]

# tables
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
out["tables"] = tables

for table in tables:
    if "audit" in table.lower() or "repair" in table.lower() or "quarantine" in table.lower():
        n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        sample = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid DESC LIMIT 8').fetchall()
        out[f"table_{table}"] = {
            "count": n,
            "columns": cols,
            "sample": [{k: r[k] for k in r.keys()} for r in sample],
        }

# max internal id
out["max_internal"] = conn.execute(
    "SELECT MAX(mod_id) FROM mods WHERE mod_id >= 9000000000000000"
).fetchone()[0]
out["internal_count"] = conn.execute(
    "SELECT COUNT(*) FROM mods WHERE mod_id >= 9000000000000000"
).fetchone()[0]
out["steam_url_internal"] = conn.execute(
    "SELECT COUNT(*) FROM mods WHERE source_url LIKE '%id=9000%'"
).fetchone()[0]

conn.close()

# filesystem: quarantine, backup db, remaining unknown folders
lib = ROOT / "mod"
qdirs = []
for p in [ROOT / "data", lib]:
    if not p.exists():
        continue
    for child in p.iterdir():
        name = child.name.lower()
        if child.is_dir() and ("quarantine" in name or "identity_repair" in name):
            qdirs.append(str(child))
out["quarantine_dirs"] = qdirs

backup_root = ROOT / "data" / "identity_repair_production_backup"
out["backup_root_exists"] = backup_root.is_dir()
if backup_root.is_dir():
    out["backup_children"] = [p.name for p in backup_root.iterdir()]
    stamp = backup_root / "20260902T094631Z"
    if stamp.is_dir():
        out["backup_stamp_children"] = [p.name for p in stamp.iterdir()][:30]
        for dbcand in stamp.rglob("*.db"):
            out.setdefault("backup_dbs", []).append(str(dbcand))

# hosts / remaining unknown
duck = lib / "逃离鸭科夫"
out["remaining_unknown_folders"] = [
    c.name for c in duck.iterdir() if c.is_dir() and "Unknown" in c.name
] if duck.is_dir() else []

dest = ROOT / "data" / "p0_forensics_followup.json"
dest.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("WROTE", dest)
print("ghost_range_count", out["ghost_range_count"])
print("unknown_title_count", out["unknown_title_count"])
print("dupe_int_present", out["dupe_int_present"])
print("max_internal", out["max_internal"])
print("internal_count", out["internal_count"])
print("ws_count", out["ws_17863521013284165"])
