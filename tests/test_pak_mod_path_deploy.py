"""Generic pak_mod_path deploy and custom install directory picker defaults."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import PLATFORM_NEXUS
from services.deploy import ModDeployer
from services.deploy_rules import DEPLOY_TYPE_FOLDER_COPY, DEPLOY_TYPE_PAK_MOD_PATH
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.importers.archive import ArchiveImporter
from services.importers.importer_base import ImportContext
from ui.edit_mod_dialog import EditModDialog

BG3_APP_ID = 1086940


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "pak_mod_path.db")
    manager.upsert_game(
        GameInfo(app_id=BG3_APP_ID, name="Baldur's Gate 3", folder_name="BG3")
    )
    manager.upsert_game(
        GameInfo(app_id=100, name="SomeGame", folder_name="SomeGame")
    )
    yield manager
    DatabaseManager.reset_instance()


def _write_meta(mod_dir: Path, *, mid: str, title: str, app_id: int) -> None:
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "{title}",\n'
        f'  "app_id": {app_id}\n'
        "}\n",
        encoding="utf-8",
    )


def _make_zip(path: Path, mapping: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in mapping.items():
            zf.writestr(name, data)
    return path


def test_bg3_zip_pak_deploys_flat_to_mod_path(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """BG3-style archive: zip containing a loose .pak → game.mod_path (flat)."""
    library = tmp_path / "library"
    library.mkdir()
    mod_path = tmp_path / "BG3Mods"
    mod_path.mkdir()
    db.update_game_deploy_config(
        BG3_APP_ID,
        name="Baldur's Gate 3",
        install_path=str(tmp_path / "BG3Install"),
        mod_path=str(mod_path),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )

    zpath = _make_zip(tmp_path / "mod.zip", {"MyCoolMod.pak": b"PAKDATA"})
    result = ArchiveImporter(db=db).import_mod(
        archive_path=zpath,
        platform=PLATFORM_NEXUS,
        nexus_id="bg3-1",
        title="CoolPakMod",
        library_root=library,
        context=ImportContext(game_id=BG3_APP_ID, game_name="Baldur's Gate 3"),
    )
    assert result.success, result.error

    deploy = ModDeployer(library_root=library, db=db).deploy_mod(result.mod_id)
    assert deploy["success"] is True, deploy
    assert deploy["deploy_type"] == DEPLOY_TYPE_PAK_MOD_PATH
    assert (mod_path / "MyCoolMod.pak").read_bytes() == b"PAKDATA"
    assert not (mod_path / "CoolPakMod").exists()


def test_generic_game_without_pak_keeps_folder_copy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Non-pak Mods still deploy via folder_copy into mod_path/<folder>/."""
    library = tmp_path / "library"
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    mod = library / "SomeGame" / "PlainMod"
    mod.mkdir(parents=True)
    (mod / "a.txt").write_text("A", encoding="utf-8")
    _write_meta(mod, mid="81001", title="PlainMod", app_id=100)

    db.update_game_deploy_config(
        100,
        name="SomeGame",
        mod_path=str(mods_root),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    db.upsert_mod(ModMetadata(published_file_id="81001", title="PlainMod", app_id=100))

    result = ModDeployer(library_root=library, db=db).deploy_mod("81001")
    assert result["success"] is True
    assert result["deploy_type"] == DEPLOY_TYPE_FOLDER_COPY
    assert (mods_root / "PlainMod" / "a.txt").read_text(encoding="utf-8") == "A"


def test_custom_deploy_path_overrides_pak_mod_path(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """custom_deploy_path wins over pak_mod_path."""
    library = tmp_path / "library"
    managed = library / "BG3" / "CustomPak"
    managed.mkdir(parents=True)
    (managed / "Override.pak").write_bytes(b"OVERRIDE")
    _write_meta(managed, mid="82001", title="CustomPak", app_id=BG3_APP_ID)

    custom = tmp_path / "custom_target"
    custom.mkdir()
    db.upsert_mod(
        ModMetadata(published_file_id="82001", title="CustomPak", app_id=BG3_APP_ID)
    )
    db.update_mod_user_metadata(
        82001,
        {
            "display_name": "CustomPak",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
            "custom_deploy_path": str(custom),
        },
    )
    db.update_game_deploy_config(
        BG3_APP_ID,
        name="Baldur's Gate 3",
        mod_path=str(tmp_path / "unused_mod_path"),
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod("82001")
    assert result["success"] is True, result
    assert (custom / "Override.pak").is_file()
    assert not (tmp_path / "unused_mod_path" / "Override.pak").exists()


def test_edit_dialog_browse_starts_at_game_install_path(
    qapp: QApplication,
) -> None:
    install = r"D:\Games\BG3"
    dlg = EditModDialog(
        game_install_path=install,
        custom_deploy_path=r"E:\Somewhere\Else",
    )
    captured: dict[str, str] = {}

    def _fake_dialog(_parent, _title, start_dir: str) -> str:
        captured["start"] = start_dir
        return ""

    with patch(
        "ui.edit_mod_dialog.QFileDialog.getExistingDirectory",
        side_effect=_fake_dialog,
    ):
        dlg._browse_custom_deploy()

    assert captured["start"] == install
    dlg.close()


def test_edit_dialog_browse_uses_each_game_install_path(
    qapp: QApplication,
) -> None:
    paths = {
        "game_a": r"D:\Steam\common\GameA",
        "game_b": r"E:\Library\GameB",
    }
    seen: list[str] = []

    def _fake_dialog(_parent, _title, start_dir: str) -> str:
        seen.append(start_dir)
        return ""

    with patch(
        "ui.edit_mod_dialog.QFileDialog.getExistingDirectory",
        side_effect=_fake_dialog,
    ):
        for start in paths.values():
            dlg = EditModDialog(game_install_path=start)
            dlg._browse_custom_deploy()
            dlg.close()

    assert seen == list(paths.values())
