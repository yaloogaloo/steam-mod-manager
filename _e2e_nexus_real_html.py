"""Real HTML E2E validation — parse → merge → SQLite on the live project DB."""
from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "data/mod_manager.db"
MOD_ID = "9000000000003064"
EXPECTED_URL = "https://www.nexusmods.com/stardewvalley/mods/10062"
EXPECTED_ID = "10062"
EXPECTED_TITLE = "Hugs and Kisses"


def _find_mod_and_html() -> tuple[Path, Path]:
    for html in ROOT.joinpath("mod").rglob("Empty Mod f20722b2/.info/offline/index.html"):
        if html.is_file():
            return html.parent.parent.parent, html
    raise FileNotFoundError("real user offline HTML not found")


def _read_db() -> dict:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT title, display_name, source_url, external_id FROM mods WHERE mod_id=?",
        (MOD_ID,),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def main() -> int:
    from core.db_manager import DatabaseManager
    from services.offline.nexus_html_parser import (
        apply_nexus_offline_candidates,
        parse_nexus_offline_html,
    )

    mod, html = _find_mod_and_html()
    print("=== Nexus Offline Real HTML E2E ===\n")
    print("HTML URL:")
    print(EXPECTED_URL)

    c = parse_nexus_offline_html(html)
    print("\nParser URL:")
    print(c.source_url)
    print("\nParser external_id:")
    print(c.external_id)
    print("\nParser title:")
    print(c.title)

    db = DatabaseManager.instance(DB)
    apply_nexus_offline_candidates(MOD_ID, mod, c, db=db)

    row = _read_db()
    print("\nDB URL:")
    print(row.get("source_url"))
    print("\nDB external_id:")
    print(row.get("external_id"))
    print("\nDB title:")
    print(row.get("title"))

    ok = (
        c.source_url == EXPECTED_URL
        and c.external_id == EXPECTED_ID
        and c.title == EXPECTED_TITLE
        and row.get("source_url") == EXPECTED_URL
        and row.get("external_id") == EXPECTED_ID
        and row.get("title") == EXPECTED_TITLE
    )
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
