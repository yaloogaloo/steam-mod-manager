"""Read-only production ID semantic audit. Never mutates."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.paths import database_path
from services.identity_invariants import (
    audit_production_id_semantics,
    scan_id_architecture_source,
)
from services.identity_repair import open_readonly_sqlite

GHOSTS = [
    "9000000000003438",
    "9000000000003439",
    "9000000000003440",
    "9000000000003441",
    "9000000000003442",
    "9000000000003443",
    "9000000000003444",
    "9000000000003445",
    "9000000000003446",
    "9000000000003447",
    "9000000000003448",
    "9000000000003449",
    "9000000000003450",
    "9000000000000349",
    "9000000000000351",
    "9000000000003226",
    "9000000000003227",
    "9000000000003228",
    "9000000000003229",
    "9000000000003230",
    "9000000000003232",
    "9000000000003251",
    "9000000000003451",
    "9000000000003452",
]


def main() -> int:
    dbp = database_path()
    print("DB", dbp, "exists", dbp.is_file())
    if not dbp.is_file():
        print("NO_PRODUCTION_DB")
        return 0
    db = open_readonly_sqlite(dbp)
    try:
        audit = audit_production_id_semantics(db)
        print("SCANNED", audit.get("scanned"))
        print("COUNTS", audit.get("counts"))
        non = audit.get("non_valid") or []
        print("NON_VALID", len(non))
        by_plat: dict[str, int] = {}
        for row in non:
            plat = str(row.get("platform") or "?")
            by_plat[plat] = by_plat.get(plat, 0) + 1
        print("NON_VALID_BY_PLATFORM", by_plat)
        for row in non[:30]:
            print(row)
        print("--- GHOST CHECK ---")
        with db._lock:  # noqa: SLF001
            for gid in GHOSTS:
                row = db._conn.execute(  # noqa: SLF001
                    "SELECT mod_id, platform, external_id, workspace_id, title, "
                    "display_name, source_url, last_known_path FROM mods "
                    "WHERE CAST(mod_id AS TEXT)=?",
                    (gid,),
                ).fetchone()
                if row is None:
                    print(gid, "ABSENT")
                else:
                    print(gid, "PRESENT", {k: row[k] for k in row.keys()})
        print("--- SAME NUMERIC ---")
        with db._lock:  # noqa: SLF001
            n = db._conn.execute(  # noqa: SLF001
                """
                SELECT COUNT(*) AS c FROM mods
                WHERE platform='steam'
                  AND TRIM(COALESCE(workspace_id,'')) != ''
                  AND TRIM(workspace_id)=CAST(mod_id AS TEXT)
                  AND TRIM(workspace_id)=TRIM(COALESCE(external_id,''))
                """
            ).fetchone()["c"]
            print("steam_same_three", n)
            bad = db._conn.execute(  # noqa: SLF001
                """
                SELECT COUNT(*) AS c FROM mods
                WHERE TRIM(COALESCE(workspace_id,'')) = CAST(mod_id AS TEXT)
                  AND CAST(mod_id AS INTEGER) >= 9000000000000000
                """
            ).fetchone()["c"]
            print("workspace_eq_internal_nonsteam", bad)
    finally:
        db._conn.close()  # noqa: SLF001
    print("--- SOURCE SCAN ---")
    src = scan_id_architecture_source()
    print("source_findings", len(src))
    for f in src:
        print(f.violation_code, f.entity_id, f.evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
