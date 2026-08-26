"""Import duplicate detection — workshop id / source_url before write."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_NEXUS, PLATFORM_STEAM
from services.importers.duplicate_check import (
    DUPLICATE_STATUS,
    check_import_duplicate,
)
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from services.importers.steam import SteamImporter
from ui.import_thread import ImportWorker

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "dup.db")
    manager.update_game_deploy_config(1623730, name="Palworld")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _folder(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    (folder / "mod.pak").write_bytes(b"x")
    return folder


def test_workshop_id_duplicate_skips_pipeline(tmp_path: Path, db: DatabaseManager) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    first = SteamImporter(db=db).import_mod(
        workshop_id="123",
        title="One",
        library_root=lib,
        context=PALWORLD,
    )
    assert first.success

    materialize = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "services.importers.steam.materialize_imported_mod",
            materialize,
        )
        again = SteamImporter(db=db).import_mod(
            workshop_id="123",
            title="Again",
            source_folder=_folder(tmp_path, "again"),
            library_root=lib,
            context=PALWORLD,
        )

    assert again.is_duplicate
    assert again.status == DUPLICATE_STATUS
    assert again.success is False
    assert again.error == "该Mod已经存在"
    materialize.assert_not_called()


def test_source_url_duplicate(tmp_path: Path, db: DatabaseManager) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    url = "https://www.nexusmods.com/palworld/mods/10062"
    first = NexusImporter(db=db).import_mod(
        source_folder=_folder(tmp_path, "a"),
        title="A",
        nexus_url=url,
        nexus_id="10062",
        library_root=lib,
        context=PALWORLD,
    )
    assert first.success

    # Same URL, different external_id path would still collide on URL.
    dup = check_import_duplicate(
        db,
        platform=PLATFORM_NEXUS,
        external_id="99999",
        source_url=url + "?tab=files",
        app_id=1623730,
    )
    assert dup is not None
    assert dup.is_duplicate

    materialize = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "services.importers.nexus.materialize_imported_mod",
            materialize,
        )
        again = NexusImporter(db=db).import_mod(
            source_folder=_folder(tmp_path, "b"),
            title="B",
            nexus_url=url,
            nexus_id="10062",
            library_root=lib,
            context=PALWORLD,
        )
    assert again.is_duplicate
    materialize.assert_not_called()


def test_different_workshop_ids_import_ok(tmp_path: Path, db: DatabaseManager) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    a = SteamImporter(db=db).import_mod(
        workshop_id="123", title="A", library_root=lib, context=PALWORLD
    )
    b = SteamImporter(db=db).import_mod(
        workshop_id="456", title="B", library_root=lib, context=PALWORLD
    )
    assert a.success
    assert b.success
    assert a.mod_id != b.mod_id


def test_batch_skips_duplicate_without_failing(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "batch_dup.db")
    manager.update_game_deploy_config(1623730, name="Palworld")
    monkeypatch.setattr("ui.import_thread.get_db", lambda: manager)

    parent = tmp_path / "batch"
    dirs = []
    for name in ("ModA", "ModB", "ModC"):
        d = parent / name
        d.mkdir(parents=True)
        (d / "a.pak").write_bytes(b"1")
        dirs.append(d)

    lib = tmp_path / "lib"
    # Pre-register ModB as existing Nexus external id = folder name (batch identity).
    NexusImporter(db=manager).import_mod(
        source_folder=dirs[1],
        title="ModB",
        nexus_url="",
        nexus_id="ModB",
        library_root=lib,
        context=PALWORLD,
        is_batch_mode=True,
    )

    worker = ImportWorker(
        platform=PLATFORM_NEXUS,
        library_root=lib,
        params={
            "folder": str(parent),
            "is_batch_mode": True,
            "game_id": 1623730,
            "game_name": "Palworld",
            "context": PALWORLD,
        },
    )
    result = worker._do_batch_folder_import(dirs)
    assert result.success
    assert int(result.imported_count or 0) == 2
    assert int(result.skipped_count or 0) == 1
