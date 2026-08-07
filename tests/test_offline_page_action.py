"""Detail panel — per-Mod offline page download action."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox

from core.db_manager import PLATFORM_GITHUB, PLATFORM_NEXUS, PLATFORM_STEAM, DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, ModFileManager
from services.offline.base import OfflineUpdateResult
from ui.mod_detail_panel import ModDetailPanel
from ui.offline_archive_thread import OfflineArchiveWorker


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "offline_action.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed_mod(lib: Path, *, mid: str, title: str) -> Path:
    folder = lib / "Game" / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": title,
                "game_name": "Game",
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_steam_button_starts_worker_and_calls_manager(
    qapp: QApplication,
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "library"
    lib.mkdir()
    folder = _seed_mod(lib, mid="3761838546", title="SteamMod")
    db.upsert_mod(
        ModMetadata(published_file_id="3761838546", title="SteamMod", managed_path=str(folder))
    )
    db.update_mod_platform_info(
        "3761838546",
        platform=PLATFORM_STEAM,
        source_url="https://steamcommunity.com/sharedfiles/filedetails/?id=3761838546",
        external_id="3761838546",
    )

    calls: list[str] = []

    class FakeManager:
        def __init__(self, *a, **k):
            pass

        def update_mod_offline(self, mod_id, **kwargs):
            calls.append(str(mod_id))
            path = Path(kwargs["managed_path"]) / INFO_DIR_NAME / "index.html"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<html>ok</html>", encoding="utf-8")
            return OfflineUpdateResult(
                mod_id=str(mod_id),
                index_path=path,
                status="archived",
                provider="steam_archive",
            )

    monkeypatch.setattr("ui.offline_archive_thread.OfflineManager", FakeManager)

    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()
    assert panel.btn_download_offline.text() == "下载 Steam 页面"
    assert panel.btn_offline.text() == "打开离线页面"
    assert panel.btn_steam.text() == "原链接"

    updated: list = []
    panel.offline_page_updated.connect(lambda p: updated.append(p))

    panel._download_offline_page()
    for _ in range(50):
        qapp.processEvents()
        if panel._offline_worker is None or not panel._offline_worker.isRunning():
            break
        import time

        time.sleep(0.02)

    assert calls == ["3761838546"]
    assert panel._has_offline_page()
    assert updated


def test_non_steam_uses_manager_and_button_label(
    qapp: QApplication,
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "library"
    lib.mkdir()
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="42",
        source_url="https://www.nexusmods.com/x/mods/42",
        title="NexusMod",
        app_id=1623730,
        game_name="Palworld",
    )
    folder = _seed_mod(lib, mid=str(info.mod_id), title="NexusMod")

    calls: list[str] = []

    class FakeManager:
        def __init__(self, *a, **k):
            pass

        def update_mod_offline(self, mod_id, **kwargs):
            calls.append("nexus")
            path = Path(kwargs["managed_path"]) / INFO_DIR_NAME / "index.html"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<html>nexus</html>", encoding="utf-8")
            return OfflineUpdateResult(
                mod_id=str(mod_id),
                index_path=path,
                status="archived",
                provider="nexus_snapshot",
            )

    monkeypatch.setattr("ui.offline_archive_thread.OfflineManager", FakeManager)

    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()
    assert panel.btn_download_offline.text() == "保存 Nexus 页面"

    panel._download_offline_page()
    for _ in range(50):
        qapp.processEvents()
        if panel._offline_worker is None or not panel._offline_worker.isRunning():
            break
        import time

        time.sleep(0.02)

    assert calls == ["nexus"]
    assert panel._has_offline_page()


def test_worker_routes_platforms_via_manager(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "mod"
    (folder / INFO_DIR_NAME).mkdir(parents=True)
    seen: list[str] = []

    class FakeManager:
        def __init__(self, *a, **k):
            pass

        def update_mod_offline(self, mod_id, **kwargs):
            seen.append(kwargs.get("platform") or "")
            p = Path(kwargs["managed_path"]) / INFO_DIR_NAME / "index.html"
            p.write_text("x", encoding="utf-8")
            return OfflineUpdateResult(
                mod_id=str(mod_id),
                index_path=p,
                status="generated",
                provider="x",
            )

    monkeypatch.setattr("ui.offline_archive_thread.OfflineManager", FakeManager)

    w1 = OfflineArchiveWorker(folder, platform=PLATFORM_STEAM, published_file_id="1")
    w1.run()
    w2 = OfflineArchiveWorker(folder, platform=PLATFORM_GITHUB, published_file_id="2")
    w2.run()
    assert seen == [PLATFORM_STEAM, PLATFORM_GITHUB]
