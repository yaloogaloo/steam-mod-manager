"""Phase 4 — Mod Import UI + workflow (Steam / Nexus / GitHub)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox

from core.db_manager import (
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    DatabaseManager,
)
from core.mod_platform import PLATFORM_OTHER
from core.game_info import GameInfo
from services.file_ops import (
    INFO_DIR_NAME,
    MISSING_CONTENT_METADATA_KEY,
    ModFileManager,
    read_info_metadata_dict,
    read_is_missing_content,
)
from services.importers import (
    GithubImporter,
    NexusImporter,
    SteamImporter,
    scan_mod_directory,
)
from services.importers.importer_base import ImportContext
from services.importers.local_scanner import classify_file_kind
from services.mod_files import scan_folder_to_mod_files
from ui.library_view import ModLibraryView
from ui.mod_card import ModCardWidget
from ui.import_thread import ImportWorker
from ui.mod_import_dialog import ModImportDialog

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "import_ui.db")
    manager.upsert_game(
        GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld")
    )
    yield manager
    DatabaseManager.reset_instance()


def _mod_folder(root: Path, name: str, files: dict[str, bytes]) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    for rel, data in files.items():
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return folder


def test_local_scanner_classifies_and_builds_mod_files(tmp_path: Path) -> None:
    folder = _mod_folder(
        tmp_path,
        "Pack",
        {
            "Character.pak": b"main",
            "Optional/HatAddon.pak": b"hat",
            "config.ini": b"x=1",
            "plugin.dll": b"MZ",
            "meta.json": b"{}",
            "main.zip": b"PK",
            "Optional/extra.7z": b"7z",
        },
    )
    assert classify_file_kind("a.pak") == "pak"
    assert classify_file_kind("b.DLL") == "dll"
    assert classify_file_kind("c.json") == "json"
    assert classify_file_kind("d.ini") == "ini"
    assert classify_file_kind("e.cfg") == "cfg"
    assert classify_file_kind("f.bin") == "other"

    # Archives only — loose pak/json/dll never enter Files list.
    bundle = scan_mod_directory(folder)
    assert {f.filename for f in bundle.files} == {"main.zip", "extra.7z"}
    enabled = [f for f in bundle.files if f.enabled]
    assert len(enabled) == 1
    assert enabled[0].type == "main"
    optionals = [f for f in bundle.files if not f.enabled]
    assert len(optionals) == 1
    # Alias used by older callers
    assert len(scan_folder_to_mod_files(folder).files) == len(bundle.files)


def test_steam_import_into_library(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    lib.mkdir()
    result = SteamImporter(db=db).import_mod(
        workshop_id="3761838546",
        title="Storage Mod",
        library_root=lib,
    )
    assert result.success
    assert result.platform == PLATFORM_STEAM
    assert result.external_id == "3761838546"
    assert "filedetails/?id=3761838546" in result.source_url
    info = db.get_mod_display_info("3761838546")
    assert info is not None
    assert info.platform == PLATFORM_STEAM

    folders = ModFileManager(lib).list_managed_mods()
    assert len(folders) == 1
    meta = ModFileManager(lib).load_metadata(folders[0])
    assert meta is not None
    assert meta.published_file_id == "3761838546"


def test_nexus_import_multi_file_and_ui_refresh(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "library"
    lib.mkdir()
    src = _mod_folder(
        tmp_path,
        "MyCharacterMod",
        {
            "Character.zip": b"PK",
            "Optional/HatAddon.7z": b"7z",
            # Loose packages ignored by Files list scan.
            "Character.pak": b"A",
        },
    )
    result = NexusImporter(db=db).import_mod(
        source_folder=src,
        title="My Character Mod",
        nexus_url="https://www.nexusmods.com/palworld/mods/4242",
        nexus_id="4242",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success
    assert result.platform == PLATFORM_NEXUS
    assert result.external_id == "4242"
    assert result.files_count == 2
    files = db.get_mod_files(result.mod_id).files
    assert sum(1 for f in files if f.enabled) == 1
    assert any(f.path.endswith("HatAddon.7z") for f in files)
    assert result.managed_path
    assert (Path(result.managed_path) / INFO_DIR_NAME / "metadata.json").is_file()

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()
    assert len(view._cards) == 1
    card = view._cards[0]
    assert isinstance(card, ModCardWidget)
    assert not card.platform_badge.isHidden()
    assert "Nexus" in card.platform_badge.text()

    # Dialog path: import another mod via async ImportWorker
    src2 = _mod_folder(tmp_path, "Another", {"main.pak": b"Z"})
    dialog = ModImportDialog(lib, parent=view, game_context=PALWORLD.as_dict())
    dialog.radio_nexus.setChecked(True)
    dialog.nexus_url_edit.setText("https://www.nexusmods.com/palworld/mods/99")
    dialog.nexus_folder_edit.setText(str(src2))
    dialog.nexus_title_edit.setText("Another Nexus")
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    )
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    )
    dialog._on_import()
    import time

    for _ in range(100):
        qapp.processEvents()
        if dialog.last_result is not None or dialog._worker is None:
            if dialog.last_result is not None:
                break
            if dialog._worker is None and dialog.last_result is None:
                # still starting
                pass
        if dialog._worker is not None and not dialog._worker.isRunning() and dialog.last_result:
            break
        time.sleep(0.02)
    assert dialog.last_result is not None
    assert dialog.last_result.success
    view.refresh()
    qapp.processEvents()
    assert len(view._cards) == 2


def test_github_import(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    lib.mkdir()
    src = _mod_folder(
        tmp_path,
        "repo",
        {"mod.zip": b"PK", "extra.7z": b"7z", "settings.json": b"{}", "helper.dll": b"x"},
    )
    result = GithubImporter(db=db).import_mod(
        github_url="https://github.com/user/project",
        source_folder=src,
        title="Repo Mod",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success
    assert result.platform == PLATFORM_GITHUB
    assert result.external_id == "user/project"
    assert result.source_url.startswith("https://github.com/user/project")
    assert result.files_count >= 2
    assert ModFileManager(lib).list_managed_mods()


def test_duplicate_and_missing_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    lib.mkdir()
    src = _mod_folder(tmp_path, "Once", {"a.pak": b"1"})
    first = NexusImporter(db=db).import_mod(
        source_folder=src,
        nexus_url="https://www.nexusmods.com/x/mods/7",
        nexus_id="7",
        library_root=lib,
        context=PALWORLD,
    )
    assert first.success
    again = NexusImporter(db=db).import_mod(
        source_folder=src,
        nexus_url="https://www.nexusmods.com/x/mods/7",
        nexus_id="7",
        library_root=lib,
        context=PALWORLD,
    )
    assert not again.success
    assert again.error == "该Mod已经存在"

    missing = NexusImporter(db=db).import_mod(
        source_folder=tmp_path / "nope",
        nexus_id="8",
        library_root=lib,
        context=PALWORLD,
    )
    assert not missing.success
    assert missing.error == "Mod目录不存在"

    steam = SteamImporter(db=db).import_mod(
        workshop_id="111", title="S", library_root=lib, context=PALWORLD
    )
    assert steam.success
    steam2 = SteamImporter(db=db).import_mod(
        workshop_id="111", title="S", library_root=lib, context=PALWORLD
    )
    assert not steam2.success
    assert steam2.error == "该Mod已经存在"

    gh = GithubImporter(db=db).import_mod(
        github_url="https://github.com/a/b",
        source_folder=tmp_path / "missing-clone",
        context=PALWORLD,
    )
    assert not gh.success
    assert gh.error == "Mod目录不存在"


def test_import_dialog_empty_path_creates_missing_content(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    del qapp
    lib = tmp_path / "library"
    lib.mkdir()
    dlg = ModImportDialog(
        lib,
        game_context={"game_id": 1623730, "game_name": "Palworld"},
    )
    dlg.radio_other.setChecked(True)
    dlg.other_title_edit.setText("Empty Stub")
    dlg.other_folder_edit.clear()
    params = dlg._collect_params(PLATFORM_OTHER)
    assert params is not None
    stub = Path(params["folder"])
    assert stub.is_dir()
    assert not any(stub.iterdir())

    result = ImportWorker(
        platform=PLATFORM_OTHER,
        library_root=lib,
        params=params,
    )._do_import()
    assert result.success, result.error
    assert result.managed_path
    managed = Path(result.managed_path)
    assert read_is_missing_content(managed)
    meta = read_info_metadata_dict(managed) or {}
    assert meta.get(MISSING_CONTENT_METADATA_KEY) is True
    dlg.close()


def test_import_button_present(qapp: QApplication, tmp_path: Path, db: DatabaseManager) -> None:
    lib = tmp_path / "library"
    lib.mkdir()
    view = ModLibraryView()
    view.set_target_root(str(lib))
    assert view.import_btn.text() == "导入 Mod"
    assert view.import_btn.isEnabled()
