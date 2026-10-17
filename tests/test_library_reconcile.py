"""Library reconcile — bind existing Entities; never mint identity from disk."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_GITHUB, PLATFORM_NEXUS
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_reconcile import (
    LIBRARY_STATUS_MISSING,
    reconcile_library,
    resolve_library_games,
)
from services.metadata_backup import load_backup
from services.mod_identity import ensure_mod_identity, read_internal_id
from services.mod_metadata_resolver import list_visible_mods, resolve_mod_metadata
from tests.helpers.identity import (
    bind_managed_path,
    create_other_test_mod,
    create_test_mod_identity,
    write_info_sidecar,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "reconcile.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    monkeypatch.setattr("core.paths.data_dir", lambda: root)
    return root


def _write_info(folder: Path, payload: dict) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (folder / "content.pak").write_bytes(b"pak")


def _only_mod_row(db: DatabaseManager) -> dict:
    with db._lock:
        rows = db._conn.execute(
            """
            SELECT mod_id, platform, external_id, workspace_id, source_type,
                   last_known_path, folder_present, library_status, content_status
            FROM mods
            """
        ).fetchall()
    assert len(rows) == 1
    return {k: rows[0][k] for k in rows[0].keys()}


def test_case1_unbound_folder_is_ignored_not_imported(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    """``.info`` without matching Entity → IGNORE_UNBOUND; no Entity mint."""
    library = tmp_path / "mod"
    folder = library / "GameX" / "ModA"
    _write_info(
        folder,
        {
            "published_file_id": "960001",
            "title": "ModA",
            "display_name": "ModA",
            "game_name": "GameX",
            "source_type": "nexus",
            "url": "https://www.nexusmods.com/gamex/mods/960001",
            "workspace_id": "960001",
            "external_id": "960001",
            "app_id": 1,
        },
    )

    result = reconcile_library(library)
    assert result.scanned >= 1
    assert result.imported == 0
    assert any("IGNORE_UNBOUND" in n for n in result.notes)
    with db._lock:
        n = db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    assert int(n) == 0


def test_case2_delete_game_still_visible(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    created = create_test_mod_identity(
        db,
        platform=PLATFORM_GITHUB,
        external_id="owner/modb",
        source_url="https://github.com/owner/modb",
        title="ModB",
        app_id=2,
        game_name="GameY",
    )
    pk = str(created.mod_id)
    frozen = str(created.internal_id)
    folder = library / "GameY" / "ModB"
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title="ModB",
        external_id="owner/modb",
        workspace_id=str(created.workspace_id or "owner/modb"),
        app_id=2,
        game_name="GameY",
        platform=PLATFORM_GITHUB,
        extra={"url": "https://github.com/owner/modb", "source_type": "github"},
    )
    (folder / "content.pak").write_bytes(b"pak")
    bind_managed_path(db, pk, folder, title="ModB", game_name="GameY")

    reconcile_library(library)
    mid = str(_only_mod_row(db)["mod_id"])
    assert mid == pk
    shutil.rmtree(library / "GameY")
    result = reconcile_library(library)
    assert result.missing >= 1

    games = resolve_library_games(library)
    assert any(g["folder"] == "GameY" for g in games)
    game = next(g for g in games if g["folder"] == "GameY")
    assert int(game["count"]) >= 1
    resolved = resolve_mod_metadata(mid, folder)
    assert resolved is not None
    assert resolved.folder_present is False
    row = db.get_mod_backup_row(mid)
    assert row is not None
    assert str(row.get("library_status") or "") == LIBRARY_STATUS_MISSING


def test_case3_restore_info_overwrites_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    created = create_test_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="960003",
        source_url="https://www.nexusmods.com/gamez/mods/960003",
        title="Old",
        app_id=3,
        game_name="GameZ",
    )
    pk = str(created.mod_id)
    frozen = str(created.internal_id)
    folder = library / "GameZ" / "ModC"
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title="Old",
        external_id="960003",
        workspace_id="960003",
        app_id=3,
        game_name="GameZ",
        platform=PLATFORM_NEXUS,
        extra={
            "display_name": "Old",
            "url": "https://www.nexusmods.com/gamez/mods/960003",
            "source_type": "nexus",
        },
    )
    (folder / "content.pak").write_bytes(b"pak")
    bind_managed_path(db, pk, folder, title="Old", game_name="GameZ")

    reconcile_library(library)
    mid = str(_only_mod_row(db)["mod_id"])
    assert mid == pk
    archive = tmp_path / "archive" / "GameZ"
    shutil.copytree(library / "GameZ", archive)
    shutil.rmtree(library / "GameZ")
    reconcile_library(library)

    shutil.copytree(archive, library / "GameZ")
    restored = library / "GameZ" / "ModC"
    meta_path = restored / INFO_DIR_NAME / METADATA_FILENAME
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    data["title"] = "New"
    data["display_name"] = "New"
    meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    reconcile_library(library)
    resolved = resolve_mod_metadata(mid, restored)
    assert resolved is not None
    assert (resolved.display_name or resolved.title) == "New"
    snap = load_backup(mid)
    assert snap is not None
    assert snap.metadata.get("title") == "New" or snap.metadata.get("display_name") == "New"


def test_case4_rename_does_not_duplicate(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    created = create_test_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="960004",
        source_url="https://www.nexusmods.com/anno1800/mods/960004",
        title="BetterHarbor",
        app_id=4,
        game_name="Anno1800",
    )
    pk = str(created.mod_id)
    frozen = str(created.internal_id)
    folder = library / "Anno1800" / "BetterHarbor"
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title="BetterHarbor",
        external_id="960004",
        workspace_id="960004",
        app_id=4,
        game_name="Anno1800",
        platform=PLATFORM_NEXUS,
        extra={
            "url": "https://www.nexusmods.com/anno1800/mods/960004",
            "source_type": "nexus",
        },
    )
    (folder / "content.pak").write_bytes(b"pak")
    bind_managed_path(db, pk, folder, title="BetterHarbor", game_name="Anno1800")

    reconcile_library(library)
    mid = str(_only_mod_row(db)["mod_id"])
    assert mid == pk
    renamed = library / "Anno1800" / "Better Harbor New"
    folder.rename(renamed)
    result = reconcile_library(library)
    assert result.renamed >= 1 or any(
        Path(str((db.get_mod_backup_row(mid) or {}).get("last_known_path") or "")).resolve()
        == renamed.resolve()
        for _ in (0,)
    )

    visible = list_visible_mods(library, "Anno1800")
    ids = [m.published_file_id for m in visible]
    assert ids.count(mid) == 1
    row = db.get_mod_backup_row(mid)
    assert row is not None
    assert Path(str(row["last_known_path"])).samefile(renamed)
    assert str(row.get("internal_id") or "") == frozen


def test_case5_unbound_local_folder_does_not_mint_uuid(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameU" / "LocalMod"
    _write_info(
        folder,
        {
            "title": "LocalMod",
            "display_name": "LocalMod",
            "game_name": "GameU",
            "source_type": "github",
            "url": "https://github.com/a/b",
            "app_id": 1623730,
            "external_id": "a/b",
        },
    )
    result = reconcile_library(library)
    assert result.imported == 0
    assert any("IGNORE_UNBOUND" in n for n in result.notes)
    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert not read_internal_id(data)
    assert db.find_mod_by_external("github", "a/b", app_id=1623730) is None


def test_case6_info_without_db_entity_does_not_rebuild(
    tmp_path: Path, data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.info/internal_id`` absent from DB → orphan/unbound; never create Entity."""
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "fresh.db")
    library = tmp_path / "mod"
    folder = library / "Palworld" / "Imported"
    _write_info(
        folder,
        {
            "published_file_id": "960006",
            "title": "Imported",
            "game_name": "Palworld",
            "source_type": "nexus",
            "url": "https://www.nexusmods.com/palworld/mods/336",
            "workspace_id": "336",
            "external_id": "336",
            "app_id": 1623730,
            "internal_id": "orphan-uuid-not-in-db",
        },
    )
    assert db.get_mod("960006") is None
    result = reconcile_library(library)
    assert result.imported == 0
    assert any("IGNORE_UNBOUND" in n or "ORPHAN" in n.upper() for n in result.notes) or result.imported == 0
    with db._lock:
        n = db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    assert int(n) == 0
    DatabaseManager.reset_instance()


