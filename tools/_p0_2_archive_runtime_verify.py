"""One-shot P0-2 live Archive runtime verification. Does not mint identity."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.paths import database_path  # noqa: E402
from services.archive import (  # noqa: E402
    OfflinePageArchiver,
    get_asset_cache_stats,
    is_valid_steam_workshop_page,
    reset_asset_cache_stats,
)

MOD_ID = "3786388428"


def _open_ro(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db_path = database_path()
    conn = _open_ro(db_path)
    try:
        count_before = int(conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0])
        ids_before = {
            str(r[0])
            for r in conn.execute("SELECT CAST(mod_id AS TEXT) FROM mods").fetchall()
        }
        row = conn.execute(
            """
            SELECT CAST(mod_id AS TEXT), title, last_known_path, platform
            FROM mods WHERE CAST(mod_id AS TEXT) = ?
            """,
            (MOD_ID,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        print(json.dumps({"ok": False, "error": "mod row missing", "mod_id": MOD_ID}))
        return 2

    folder = Path(str(row["last_known_path"] or "").strip())
    if not folder.is_dir():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "last_known_path missing or not a directory",
                    "mod_id": MOD_ID,
                    "last_known_path": str(folder),
                },
                ensure_ascii=False,
            )
        )
        return 2

    info_dir = folder / ".info"
    info_dir.mkdir(parents=True, exist_ok=True)
    index_path = info_dir / "index.html"

    reset_asset_cache_stats()
    records: list[str] = []
    log = logging.getLogger("services.archive")

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    list_handler = ListHandler()
    list_handler.setLevel(logging.INFO)
    log.addHandler(list_handler)

    t0 = time.perf_counter()
    with OfflinePageArchiver() as archiver:
        result = archiver.archive(MOD_ID, info_dir, overwrite=True)
    total_elapsed = time.perf_counter() - t0
    joined = "\n".join(records)

    def _search(pattern: str, text: str, default: str = "") -> str:
        m = re.search(pattern, text)
        return m.group(1) if m else default

    html_elapsed = _search(r"html=([0-9.]+)s", joined)
    asset_elapsed = _search(r"assets=([0-9.]+)s", joined)
    hit = _search(r"\[ARCHIVE ASSETS\] total=\S+ hit=(\d+)", joined)
    miss = _search(r"\[ARCHIVE ASSETS\] total=\S+ hit=\d+ miss=(\d+)", joined)
    fail = _search(r"\[ARCHIVE ASSETS\] total=\S+ hit=\d+ miss=\d+ fail=(\d+)", joined)
    top_ok = _search(r"top_ok=(\d+)", joined)
    top_fail = _search(r"top_fail=(\d+)", joined)
    nested_ok = _search(r"nested_ok=(\d+)", joined)
    nested_fail = _search(r"nested_fail=(\d+)", joined)

    conn = _open_ro(db_path)
    try:
        count_after = int(conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0])
        ids_after = {
            str(r[0])
            for r in conn.execute("SELECT CAST(mod_id AS TEXT) FROM mods").fetchall()
        }
    finally:
        conn.close()

    cache = get_asset_cache_stats()
    out = {
        "mod_id": MOD_ID,
        "title": row["title"],
        "info_dir": str(info_dir),
        "outcome": result.outcome,
        "error": result.error,
        "html_elapsed": html_elapsed,
        "asset_elapsed": asset_elapsed,
        "total_elapsed": round(total_elapsed, 3),
        "hit": hit or cache.get("hit"),
        "miss": miss or cache.get("miss"),
        "fail": fail or cache.get("fail"),
        "top_ok": top_ok,
        "top_fail": top_fail,
        "nested_ok": nested_ok,
        "nested_fail": nested_fail,
        "cache": cache,
        "output_exists": index_path.is_file(),
        "output_size": index_path.stat().st_size if index_path.is_file() else 0,
        "output_valid": is_valid_steam_workshop_page(index_path),
        "mod_count_before": count_before,
        "mod_count_after": count_after,
        "new_mod_ids": sorted(ids_after - ids_before),
        "identity_unchanged": count_before == count_after and ids_before == ids_after,
        "previous_asset_elapsed_s": 419.01,
    }
    if asset_elapsed:
        try:
            new_s = float(asset_elapsed)
            out["asset_improvement_s"] = round(419.01 - new_s, 2)
            out["asset_speedup_x"] = round(419.01 / new_s, 2) if new_s > 0 else None
        except ValueError:
            pass
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if result.outcome == "success" and out["output_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
