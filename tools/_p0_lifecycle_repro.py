"""Disposable Incident A/B reproduction. Uses tmp dirs only. Never touches production."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.db_manager import DatabaseManager, get_db
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS, PLATFORM_STEAM
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_reconcile import reconcile_library
from services.identity_repair import open_readonly_sqlite
from core.paths import database_path as prod_database_path


def _write_info(folder: Path, payload: dict) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "content.pak").write_bytes(b"pak")


def _rows_10782(db: DatabaseManager) -> list[dict]:
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            """
            SELECT mod_id, platform, app_id, workspace_id, external_id,
                   source_url, last_known_path, title, display_name, updated_at
            FROM mods
            WHERE TRIM(workspace_id)='10782' OR CAST(mod_id AS TEXT)='10782'
            """
        ).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def _setup(tmp: Path, monkey: dict) -> tuple[DatabaseManager, Path]:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp / "repro.db")
    data = tmp / "data"
    data.mkdir()
    lib = tmp / "mod"
    db.upsert_game(GameInfo(app_id=292030, name="巫师3", folder_name="巫师3"))
    import core.db_manager as dbm
    import core.paths as paths
    import services.metadata_backup as mb

    monkey["get_db"] = dbm.get_db
    monkey["default_mod_library"] = paths.default_mod_library
    monkey["data_dir"] = paths.data_dir
    monkey["mb_data_dir"] = mb.data_dir
    dbm.get_db = lambda: db
    paths.default_mod_library = lambda: lib
    paths.data_dir = lambda: data
    mb.data_dir = lambda: data
    return db, lib


def _restore(monkey: dict) -> None:
    import core.db_manager as dbm
    import core.paths as paths
    import services.metadata_backup as mb

    if "get_db" in monkey:
        dbm.get_db = monkey["get_db"]
    if "default_mod_library" in monkey:
        paths.default_mod_library = monkey["default_mod_library"]
    if "data_dir" in monkey:
        paths.data_dir = monkey["data_dir"]
    if "mb_data_dir" in monkey:
        mb.data_dir = monkey["mb_data_dir"]
    DatabaseManager.reset_instance()


def scenario(name: str, sidecar: dict, *, include_uuid: bool, include_ws: bool, include_pub: str | None) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="p0_lifecycle_"))
    monkey: dict = {}
    db, lib = _setup(tmp, monkey)
    try:
        folder = lib / "巫师3" / "OldName"
        payload = dict(sidecar)
        reg = db.register_external_mod(
            platform=PLATFORM_NEXUS,
            external_id="10782",
            source_url="https://www.nexusmods.com/witcher3/mods/10782",
            title="OldName",
            app_id=292030,
            game_name="巫师3",
        )
        mid = str(reg.mod_id)
        uuid = ""
        if include_uuid:
            import uuid as uuidlib

            uuid = str(uuidlib.uuid4())
            db.update_mod_identity_fields(mid, internal_id=uuid)
            payload["internal_id"] = uuid
        if include_ws:
            payload["workspace_id"] = "10782"
        if include_pub is not None:
            payload["published_file_id"] = include_pub.format(mid=mid)
        payload.setdefault("source_type", "nexus")
        payload.setdefault("platform", "nexus")
        payload.setdefault("url", "https://www.nexusmods.com/witcher3/mods/10782")
        payload.setdefault("title", "OldName")
        payload.setdefault("app_id", 292030)
        payload.setdefault("game_name", "巫师3")
        _write_info(folder, payload)
        db.update_mod_identity_fields(
            mid,
            last_known_path=str(folder.resolve()),
            folder_present=True,
            workspace_id="10782",
            platform=PLATFORM_NEXUS,
            source_url="https://www.nexusmods.com/witcher3/mods/10782",
            external_id="10782",
            app_id=292030,
        )
        before = {
            "count": len(_rows_10782(db)),
            "rows": _rows_10782(db),
            "internal_id": mid,
        }
        new = folder.parent / "RenamedName"
        folder.rename(new)
        result = reconcile_library(lib)
        after_rows = _rows_10782(db)
        after_ids = [str(r["mod_id"]) for r in after_rows]
        return {
            "name": name,
            "before_internal": mid,
            "before_count": before["count"],
            "after_count": len(after_rows),
            "after_ids": after_ids,
            "delta": len(after_rows) - before["count"],
            "original_still_present": mid in after_ids,
            "original_path": str((db.get_mod_backup_row(mid) or {}).get("last_known_path") or ""),
            "imported": result.imported,
            "renamed": result.renamed,
            "notes": result.notes[:8],
            "after_rows": after_rows,
        }
    finally:
        _restore(monkey)


def incident_b() -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="p0_lifecycle_b_"))
    monkey: dict = {}
    db, lib = _setup(tmp, monkey)
    try:
        folder = lib / "巫师3" / "BetterUI"
        import uuid as uuidlib

        uid = str(uuidlib.uuid4())
        polluted = "https://steamcommunity.com/sharedfiles/filedetails/?id=PLACEHOLDER"
        reg = db.register_external_mod(
            platform=PLATFORM_NEXUS,
            external_id="title-not-id",
            source_url="",
            title="BetterUI",
            app_id=292030,
            game_name="巫师3",
        )
        mid = str(reg.mod_id)
        polluted = f"https://steamcommunity.com/sharedfiles/filedetails/?id={mid}"
        db.update_mod_identity_fields(
            mid,
            last_known_path=str(folder.resolve()),
            folder_present=True,
            platform=PLATFORM_NEXUS,
            source_url="",
            workspace_id="17870313801551871",
            internal_id=uid,
        )
        _write_info(
            folder,
            {
                "published_file_id": mid,
                "internal_id": uid,
                "workspace_id": "17870313801551871",
                "source_type": "nexus",
                "platform": "nexus",
                "url": polluted,
                "title": "BetterUI",
                "app_id": 292030,
                "game_name": "巫师3",
            },
        )
        before_url = (db.get_mod_backup_row(mid) or {}).get("source_url")
        reconcile_library(lib)
        after_url = (db.get_mod_backup_row(mid) or {}).get("source_url")
        return {
            "internal_id": mid,
            "before_url": before_url,
            "after_url": after_url,
            "sidecar_url": json.loads(
                (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
            ).get("url"),
            "rehydrated": bool(after_url and f"id={mid}" in str(after_url).replace(" ", "")),
        }
    finally:
        _restore(monkey)


def production_readonly() -> dict:
    dbp = prod_database_path()
    if not dbp.is_file():
        return {"error": "no production db"}
    db = open_readonly_sqlite(dbp)
    conn = db._conn  # noqa: SLF001
    try:
        rows_10782 = [
            dict(r)
            for r in conn.execute(
                """
                SELECT mod_id, platform, app_id, workspace_id, external_id,
                       source_url, last_known_path, title, display_name,
                       updated_at, folder_present
                FROM mods
                WHERE TRIM(workspace_id)='10782' OR CAST(mod_id AS TEXT)='10782'
                """
            )
        ]
        dups = [
            dict(r)
            for r in conn.execute(
                """
                SELECT workspace_id, COUNT(*) AS c,
                       GROUP_CONCAT(CAST(mod_id AS TEXT), ',') AS ids
                FROM mods
                WHERE TRIM(COALESCE(workspace_id,'')) != ''
                GROUP BY workspace_id
                HAVING COUNT(*) > 1
                ORDER BY c DESC
                """
            )
        ]
        row_3054 = conn.execute(
            """
            SELECT mod_id, platform, app_id, workspace_id, external_id,
                   source_url, last_known_path, title, display_name, updated_at
            FROM mods WHERE CAST(mod_id AS TEXT)='9000000000003054'
            """
        ).fetchone()
        return {
            "workspace_10782_rows": rows_10782,
            "duplicate_workspace_groups": dups,
            "row_3054": dict(row_3054) if row_3054 else None,
        }
    finally:
        conn.close()


def main() -> int:
    cases = [
        scenario(
            "ws+uuid+pub_empty",
            {},
            include_uuid=True,
            include_ws=True,
            include_pub="",
        ),
        scenario(
            "ws_only_no_uuid_pub_empty",
            {},
            include_uuid=False,
            include_ws=True,
            include_pub="",
        ),
        scenario(
            "pub_is_nexus_id_no_ws",
            {},
            include_uuid=False,
            include_ws=False,
            include_pub="10782",
        ),
        scenario(
            "pub_is_nexus_id_with_ws",
            {},
            include_uuid=False,
            include_ws=True,
            include_pub="10782",
        ),
        scenario(
            "pub_is_internal_pk_with_ws",
            {},
            include_uuid=True,
            include_ws=True,
            include_pub="{mid}",
        ),
        scenario(
            "url_only_no_ws_no_pub_with_app_id",
            {},
            include_uuid=False,
            include_ws=False,
            include_pub="",
        ),
    ]
    print("=== INCIDENT A REPRO ===")
    for c in cases:
        print(
            json.dumps(
                {k: c[k] for k in c if k != "after_rows"},
                ensure_ascii=False,
            )
        )
        if c["delta"] != 0:
            print("  AFTER_ROWS", c["after_rows"])
    print("=== INCIDENT B REPRO ===")
    print(json.dumps(incident_b(), ensure_ascii=False))
    print("=== PRODUCTION READONLY ===")
    prod = production_readonly()
    print("10782 rows", json.dumps(prod.get("workspace_10782_rows"), ensure_ascii=False, default=str))
    print("dup groups", len(prod.get("duplicate_workspace_groups") or []))
    for g in (prod.get("duplicate_workspace_groups") or [])[:20]:
        print(" ", dict(g))
    print("3054", json.dumps(prod.get("row_3054"), ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
