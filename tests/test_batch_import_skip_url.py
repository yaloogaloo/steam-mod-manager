"""Batch import skips source URL; single import still requires it."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_GITHUB, PLATFORM_NEXUS
from services.importers.github import GithubImporter
from services.importers.nexus import NexusImporter
from ui.mod_import_dialog import ModImportDialog


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "batch_skip.db")
    manager.update_game_deploy_config(1623730, name="Palworld")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _batch_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "batch"
    for name in ("ModA", "ModB"):
        d = parent / name
        d.mkdir(parents=True)
        (d / "main.pak").write_bytes(b"pak")
    return parent


def test_collect_params_batch_skips_github_url(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    del db
    parent = _batch_parent(tmp_path)
    dlg = ModImportDialog(
        tmp_path / "lib",
        game_context={"game_id": 1623730, "game_name": "Palworld"},
    )
    dlg.radio_github.setChecked(True)
    dlg.github_src_folder.setChecked(True)
    dlg.github_folder_edit.setText(str(parent))
    dlg.github_url_edit.clear()
    dlg._refresh_batch_mode_ui()

    params = dlg._collect_params(PLATFORM_GITHUB)
    assert params is not None
    assert params["is_batch_mode"] is True
    assert params["github_url"] == ""


def test_collect_params_single_github_still_requires_url(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    del db
    single = tmp_path / "OneMod"
    single.mkdir()
    (single / "main.pak").write_bytes(b"pak")

    warned: list[str] = []

    def _warn(_self, title: str, text: str) -> None:
        warned.append(f"{title}:{text}")

    monkeypatch.setattr(
        "ui.mod_import_dialog.QMessageBox.warning",
        lambda *args, **kwargs: _warn(args[0] if args else None, args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else ""),
    )

    dlg = ModImportDialog(
        tmp_path / "lib",
        game_context={"game_id": 1623730, "game_name": "Palworld"},
    )
    dlg.radio_github.setChecked(True)
    dlg.github_src_folder.setChecked(True)
    dlg.github_folder_edit.setText(str(single))
    dlg.github_url_edit.clear()

    params = dlg._collect_params(PLATFORM_GITHUB)
    assert params is None
    assert any("GitHub URL" in w for w in warned)


def test_nexus_batch_forces_empty_source_url(
    tmp_path: Path, db: DatabaseManager
) -> None:
    folder = tmp_path / "Cool_Mod_Name"
    folder.mkdir()
    (folder / "a.pak").write_bytes(b"a")
    lib = tmp_path / "lib"
    result = NexusImporter(db=db).import_mod(
        source_folder=folder,
        title="Cool_Mod_Name",
        nexus_id="Cool_Mod_Name",
        nexus_url="",
        library_root=lib,
        context={"game_id": 1623730, "game_name": "Palworld"},
        is_batch_mode=True,
    )
    assert result.success
    assert result.source_url == ""
    info = db.get_mod_display_info(result.mod_id)
    assert info is not None
    assert (info.source_url or "") == ""


def test_github_batch_import_without_url(
    tmp_path: Path, db: DatabaseManager
) -> None:
    folder = tmp_path / "LocalMod"
    folder.mkdir()
    (folder / "a.pak").write_bytes(b"a")
    lib = tmp_path / "lib"
    result = GithubImporter(db=db).import_mod(
        github_url="",
        source_folder=folder,
        title="LocalMod",
        library_root=lib,
        context={"game_id": 1623730, "game_name": "Palworld"},
        is_batch_mode=True,
        external_id_suffix="LocalMod",
    )
    assert result.success
    assert result.external_id == "local/LocalMod"
    assert result.source_url == ""


def test_nexus_batch_flag_clears_shared_url(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    del db
    parent = _batch_parent(tmp_path)
    dlg = ModImportDialog(
        tmp_path / "lib",
        game_context={"game_id": 1623730, "game_name": "Palworld"},
    )
    dlg.radio_nexus.setChecked(True)
    dlg.nexus_src_folder.setChecked(True)
    dlg.nexus_folder_edit.setText(str(parent))
    dlg.nexus_url_edit.setText("https://www.nexusmods.com/palworld/mods/999")
    params = dlg._collect_params(PLATFORM_NEXUS)
    assert params is not None
    assert params["is_batch_mode"] is True
    assert params["nexus_url"] == ""
    assert params["nexus_id"] == ""
