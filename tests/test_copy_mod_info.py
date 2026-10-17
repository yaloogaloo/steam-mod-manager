"""Clipboard actions on Mod DetailPanel (compat suite)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox

from core.db_manager import PLATFORM_NEXUS, DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import ModFileEntry, ModFilesBundle
from tests.helpers.identity import prove_managed_folder
from ui.mod_detail_panel import ModDetailPanel
from ui.platform_labels import format_mod_info_clipboard


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "copy_info.db")
    manager.upsert_game(
        GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld")
    )
    yield manager
    DatabaseManager.reset_instance()


def test_format_mod_info_clipboard() -> None:
    text = format_mod_info_clipboard(
        name="Pal Analyzer",
        platform=PLATFORM_NEXUS,
        source_url="https://www.nexusmods.com/palworld/mods/336",
        external_id="336",
        files=["Pal Analyzer.pak"],
        deploy_status="已部署",
    )
    assert "名称:\nPal Analyzer" in text
    assert "平台:\nNexus Mods" in text
    assert "来源:\nhttps://www.nexusmods.com/palworld/mods/336" in text
    assert "Workspace ID:\n336" in text
    assert "文件:\nPal Analyzer.pak" in text


def test_copy_link_and_copy_info_buttons(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Ok)

    bundle = ModFilesBundle(
        files=[ModFileEntry(name="Pal Analyzer.pak", filename="Pal Analyzer.pak")]
    )
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="336",
        source_url="https://www.nexusmods.com/palworld/mods/336",
        title="Pal Analyzer",
        mod_files=bundle,
        app_id=1623730,
        game_name="Palworld",
    )
    pk = str(info.mod_id)
    folder = tmp_path / "Palworld" / "Pal Analyzer"
    folder.mkdir(parents=True)
    (folder / "payload.zip").write_bytes(b"z")
    prove_managed_folder(
        db,
        folder,
        handle=pk,
        title="Pal Analyzer",
        app_id=1623730,
        game_name="Palworld",
        platform=PLATFORM_NEXUS,
        extra={
            "url": "https://www.nexusmods.com/palworld/mods/336",
            "source_type": PLATFORM_NEXUS,
        },
    )

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=pk)
    qapp.processEvents()

    assert panel.btn_copy_link is not None
    assert panel.btn_copy_info is not None
    assert panel.btn_copy_link.text() == "复制链接"
    assert "复制全部信息" in (panel.btn_copy_info.toolTip() or "")

    url = "https://www.nexusmods.com/palworld/mods/336"
    assert panel._current_source_url() == url

    copied: list[str] = []
    real_copy = panel._copy_to_clipboard

    def _spy(text: str) -> bool:
        copied.append(text)
        return real_copy(text)

    monkeypatch.setattr(panel, "_copy_to_clipboard", _spy)

    panel._copy_source_url()
    assert copied[-1] == url

    panel._copy_mod_info()
    payload = copied[-1]
    assert "Pal Analyzer" in payload
    assert "Nexus Mods" in payload
    assert url in payload
    assert "336" in payload
    assert "Pal Analyzer.pak" in payload
