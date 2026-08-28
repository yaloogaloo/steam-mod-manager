"""Custom deploy directory picker must open at game.install_path."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from services.deploy_status import resolve_game_install_path
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from ui.edit_mod_dialog import EditModDialog
from ui.mod_detail_panel import ModDetailPanel

BG3_APP_ID = 1086940
ANNO_APP_ID = 916440


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "install_picker.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed_game(
    db: DatabaseManager,
    *,
    app_id: int,
    name: str,
    install: Path,
) -> None:
    db.upsert_game(GameInfo(app_id=app_id, name=name, folder_name=name))
    db.update_game_deploy_config(
        app_id,
        name=name,
        install_path=str(install),
        mod_path=str(install / "Mods"),
    )


def _mod_folder(
    root: Path,
    *,
    game: str,
    mid: str,
    title: str,
    app_id: int,
) -> Path:
    folder = root / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": title,
                "app_id": app_id,
                "game_name": game,
            }
        ),
        encoding="utf-8",
    )
    return folder


def _capture_dialog_browse(monkeypatch, panel: ModDetailPanel) -> dict[str, object]:
    captured: dict[str, object] = {}

    def _fake_exec(self: EditModDialog) -> int:
        captured["game_id"] = self._game_id
        captured["install"] = self.browse_start_directory()
        captured["dialog"] = self
        return int(QDialog.DialogCode.Rejected)

    monkeypatch.setattr(EditModDialog, "exec", _fake_exec)
    panel.open_edit_info_dialog()
    return captured


def test_resolve_game_install_path_from_mod(db: DatabaseManager, tmp_path: Path) -> None:
    install = tmp_path / "BG3Root"
    install.mkdir()
    _seed_game(db, app_id=BG3_APP_ID, name="Baldur's Gate 3", install=install)
    db.upsert_mod(
        ModMetadata(published_file_id="88001", title="PakMod", app_id=BG3_APP_ID)
    )
    assert resolve_game_install_path(mod_id="88001", db=db) == str(install)
    assert resolve_game_install_path(app_id=BG3_APP_ID, db=db) == str(install)


def test_bg3_edit_dialog_browse_starts_at_bg3_root(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    install = tmp_path / "Baldurs Gate 3"
    install.mkdir()
    _seed_game(db, app_id=BG3_APP_ID, name="Baldur's Gate 3", install=install)
    folder = _mod_folder(
        tmp_path / "library",
        game="BG3",
        mid="88011",
        title="CoolPak",
        app_id=BG3_APP_ID,
    )
    db.upsert_mod(
        ModMetadata(published_file_id="88011", title="CoolPak", app_id=BG3_APP_ID)
    )

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id="88011", game_id=BG3_APP_ID)
    captured = _capture_dialog_browse(monkeypatch, panel)

    assert captured["game_id"] == BG3_APP_ID
    assert captured["install"] == str(install)

    starts: list[str] = []

    def _fake_dialog(_parent, _title, start_dir: str) -> str:
        starts.append(start_dir)
        return ""

    with patch(
        "ui.edit_mod_dialog.QFileDialog.getExistingDirectory",
        side_effect=_fake_dialog,
    ):
        captured["dialog"]._browse_custom_deploy()  # type: ignore[index]
    assert starts == [str(install)]


def test_anno_edit_dialog_browse_starts_at_anno_root(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    install = tmp_path / "Anno 1800"
    install.mkdir()
    _seed_game(db, app_id=ANNO_APP_ID, name="Anno 1800", install=install)
    folder = _mod_folder(
        tmp_path / "library",
        game="Anno",
        mid="88022",
        title="AnnoMod",
        app_id=ANNO_APP_ID,
    )
    db.upsert_mod(
        ModMetadata(published_file_id="88022", title="AnnoMod", app_id=ANNO_APP_ID)
    )

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id="88022", game_id=ANNO_APP_ID)
    captured = _capture_dialog_browse(monkeypatch, panel)
    assert captured["install"] == str(install)


def test_multi_game_mods_open_different_install_roots(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    bg3_install = tmp_path / "BG3Install"
    anno_install = tmp_path / "AnnoInstall"
    bg3_install.mkdir()
    anno_install.mkdir()
    _seed_game(db, app_id=BG3_APP_ID, name="Baldur's Gate 3", install=bg3_install)
    _seed_game(db, app_id=ANNO_APP_ID, name="Anno 1800", install=anno_install)

    bg3_folder = _mod_folder(
        tmp_path / "library",
        game="BG3",
        mid="88031",
        title="Bg3Mod",
        app_id=BG3_APP_ID,
    )
    anno_folder = _mod_folder(
        tmp_path / "library",
        game="Anno",
        mid="88032",
        title="AnnoMod",
        app_id=ANNO_APP_ID,
    )
    db.upsert_mod(
        ModMetadata(published_file_id="88031", title="Bg3Mod", app_id=BG3_APP_ID)
    )
    db.upsert_mod(
        ModMetadata(published_file_id="88032", title="AnnoMod", app_id=ANNO_APP_ID)
    )

    panel = ModDetailPanel()
    panel.show_mod(bg3_folder, mod_id="88031", game_id=BG3_APP_ID)
    bg3_cap = _capture_dialog_browse(monkeypatch, panel)
    panel.show_mod(anno_folder, mod_id="88032", game_id=ANNO_APP_ID)
    anno_cap = _capture_dialog_browse(monkeypatch, panel)

    assert bg3_cap["install"] == str(bg3_install)
    assert anno_cap["install"] == str(anno_install)
    assert bg3_cap["install"] != anno_cap["install"]


def test_dialog_resolves_install_path_from_mod_id_alone(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    """Even if caller forgets game_install_path, mod_id alone is enough."""
    install = tmp_path / "GameRoot"
    install.mkdir()
    _seed_game(db, app_id=BG3_APP_ID, name="Baldur's Gate 3", install=install)
    db.upsert_mod(
        ModMetadata(published_file_id="88041", title="OnlyModId", app_id=BG3_APP_ID)
    )

    dlg = EditModDialog(mod_id="88041")  # no game_install_path / game_id
    assert dlg.browse_start_directory() == str(install)
    dlg.close()
