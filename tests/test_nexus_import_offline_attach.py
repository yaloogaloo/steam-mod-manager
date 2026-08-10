"""Nexus Mod import can optionally attach a user-saved offline HTML page."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_NONE,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    PROVIDER_NEXUS_MANUAL_IMPORT,
)
from services.file_ops import INFO_DIR_NAME
from services.importers.github import GithubImporter
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from services.importers.steam import SteamImporter
from services.offline.manager import attach_nexus_offline_page
from ui.import_thread import ImportWorker

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "nexus_import_offline.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _write_html(path: Path, body: str = "Nexus Offline") -> None:
    path.write_text(
        f"<!DOCTYPE html><html><body><h1>{body}</h1></body></html>",
        encoding="utf-8",
    )


def test_nexus_import_with_html_attaches_offline(
    tmp_path: Path, db: DatabaseManager
) -> None:
    src = tmp_path / "mod"
    src.mkdir()
    (src / "mod.pak").write_bytes(b"pak")
    html = tmp_path / "page.html"
    _write_html(html, "Imported Page")

    lib = tmp_path / "library"
    result = NexusImporter(db=db).import_mod(
        source_folder=src,
        title="WithHtml",
        nexus_url="https://www.nexusmods.com/palworld/mods/901",
        nexus_id="901",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error

    attach = attach_nexus_offline_page(
        result.mod_id,
        html,
        managed_path=result.managed_path,
        library_root=lib,
    )
    assert attach.provider == PROVIDER_NEXUS_MANUAL_IMPORT
    assert attach.status == OFFLINE_STATUS_ARCHIVED
    index = Path(result.managed_path) / INFO_DIR_NAME / "offline" / "index.html"
    assert index.is_file()
    assert "Imported Page" in index.read_text(encoding="utf-8")

    row = db.get_mod_display_info(result.mod_id)
    assert row is not None
    assert row.platform == PLATFORM_NEXUS
    assert row.offline_provider == PROVIDER_NEXUS_MANUAL_IMPORT
    assert row.offline_status == OFFLINE_STATUS_ARCHIVED


def test_nexus_archive_import_worker_with_html(
    tmp_path: Path, db: DatabaseManager
) -> None:
    zpath = tmp_path / "ModPack.zip"
    with ZipFile(zpath, "w") as zf:
        zf.writestr("mod.pak", b"pak")
    html = tmp_path / "ModPack.html"
    _write_html(html, "Zip Offline")

    lib = tmp_path / "library"
    worker = ImportWorker(
        platform=PLATFORM_NEXUS,
        library_root=lib,
        params={
            "source_path": str(zpath),
            "use_archive": True,
            "nexus_url": "https://www.nexusmods.com/palworld/mods/902",
            "nexus_id": "902",
            "title": "ZipMod",
            "game_id": 1623730,
            "game_name": "Palworld",
            "offline_html_path": str(html),
            "context": PALWORLD.as_dict(),
        },
    )
    result = worker._do_import()
    assert result.success, result.error
    index = Path(result.managed_path) / INFO_DIR_NAME / "offline" / "index.html"
    assert index.is_file()
    row = db.get_mod_display_info(result.mod_id)
    assert row is not None
    assert row.offline_provider == PROVIDER_NEXUS_MANUAL_IMPORT
    assert row.offline_status == OFFLINE_STATUS_ARCHIVED


def test_nexus_import_without_html_ok(tmp_path: Path, db: DatabaseManager) -> None:
    src = tmp_path / "mod"
    src.mkdir()
    (src / "mod.pak").write_bytes(b"pak")
    lib = tmp_path / "library"
    result = NexusImporter(db=db).import_mod(
        source_folder=src,
        title="NoHtml",
        nexus_url="https://www.nexusmods.com/palworld/mods/903",
        nexus_id="903",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    row = db.get_mod_display_info(result.mod_id)
    assert row is not None
    assert row.offline_status == OFFLINE_STATUS_NONE
    assert not (
        Path(result.managed_path) / INFO_DIR_NAME / "offline" / "index.html"
    ).is_file()


def test_steam_import_unchanged(tmp_path: Path, db: DatabaseManager) -> None:
    from core.game_info import GameInfo

    db.upsert_game(GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld"))
    lib = tmp_path / "library"
    result = SteamImporter(db=db).import_mod(
        workshop_id="3761838546",
        title="SteamMod",
        library_root=lib,
        game_name="Palworld",
        app_id=1623730,
    )
    assert result.success, result.error
    assert result.platform == PLATFORM_STEAM

    html = tmp_path / "ignored.html"
    _write_html(html)
    worker = ImportWorker(
        platform=PLATFORM_STEAM,
        library_root=lib,
        params={
            "workshop_id": "3761838547",
            "title": "Steam2",
            "game_id": 1623730,
            "game_name": "Palworld",
            "offline_html_path": str(html),
        },
    )
    r2 = worker._do_import()
    assert r2.success, r2.error
    assert r2.platform == PLATFORM_STEAM
    info = db.get_mod_display_info(r2.mod_id)
    assert info is not None
    assert info.offline_provider != PROVIDER_NEXUS_MANUAL_IMPORT


def test_github_import_unchanged(tmp_path: Path, db: DatabaseManager) -> None:
    src = tmp_path / "repo"
    src.mkdir()
    (src / "README.md").write_text("hi", encoding="utf-8")
    lib = tmp_path / "library"
    result = GithubImporter(db=db).import_mod(
        github_url="https://github.com/o/r",
        source_folder=src,
        title="GhMod",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    assert result.platform == PLATFORM_GITHUB


def test_dialog_shows_offline_html_only_for_nexus(qapp, tmp_path: Path) -> None:
    from ui.mod_import_dialog import ModImportDialog

    dlg = ModImportDialog(
        tmp_path / "lib",
        game_context={"game_id": 1623730, "game_name": "Palworld"},
    )
    dlg.radio_steam.setChecked(True)
    dlg._on_platform_toggled()
    assert dlg.offline_html_row.isHidden()

    dlg.radio_nexus.setChecked(True)
    dlg._on_platform_toggled()
    assert not dlg.offline_html_row.isHidden()

    dlg.radio_github.setChecked(True)
    dlg._on_platform_toggled()
    assert dlg.offline_html_row.isHidden()
