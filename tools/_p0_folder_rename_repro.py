"""Disposable forensic reproduction of folder-rename → duplicate Workspace ID.

Never opens or writes the production database / library.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_GITHUB, PLATFORM_NEXUS, PLATFORM_STEAM
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.library_reconcile import reconcile_library
from services.mod_identity import INTERNAL_ID_KEY, resolve_existing_mod_id
from services.mod_metadata_resolver import list_visible_mods

APP_W3 = 292030
WS = "10782"


def _write_folder(library: Path, game: str, name: str, payload: dict, *, pak: bool = True) -> Path:
    folder = library / game / name
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if pak:
        (folder / "content.pak").write_bytes(b"pak")
    return folder


def _dump_rows(db: DatabaseManager, label: str) -> list[dict]:
    with db._lock:
        rows = db._conn.execute(
            """
            SELECT mod_id, app_id, title, display_name, platform, external_id,
                   workspace_id, source_url, last_known_path, folder_present,
                   content_status, library_status, internal_id, updated_at
            FROM mods
            ORDER BY mod_id
            """
        ).fetchall()
    out = [{k: r[k] for k in r.keys()} for r in rows]
    print(f"\n=== {label} row_count={len(out)} ===")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return out


def _row_for_ws(rows: list[dict], workspace_id: str) -> list[dict]:
    return [r for r in rows if str(r.get("workspace_id") or "") == workspace_id]


def isolate(tmp: Path) -> tuple[DatabaseManager, Path, Path]:
    DatabaseManager.reset_instance()
    db_file = tmp / "repro.db"
    data = tmp / "data"
    data.mkdir()
    library = tmp / "mod"
    library.mkdir()
    import os

    os.environ["SMM_TEST_DB"] = str(db_file)
    db = DatabaseManager.instance(db_file)
    db.upsert_game(GameInfo(app_id=APP_W3, name="巫师三", folder_name="巫师三"))
    db.upsert_game(GameInfo(app_id=100, name="SomeGame", folder_name="SomeGame"))
    # Redirect backup/data helpers used by reconcile.
    import services.metadata_backup as mb
    import services.library_reconcile as lr
    import core.paths as paths

    mb.data_dir = lambda: data  # type: ignore[assignment]
    paths.data_dir = lambda: data  # type: ignore[assignment]
    lr.default_mod_library = lambda: library  # type: ignore[assignment]
    return db, library, data


def snapshot_mod(db: DatabaseManager, folder: Path) -> dict:
    side = json.loads((folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8"))
    with db._lock:
        rows = db._conn.execute("SELECT * FROM mods").fetchall()
    db_rows = [{k: r[k] for k in r.keys()} for r in rows]
    return {
        "folder": str(folder),
        "folder_name": folder.name,
        "sidecar": {
            k: side.get(k)
            for k in (
                "published_file_id",
                "workspace_id",
                "external_id",
                "internal_id",
                "source_type",
                "platform",
                "url",
                "title",
                "display_name",
                "app_id",
                "identity_status",
            )
        },
        "db_rows": db_rows,
    }


def run_case(name: str, setup_fn, rename_fn) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"smm_repro_{name}_"))
    try:
        db, library, _data = isolate(tmp)
        folder = setup_fn(db, library)
        before = snapshot_mod(db, folder)
        before_count = len(before["db_rows"])
        new_folder = rename_fn(folder)
        result = reconcile_library(library)
        after = snapshot_mod(db, new_folder if new_folder.is_dir() else folder)
        after_count = len(after["db_rows"])
        ws_rows = _row_for_ws(after["db_rows"], WS)
        visible = []
        try:
            visible = [
                {
                    "published_file_id": m.published_file_id,
                    "title": m.display_name or m.title,
                    "path": str(m.managed_path),
                }
                for m in list_visible_mods(library, None)
            ]
        except Exception as exc:  # noqa: BLE001
            visible = [{"error": str(exc)}]
        before_ids = {str(r["mod_id"]) for r in before["db_rows"]}
        after_ids = {str(r["mod_id"]) for r in after["db_rows"]}
        created = sorted(after_ids - before_ids)
        report = {
            "case": name,
            "tmp": str(tmp),
            "reconcile": {
                "scanned": result.scanned,
                "imported": result.imported,
                "renamed": result.renamed,
                "missing": result.missing,
                "conflicts": result.conflicts,
                "notes": result.notes,
            },
            "existing_mod_preserved": len(created) == 0 and after_count == before_count,
            "new_mod_created": bool(created),
            "created_internal_ids": created,
            "internal_id_changed": False,
            "workspace_id_changed": False,
            "external_id_changed": False,
            "db_row_count_delta": after_count - before_count,
            "workspace_10782_row_count": len(ws_rows),
            "visible_cards": visible,
            "before": before,
            "after": after,
        }
        if before["db_rows"] and after["db_rows"]:
            b0 = before["db_rows"][0]
            # Compare original PK if still present
            orig = str(b0["mod_id"])
            a_orig = next((r for r in after["db_rows"] if str(r["mod_id"]) == orig), None)
            if a_orig:
                report["internal_id_changed"] = str(a_orig["mod_id"]) != orig
                report["workspace_id_changed"] = str(a_orig.get("workspace_id") or "") != str(
                    b0.get("workspace_id") or ""
                )
                report["external_id_changed"] = str(a_orig.get("external_id") or "") != str(
                    b0.get("external_id") or ""
                )
            else:
                report["internal_id_changed"] = True
        print(f"\n######## CASE {name} ########")
        print(json.dumps({k: v for k, v in report.items() if k not in ("before", "after")}, ensure_ascii=False, indent=2, default=str))
        print("BEFORE_DB", json.dumps(before["db_rows"], ensure_ascii=False, default=str))
        print("AFTER_DB", json.dumps(after["db_rows"], ensure_ascii=False, default=str))
        print("BEFORE_SIDECAR", json.dumps(before["sidecar"], ensure_ascii=False))
        print("AFTER_SIDECAR", json.dumps(after["sidecar"], ensure_ascii=False))
        return report
    finally:
        DatabaseManager.reset_instance()
        shutil.rmtree(tmp, ignore_errors=True)


def setup_nexus_production_shape(db: DatabaseManager, library: Path) -> Path:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=WS,
        source_url=f"https://www.nexusmods.com/witcher3/mods/{WS}",
        title="Flashbacks - Something you've already seen",
        app_id=APP_W3,
        game_name="巫师三",
        operation="import",
    )
    folder = _write_folder(
        library,
        "巫师三",
        "Empty Mod 0798b9fd",
        {
            "published_file_id": created.mod_id,
            "workspace_id": WS,
            INTERNAL_ID_KEY: "eb8a3ddd-1886-40fb-85b6-c1b1be8159a3",
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks - Something you've already seen",
            "display_name": "Flashbacks - Something you've already seen",
            "app_id": 0,
            "game_name": "巫师三",
            "identity_status": "complete",
        },
    )
    db.update_mod_identity_fields(
        created.mod_id,
        internal_id="eb8a3ddd-1886-40fb-85b6-c1b1be8159a3",
        last_known_path=str(folder.resolve()),
        folder_present=True,
        platform=PLATFORM_NEXUS,
        source_url=f"https://www.nexusmods.com/witcher3/mods/{WS}",
        external_id=WS,
        workspace_id=WS,
        app_id=APP_W3,
        title="Flashbacks - Something you've already seen",
    )
    return folder


def setup_nexus_canonical_sidecar(db: DatabaseManager, library: Path) -> Path:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=WS,
        source_url=f"https://www.nexusmods.com/witcher3/mods/{WS}",
        title="Flashbacks",
        app_id=APP_W3,
        game_name="巫师三",
        operation="import",
    )
    folder = _write_folder(
        library,
        "巫师三",
        "Flashbacks",
        {
            "workspace_id": WS,
            "source_type": "nexus",
            "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
            "title": "Flashbacks",
            "display_name": "Flashbacks",
            "app_id": APP_W3,
            "game_name": "巫师三",
        },
    )
    db.update_mod_identity_fields(
        created.mod_id,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        platform=PLATFORM_NEXUS,
        workspace_id=WS,
        external_id=WS,
        source_url=f"https://www.nexusmods.com/witcher3/mods/{WS}",
        app_id=APP_W3,
    )
    return folder


def setup_nexus_pub_is_workspace(db: DatabaseManager, library: Path) -> Path:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=WS,
        source_url=f"https://www.nexusmods.com/witcher3/mods/{WS}",
        title="Flashbacks",
        app_id=APP_W3,
        game_name="巫师三",
        operation="import",
    )
    folder = _write_folder(
        library,
        "巫师三",
        "Flashbacks",
        {
            "published_file_id": WS,
            "title": "Flashbacks",
            "display_name": "Flashbacks",
            "game_name": "巫师三",
        },
    )
    db.update_mod_identity_fields(
        created.mod_id,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        platform=PLATFORM_NEXUS,
        workspace_id=WS,
        external_id=WS,
        app_id=APP_W3,
    )
    return folder


def setup_nexus_path_only(db: DatabaseManager, library: Path) -> Path:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=WS,
        source_url=f"https://www.nexusmods.com/witcher3/mods/{WS}",
        title="Flashbacks",
        app_id=APP_W3,
        game_name="巫师三",
        operation="import",
    )
    folder = _write_folder(
        library,
        "巫师三",
        "Flashbacks",
        {"title": "Flashbacks", "display_name": "Flashbacks", "game_name": "巫师三"},
    )
    db.update_mod_identity_fields(
        created.mod_id,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        platform=PLATFORM_NEXUS,
        workspace_id=WS,
        external_id=WS,
        source_url=f"https://www.nexusmods.com/witcher3/mods/{WS}",
        app_id=APP_W3,
    )
    return folder


def setup_steam(db: DatabaseManager, library: Path) -> Path:
    wid = "3571849225"
    db.upsert_mod(
        ModMetadata(
            published_file_id=wid,
            title="Errata Thermal Katana",
            app_id=APP_W3,
            url=f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}",
        )
    )
    folder = _write_folder(
        library,
        "巫师三",
        "Errata Thermal Katana",
        {
            "published_file_id": wid,
            "workspace_id": wid,
            "source_type": "steam",
            "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}",
            "title": "Errata Thermal Katana",
            "app_id": APP_W3,
            "game_name": "巫师三",
        },
    )
    db.update_mod_identity_fields(
        wid,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        platform=PLATFORM_STEAM,
        workspace_id=wid,
        external_id=wid,
        app_id=APP_W3,
    )
    return folder


def setup_github(db: DatabaseManager, library: Path) -> Path:
    created = create_mod_identity(
        db,
        platform=PLATFORM_GITHUB,
        external_id="owner/repo",
        source_url="https://github.com/owner/repo",
        title="GH Mod",
        app_id=100,
        game_name="SomeGame",
        operation="import",
    )
    folder = _write_folder(
        library,
        "SomeGame",
        "GH Mod",
        {
            "workspace_id": created.workspace_id,
            "source_type": "github",
            "url": "https://github.com/owner/repo",
            "external_id": "owner/repo",
            "title": "GH Mod",
            "app_id": 100,
            "game_name": "SomeGame",
        },
    )
    db.update_mod_identity_fields(
        created.mod_id,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        platform=PLATFORM_GITHUB,
        workspace_id=created.workspace_id,
        external_id="owner/repo",
        source_url="https://github.com/owner/repo",
        app_id=100,
    )
    return folder


def rename_only(folder: Path) -> Path:
    target = folder.parent / f"{folder.name} Renamed"
    folder.rename(target)
    return target


def rename_twice(folder: Path) -> Path:
    mid = folder.parent / f"{folder.name} Mid"
    folder.rename(mid)
    end = folder.parent / f"{folder.name} Final"
    mid.rename(end)
    return end


def leftover_empty_and_rename(db: DatabaseManager, library: Path) -> Path:
    folder = setup_nexus_production_shape(db, library)
    renamed = folder.parent / "Flashbacks - Something you've already seen"
    shutil.copytree(folder, renamed)
    # leftover original stays (copy, not rename) — production shape
    return renamed


def dump_production_readonly() -> None:
    print("\n======== PRODUCTION_READONLY ========")
    dbp = Path(r"D:\project\steam-mod-manager\data\mod_manager.db")
    conn = sqlite3.connect(dbp.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT mod_id, app_id, title, platform, external_id, workspace_id,
               source_url, last_known_path, folder_present, content_status,
               library_status, updated_at, internal_id
        FROM mods
        WHERE TRIM(COALESCE(workspace_id,'')) = '10782'
           OR CAST(mod_id AS TEXT) = '10782'
           OR TRIM(COALESCE(external_id,'')) = '10782'
        """
    ).fetchall()
    print("PROD_ROWS", json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2, default=str))
    try:
        audits = conn.execute(
            """
            SELECT * FROM identity_audit_log
            WHERE CAST(mod_id AS TEXT) IN ('10782','9000000000003456')
            ORDER BY id
            """
        ).fetchall()
        print("PROD_AUDIT_COUNT", len(audits))
        for a in audits[-20:]:
            print(dict(a))
    except Exception as exc:
        print("PROD_AUDIT_ERR", exc)
    conn.close()
    lib = Path(r"D:\project\steam-mod-manager\mod") / "巫师三"
    hits = []
    for folder in lib.iterdir() if lib.is_dir() else []:
        if not folder.is_dir():
            continue
        meta = folder / INFO_DIR_NAME / METADATA_FILENAME
        if not meta.is_file():
            continue
        data = json.loads(meta.read_text(encoding="utf-8"))
        if str(data.get("published_file_id") or "") in {"10782", "9000000000003456"} or str(
            data.get("workspace_id") or ""
        ) == "10782":
            files = [p.name for p in folder.iterdir() if p.name != ".info"][:12]
            hits.append(
                {
                    "folder": folder.name,
                    "abs": str(folder),
                    "published_file_id": data.get("published_file_id"),
                    "workspace_id": data.get("workspace_id"),
                    "internal_id": data.get("internal_id"),
                    "url": data.get("url"),
                    "source_type": data.get("source_type"),
                    "leaf_files": files,
                }
            )
    print("PROD_FOLDERS_10782", json.dumps(hits, ensure_ascii=False, indent=2))