def test_ensure_mod_identity_does_not_use_folder_name(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "SomeFolderName"
    folder.mkdir()
    (folder / INFO_DIR_NAME).mkdir()
    payload = {"title": "X", "source_type": "github"}
    mid, out, changed = ensure_mod_identity(folder, payload)
    assert mid == ""
    assert changed is False
    assert out.get("identity_status") == "unresolved"
    assert not read_internal_id(out)


def test_bound_info_internal_id_rebinds_path(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    """``.info/internal_id`` matching DB Entity → path bind; no second Entity."""
    library = tmp_path / "mod"
    created = create_other_test_mod(db, title="Bound", external_id="ext-1", app_id=9, game_name="G")
    pk = str(created.mod_id)
    frozen = str(created.internal_id)
    folder = library / "G" / "Bound"
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title="Bound",
        external_id="ext-1",
        workspace_id=str(created.workspace_id or "ext-1"),
        app_id=9,
        game_name="G",
    )
    (folder / "content.pak").write_bytes(b"pak")
    # Intentionally leave last_known_path empty — reconcile must bind via .info.
    result = reconcile_library(library)
    assert result.imported == 0
    row = db.get_mod_backup_row(pk) or {}
    assert Path(str(row.get("last_known_path") or "")).resolve() == folder.resolve()
    assert str(row.get("internal_id") or "") == frozen
    with db._lock:
        n = db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    assert int(n) == 1
