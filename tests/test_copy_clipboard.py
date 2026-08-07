"""Phase 6 — clipboard copy helpers and DetailPanel actions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox

from core.db_manager import PLATFORM_NEXUS, DatabaseManager
from core.mod_platform import ModFileEntry, ModFilesBundle
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
    manager = DatabaseManager.instance(tmp_path / "clip.db")
    yield manager
    DatabaseManager.reset_instance()


def _mod_folder(root: Path, *, pub_id: str, title: str) -> Path:
    folder = root / "Palworld" / title
    info = folder / ".info"
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps(
            {
                "published_file_id": pub_id,
                "title": title,
                "game_name": "Palworld",
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_format_mod_info_clipboard_phase6() -> None:
    text = format_mod_info_clipboard(
        name="Pal Analyzer",
        platform=PLATFORM_NEXUS,
        source_url="https://www.nexusmods.com/palworld/mods/336",
        external_id="336",
        files=["main.zip", "optional_hat.zip"],
    )
    assert "名称:\nPal Analyzer" in text
    assert "平台:\nNexus Mods" in text
    assert "ID:\n336" in text
    assert "来源:\nhttps://www.nexusmods.com/palworld/mods/336" in text
    assert "文件:\nmain.zip\noptional_hat.zip" in text


def test_copy_buttons_and_clipboard(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        QMessageBox, "information", lambda *a, **k: QMessageBox.StandardButton.Ok
    )
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Ok
    )

    bundle = ModFilesBundle(
        files=[
            ModFileEntry(name="main.zip", filename="main.zip"),
            ModFileEntry(name="optional_hat.zip", filename="optional_hat.zip"),
        ]
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
    folder = _mod_folder(tmp_path, pub_id=info.mod_id, title="Pal Analyzer")
    panel = ModDetailPanel()
    panel.show_mod(folder)

    assert panel.btn_copy_name.text() == "复制"
    assert panel.btn_copy_id.text() == "复制"
    assert panel.btn_copy_source_url.text() == "复制链接"
    assert panel.btn_copy_info.text() == "复制全部信息"

    copied: list[str] = []
    real_copy = panel._copy_to_clipboard

    def _spy(text: str) -> bool:
        copied.append(text)
        return real_copy(text)

    monkeypatch.setattr(panel, "_copy_to_clipboard", _spy)

    panel._copy_id()
    assert copied[-1] == "336"

    panel._copy_name()
    assert copied[-1] == "Pal Analyzer"

    panel._copy_source_url()
    assert copied[-1] == "https://www.nexusmods.com/palworld/mods/336"

    panel._copy_mod_info()
    payload = copied[-1]
    assert "名称:\nPal Analyzer" in payload
    assert "ID:\n336" in payload
    assert "main.zip" in payload
    assert "optional_hat.zip" in payload