def main() -> None:
    dump_production_readonly()
    reports = []
    reports.append(run_case("A_folder_name_only_production_shape", setup_nexus_production_shape, rename_only))
    reports.append(run_case("B_display_name_unchanged_canonical_sidecar", setup_nexus_canonical_sidecar, rename_only))
    reports.append(run_case("C_pub_equals_workspace_id_no_other_keys", setup_nexus_pub_is_workspace, rename_only))
    reports.append(run_case("D_path_only_sidecar_empty_identity", setup_nexus_path_only, rename_only))
    reports.append(run_case("E_parent_game_dir_same", setup_nexus_production_shape, rename_only))
    reports.append(run_case("F_rename_twice", setup_nexus_production_shape, rename_twice))
    reports.append(run_case("STEAM_rename", setup_steam, rename_only))
    reports.append(run_case("GITHUB_rename", setup_github, rename_only))

    tmp = Path(tempfile.mkdtemp(prefix="smm_repro_leftover_"))
    try:
        db, library, _ = isolate(tmp)
        leftover_empty_and_rename(db, library)
        result = reconcile_library(library)
        rows = _dump_rows(db, "LEFTOVER_EMPTY_PLUS_RENAMED")
        visible = list_visible_mods(library, "巫师三")
        print(
            "LEFTOVER_VISIBLE",
            json.dumps(
                [
                    {
                        "id": m.published_file_id,
                        "title": m.display_name or m.title,
                        "path": str(m.managed_path),
                    }
                    for m in visible
                ],
                ensure_ascii=False,
                indent=2,
            ),
        )
        print("LEFTOVER_RECONCILE", result.imported, result.conflicts, result.notes)
        reports.append(
            {
                "case": "LEFTOVER_EMPTY_PLUS_RENAMED",
                "db_row_count": len(rows),
                "ws_10782_count": len(_row_for_ws(rows, WS)),
                "visible_count": len(visible),
                "conflicts": result.conflicts,
            }
        )
    finally:
        DatabaseManager.reset_instance()
        shutil.rmtree(tmp, ignore_errors=True)

    # Creation-boundary: existing workspace 10782, filesystem discovery tries create again
    tmp = Path(tempfile.mkdtemp(prefix="smm_repro_dupcreate_"))
    try:
        db, library, _ = isolate(tmp)
        setup_nexus_canonical_sidecar(db, library)
        before = _dump_rows(db, "DUPCREATE_BEFORE")
        other = _write_folder(
            library,
            "巫师三",
            "Another Copy",
            {
                "workspace_id": WS,
                "source_type": "nexus",
                "url": f"https://www.nexusmods.com/witcher3/mods/{WS}",
                "external_id": WS,
                "title": "Copy",
                "app_id": APP_W3,
                "game_name": "巫师三",
            },
        )
        result = reconcile_library(library)
        after = _dump_rows(db, "DUPCREATE_AFTER")
        print("DUPCREATE", result.imported, result.conflicts, result.notes, "delta", len(after) - len(before), "other", other.name)
    finally:
        DatabaseManager.reset_instance()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n======== SUMMARY ========")
    for r in reports:
        if not isinstance(r, dict):
            continue
        print(
            r.get("case"),
            "preserved=",
            r.get("existing_mod_preserved"),
            "created=",
            r.get("new_mod_created"),
            "delta=",
            r.get("db_row_count_delta"),
            "ws_rows=",
            r.get("workspace_10782_row_count"),
            "imported=",
            (r.get("reconcile") or {}).get("imported"),
            "conflicts=",
            (r.get("reconcile") or {}).get("conflicts"),
        )


if __name__ == "__main__":
    main()
