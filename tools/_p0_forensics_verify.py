#!/usr/bin/env python3
"""Read-only post-apply verification. No mutation."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(r"D:\project\steam-mod-manager")
uri = (ROOT / "data" / "mod_manager.db").resolve().as_uri() + "?mode=ro"
conn = sqlite3.connect(uri, uri=True)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA query_only = ON")

out: dict = {}
ops: Counter = Counter()
ghosts = {str(i) for i in range(9000000000003438, 9000000000003451)}
ghost_ops = []
for r in conn.execute(
    "SELECT operation, action, ghost_mod_id, canonical_mod_id FROM identity_repair_audit"
):
    ops[(r["operation"], r["action"] or "")] += 1
    if str(r["ghost_mod_id"]) in ghosts:
        ghost_ops.append(
            {
                "operation": r["operation"],
                "action": r["action"],
                "ghost": r["ghost_mod_id"],
                "canonical": r["canonical_mod_id"],
            }
        )
out["ops"] = {f"{a}|{b}": n for (a, b), n in ops.items()}
out["audit_total"] = sum(ops.values())
out["ghost_audit_count"] = len(ghost_ops)
out["ghost_audit"] = ghost_ops

out["new_internal"] = [
    dict(r)
    for r in conn.execute(
        "SELECT mod_id, title, platform, external_id, workspace_id, last_known_path, updated_at "
        "FROM mods WHERE mod_id IN (9000000000003451, 9000000000003452)"
    )
]
out["source_internal"] = [
    dict(r)
    for r in conn.execute(
        "SELECT mod_id, title, source_url FROM mods "
        "WHERE source_url LIKE '%id=9000%' OR source_url LIKE '%900000000000%'"
    )
]
out["deploy_today"] = [
    dict(r)
    for r in conn.execute(
        "SELECT mod_id, title, deploy_time, deploy_path FROM mods "
        "WHERE deploy_time LIKE '2026-09-02%' ORDER BY deploy_time"
    )
]
out["canonical_check"] = [
    dict(r)
    for r in conn.execute(
        "SELECT mod_id, title, platform, external_id, workspace_id, source_url "
        "FROM mods WHERE mod_id IN (3591453758,3592539424,3781246892,3783660244,"
        "3784396849,3784602736,3785095584,3785271947,3786388428,3786411372,"
        "3787384780,3789395672,3790849356)"
    )
]
# dangling refs to ghosts
ghost_list = list(range(9000000000003438, 9000000000003451))
dangling = []
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
for gid in ghost_list:
    hits = {}
    for table in tables:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        for col in cols:
            try:
                n = conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE CAST("{col}" AS TEXT)=?',
                    (str(gid),),
                ).fetchone()[0]
            except sqlite3.Error:
                continue
            if n:
                hits[f"{table}.{col}"] = n
    dangling.append({"ghost": str(gid), "hits": hits})
out["ghost_remaining_refs"] = dangling
conn.close()

qroot = ROOT / "data" / "identity_repair_quarantine" / "2026-09-02T101049+0000"
out["quarantine_run"] = str(qroot)
out["quarantine_exists"] = qroot.is_dir()
if qroot.is_dir():
    out["quarantine_top"] = sorted(p.name for p in qroot.iterdir())

dest = ROOT / "data" / "p0_forensics_verify.json"
dest.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("WROTE", dest)
print("ops", out["ops"])
print("ghost_audit", out["ghost_audit_count"])
print("new_internal", out["new_internal"])
print("source_internal_n", len(out["source_internal"]))
print("deploy_today_n", len(out["deploy_today"]))
print("canonical_n", len(out["canonical_check"]))
print("q_top", out.get("quarantine_top"))
