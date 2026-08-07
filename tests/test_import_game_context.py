"""Import must inherit Current Game Context (never invent GitHub/Nexus games)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox

from core.db_manager import PLATFORM_GITHUB, PLATFORM_NEXUS, DatabaseManager
from core.game_info import GameInfo
from services.file_ops import ModFileManager
from services.importers import (
    GithubImporter,
    MISSING_GAME_CONTEXT,
    NexusImporter,
)
from services.importers.importer_base import ImportContext
from ui.library_view import ModLibraryView
from ui.mod_import_dialog import ModImportDialog


PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")
SKYRIM = ImportContext(game_id=72850, game_name="Skyrim")


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "game_ctx.db")
    manager.upsert_game(
        GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld")
    )
    manager.upsert_game(
        GameInfo(app_id=72850, name="Skyrim", folder_name="Skyrim")
    )
    yield manager
    DatabaseManager.reset_instance()


def _folder(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def test_github_inherits_palworld_context(
    tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    lib.mkdir()
    src = _folder(tmp_path, "AutoLoot", {"AutoPickUp.pak": b"1"})
    result = GithubImporter(db=db).import_mod(
        github_url="https://github.com/xxx/AutoPickUp",
        source_folder=src,
        title="AutoLoot",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    assert result.platform == PLATFORM_GITHUB
    assert result.game_id == 1623730
    assert result.game_name == "Palworld"
    info = db.get_mod_display_info(result.mod_id)
    assert info is not None
    assert info.app_id == 1623730
    games = ModFileManager(lib).list_games()
    assert "Palworld" in games
    assert "GitHub" not in games


def test_nexus_inherits_skyrim_context(
    tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    lib.mkdir()
    src = _folder(tmp_path, "SkyMod", {"main.pak": b"X"})
    result = NexusImporter(db=db).import_mod(
        source_folder=src,
        nexus_url="https://www.nexusmods.com/skyrim/mods/4242",
        nexus_id="4242",
        title="Sky Mod",
        library_root=lib,
        context=SKYRIM,
    )
    assert result.success, result.error
    assert result.platform == PLATFORM_NEXUS
    assert result.external_id == "4242"
    assert result.game_id == 72850
    assert result.game_name == "Skyrim"
    assert "Skyrim" in ModFileManager(lib).list_games()
    assert "Nexus Mods" not in ModFileManager(lib).list_games()


def test_missing_game_context_fails(tmp_path: Path, db: DatabaseManager) -> None:
    src = _folder(tmp_path, "repo", {"a.pak": b"1"})
    result = GithubImporter(db=db).import_mod(
        github_url="https://github.com/a/b",
        source_folder=src,
        library_root=tmp_path / "lib",
    )
    assert not result.success
    assert result.error == MISSING_GAME_CONTEXT


def test_rejects_platform_as_game_name(
    tmp_path: Path, db: DatabaseManager
) -> None:
    src = _folder(tmp_path, "repo", {"a.pak": b"1"})
    result = GithubImporter(db=db).import_mod(
        github_url="https://github.com/a/c",
        source_folder=src,
        library_root=tmp_path / "lib2",
        game_name="GitHub",
        app_id=1623730,
    )
    # game_name=GitHub is invalid → require_import_context fails unless
    # context supplies a real name. app_id alone with invalid name:
    assert not result.success
    assert result.error == MISSING_GAME_CONTEXT


def test_library_blocks_import_when_all_games(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "library"
    (lib / "Palworld").mkdir(parents=True)
    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    # Default selection is 全部游戏
    assert view.get_current_game_context() is None

    warned: list[str] = []

    def _warn(parent, title, text, *a, **k):
        warned.append(str(text))
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", _warn)
    view._on_import_mod()
    assert warned
    assert "请先选择目标游戏" in warned[0]


def test_library_context_and_dialog_label(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    (lib / "Palworld").mkdir(parents=True)
    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    view._set_current_game_context("Palworld")
    ctx = view.get_current_game_context()
    assert ctx is not None
    assert ctx["game_name"] == "Palworld"
    assert int(ctx["game_id"]) == 1623730

    dialog = ModImportDialog(lib, game_context=ctx)
    assert "Palworld" in dialog.game_context_label.text()
