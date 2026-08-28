"""Directory import identity unification — batch and single share external_id."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_NEXUS, PLATFORM_STEAM
from services.importers.archive import ArchiveImporter
from services.importers.identity_resolve import (
    ImportIdentity,
    apply_directory_import_identity,
)
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from services.importers.steam import SteamImporter
from ui.import_thread import ImportWorker

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "identity_unify.db")
    manager.update_game_deploy_config(1623730, name="Palworld")
    yield manager
    DatabaseManager.reset_instance()


def _mod_dir(root: Path, name: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "payload.pak").write_bytes(b"pak")
    return folder


def test_apply_directory_identity_uses_folder_name() -> None:
    ident = ImportIdentity(platform=PLATFORM_NEXUS)
    out = apply_directory_import_identity(
        ident, folder=Path("NativeModLoader"), platform=PLATFORM_NEXUS
    )
    assert out.external_id == "NativeModLoader"
    assert out.source_url == ""


def test_apply_directory_identity_keeps_official_nexus_id() -> None:
    ident = ImportIdentity(
        platform=PLATFORM_NEXUS,
        external_id="10062",
        source_url="https://www.nexusmods.com/palworld/mods/10062",
    )
    out = apply_directory_import_identity(
        ident, folder=Path("NativeModLoader"), platform=PLATFORM_NEXUS
    )
    assert out.external_id == "10062"
    assert "10062" in out.source_url


def test_batch_then_single_same_folder_skips(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batch import NativeModLoader → single import same folder must skip."""
    monkeypatch.setattr("ui.import_thread.get_db", lambda: db)
    src = _mod_dir(tmp_path / "src", "NativeModLoader")
    lib = tmp_path / "lib"

    batch = NexusImporter(db=db).import_mod(
        source_folder=src,
        title="NativeModLoader",
        nexus_id="NativeModLoader",
        nexus_url="",
        library_root=lib,
        context=PALWORLD,
        is_batch_mode=True,
    )
    assert batch.success
    assert batch.external_id == "NativeModLoader"

    materialize = MagicMock()
    monkeypatch.setattr(
        "services.importers.nexus.materialize_imported_mod", materialize
    )

    worker = ImportWorker(
        platform=PLATFORM_NEXUS,
        library_root=lib,
        params={
            "folder": str(src),
            "is_batch_mode": False,
            "nexus_url": "",
            "nexus_id": "",
            "title": "",
            "game_id": 1623730,
            "game_name": "Palworld",
            "context": PALWORLD,
        },
    )
    again = worker._import_one_folder(src, title="NativeModLoader", batch=False)
    assert again.is_duplicate
    assert again.mod_id == batch.mod_id
    materialize.assert_not_called()


def test_different_platform_same_id_not_duplicate(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Steam workshop id 944 and Nexus external_id 944 are distinct."""
    lib = tmp_path / "lib"
    steam = SteamImporter(db=db).import_mod(
        workshop_id="944",
        title="Steam944",
        library_root=lib,
        context=PALWORLD,
    )
    assert steam.success

    folder = _mod_dir(tmp_path / "src", "944")
    nexus = NexusImporter(db=db).import_mod(
        source_folder=folder,
        title="Nexus944",
        nexus_id="944",
        nexus_url="",
        library_root=lib,
        context=PALWORLD,
        is_batch_mode=True,
    )
    assert nexus.success
    assert nexus.mod_id != steam.mod_id
    assert not nexus.is_duplicate


def test_archive_without_official_id_does_not_false_skip(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Archive-only Nexus import without official id must not skip an unrelated mod."""
    lib = tmp_path / "lib"
    existing_dir = _mod_dir(tmp_path / "existing", "SomeOtherMod")
    existing = NexusImporter(db=db).import_mod(
        source_folder=existing_dir,
        title="SomeOtherMod",
        nexus_id="SomeOtherMod",
        nexus_url="",
        library_root=lib,
        context=PALWORLD,
        is_batch_mode=True,
    )
    assert existing.success

    zpath = tmp_path / "pack.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("inner/mod.pak", b"data")

    result = ArchiveImporter(db=db).import_mod(
        archive_path=zpath,
        platform=PLATFORM_NEXUS,
        title="pack",
        library_root=lib,
        nexus_url="",
        nexus_id="",
        game_name="Palworld",
        app_id=1623730,
        context=PALWORLD,
    )
    assert result.success
    assert not result.is_duplicate
    assert result.mod_id != existing.mod_id
