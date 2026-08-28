"""Detail panel — per-Mod offline page download action."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox

from core.db_manager import PLATFORM_GITHUB, PLATFORM_NEXUS, PLATFORM_STEAM, DatabaseManager
from core.mod_platform import PLATFORM_MODIO, PLATFORM_OTHER
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
    assert "保存离线页面" in (panel.btn_download_offline.toolTip() or "")
    assert panel.btn_offline.text() == "打开离线页面"
    assert "离线" in (panel.btn_offline.toolTip() or "")
    assert panel.btn_steam.text() == "打开官网"
    assert "来源" in (panel.btn_steam.toolTip() or "") or "官网" in (
        panel.btn_steam.toolTip() or ""
    )

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

    def fake_attach(mod_id, html_path, **kwargs):
        calls.append("nexus")
        path = Path(kwargs["managed_path"]) / INFO_DIR_NAME / "offline" / "index.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<html>nexus</html>", encoding="utf-8")
        return OfflineUpdateResult(
            mod_id=str(mod_id),
            index_path=path,
            status="archived",
            provider="nexus_manual_import",
        )

    monkeypatch.setattr("ui.offline_archive_thread.attach_nexus_offline_page", fake_attach)

    chosen = tmp_path / "saved.html"
    chosen.write_text("<html>ok</html>", encoding="utf-8")
    monkeypatch.setattr(
        "ui.mod_detail_panel.QFileDialog.getOpenFileName",
        lambda *a, **k: (str(chosen), "HTML"),
    )

    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()
    assert "导入离线页面" in (panel.btn_download_offline.toolTip() or "")

    panel._download_offline_page()
    for _ in range(50):
        qapp.processEvents()
        if panel._offline_worker is None or not panel._offline_worker.isRunning():
            break
        import time

        time.sleep(0.02)

    assert calls == ["nexus"]
    assert panel._has_offline_page()


def test_other_platform_uses_manual_html_import(
    qapp: QApplication,
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "library"
    lib.mkdir()
    mid = "99001"
    folder = _seed_mod(lib, mid=mid, title="LocalMod")
    from core.game_info import GameInfo

    db.upsert_game(
        GameInfo(app_id=1623730, name="Palworld", header_image="", short_description="")
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id=mid,
            title="LocalMod",
            app_id=1623730,
            managed_path=str(folder),
        )
    )
    db.batch_update_platform([mid], PLATFORM_OTHER)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    calls: list[str] = []

    def fake_attach(mod_id, html_path, **kwargs):
        calls.append("other")
        path = Path(kwargs["managed_path"]) / INFO_DIR_NAME / "offline" / "index.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("<html>other</html>", encoding="utf-8")
        return OfflineUpdateResult(
            mod_id=str(mod_id),
            index_path=path,
            status="archived",
            provider="nexus_manual_import",
        )

    monkeypatch.setattr("ui.offline_archive_thread.attach_nexus_offline_page", fake_attach)

    chosen = tmp_path / "saved.mhtml"
    chosen.write_text("From: <saved>\n", encoding="utf-8")
    monkeypatch.setattr(
        "ui.mod_detail_panel.QFileDialog.getOpenFileName",
        lambda *a, **k: (str(chosen), "MHTML"),
    )

    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()
    assert panel.btn_download_offline.text() == "导入离线页面"

    panel._download_offline_page()
    for _ in range(50):
        qapp.processEvents()
        if panel._offline_worker is None or not panel._offline_worker.isRunning():
            break
        import time

        time.sleep(0.02)

    assert calls == ["other"]
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


def test_modio_button_runs_full_ui_chain(
    qapp: QApplication,
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detail panel click → worker → manager → mod.io provider (capture mocked)."""
    lib = tmp_path / "library"
    url = "https://mod.io/g/baldursgate3/m/weightless-consumables"
    folder = lib / "Game" / "Weightless Consumables"
    info_dir = folder / INFO_DIR_NAME
    info_dir.mkdir(parents=True)
    info = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="weightless-consumables",
        source_url=url,
        title="Weightless Consumables",
        app_id=1086940,
        game_name="Baldur's Gate 3",
    )
    mid = str(info.mod_id)
    import json
    import zipfile

    (info_dir / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": "Weightless Consumables",
                "url": url,
                "source_url": url,
                "source_type": "modio",
                "modio_name_id": "weightless-consumables",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(folder / "payload.zip", "w") as zf:
        zf.writestr("mod.txt", "x")
    db.update_mod_identity_fields(mid, last_known_path=str(folder.resolve()))

    captured_urls: list[str] = []
    html = (
        "<!DOCTYPE html><html><head><title>Weightless Consumables - mod.io</title></head>"
        '<body><div id="root"><h1>Weightless Consumables</h1>'
        '<section class="description"><p>Lightweight items.</p></section>'
        "<button>Subscribe</button></div></body></html>"
    )

    from services.offline.modio import ModioOfflineProvider

    monkeypatch.setattr(
        "services.offline.manager.ModioOfflineProvider",
        lambda *a, **k: ModioOfflineProvider(
            capture_func=lambda u: captured_urls.append(u) or html
        ),
    )
    from services import archive as archive_mod

    monkeypatch.setattr(archive_mod, "_get_archive_proxy", lambda: None)
    monkeypatch.setattr(archive_mod, "_get_steam_cookie", lambda: None)
    cache_root = tmp_path / "asset_cache"
    cache_root.mkdir(exist_ok=True)
    monkeypatch.setattr(archive_mod, "asset_cache_dir", lambda: cache_root)
    archive_mod.reset_asset_cache_stats()

    def fake_asset_get(self, asset_url, kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "text/css"}
        resp.raise_for_status = MagicMock()
        resp.iter_content = MagicMock(return_value=[b"body{}"])
        resp.close = MagicMock()
        resp.content = b"body{}"
        return resp

    from services.archive import OfflinePageArchiver

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", fake_asset_get)
    monkeypatch.setattr(
        OfflinePageArchiver,
        "_perform_get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no html fetch")),
    )
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid)
    qapp.processEvents()
    assert panel._current_platform == PLATFORM_MODIO
    assert "保存离线页面" in (panel.btn_download_offline.toolTip() or "")

    panel._download_offline_page()
    for _ in range(80):
        qapp.processEvents()
        if panel._offline_worker is None or not panel._offline_worker.isRunning():
            break
        import time

        time.sleep(0.02)

    assert captured_urls == [url]
    assert panel._has_offline_page()
    index = folder / INFO_DIR_NAME / "offline" / "index.html"
    assert index.is_file()
    assert "Weightless Consumables" in index.read_text(encoding="utf-8")
