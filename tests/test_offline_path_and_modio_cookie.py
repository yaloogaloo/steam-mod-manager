"""Offline page path resolution + mod.io cookie-banner cleanup."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services import archive as archive_mod
from services.archive import OfflinePageArchiver
from services.file_ops import INFO_DIR_NAME
from services.offline.modio import ModioOfflineProvider
from services.offline.modio_browser_snapshot import strip_modio_cookie_banner
from services.offline.paths import resolve_offline_page_path
from ui.mod_detail_panel import ModDetailPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "offline_path.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def _isolate_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(archive_mod, "_get_archive_proxy", lambda: None)
    monkeypatch.setattr(archive_mod, "_get_steam_cookie", lambda: None)
    cache_root = tmp_path / "asset_cache"
    cache_root.mkdir(exist_ok=True)
    monkeypatch.setattr(archive_mod, "asset_cache_dir", lambda: cache_root)
    archive_mod.reset_asset_cache_stats()


def _seed_folder(lib: Path, *, mid: str, title: str) -> Path:
    folder = lib / "Game" / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps({"published_file_id": mid, "title": title}),
        encoding="utf-8",
    )
    return folder


def test_resolve_prefers_modio_offline_index(tmp_path: Path) -> None:
    folder = _seed_folder(tmp_path / "lib", mid="91001", title="ModioMod")
    steam_legacy = folder / INFO_DIR_NAME / "index.html"
    steam_legacy.write_text("<html>steam legacy</html>", encoding="utf-8")
    preferred = folder / INFO_DIR_NAME / "offline" / "index.html"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("<html>modio offline</html>", encoding="utf-8")

    # Stale metadata still points at Steam layout — preferred path must win.
    resolved = resolve_offline_page_path(
        folder, offline_page_path=".info/index.html"
    )
    assert resolved == preferred.resolve()


def test_resolve_falls_back_to_steam_index(tmp_path: Path) -> None:
    folder = _seed_folder(tmp_path / "lib", mid="91002", title="SteamMod")
    steam = folder / INFO_DIR_NAME / "index.html"
    steam.write_text("<html>steam only</html>", encoding="utf-8")

    resolved = resolve_offline_page_path(folder)
    assert resolved == steam.resolve()


def test_open_offline_opens_modio_offline_index(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    folder = _seed_folder(tmp_path / "lib", mid="91003", title="BiggerHarbour")
    legacy = folder / INFO_DIR_NAME / "index.html"
    legacy.write_text("<html>wrong</html>", encoding="utf-8")
    preferred = folder / INFO_DIR_NAME / "offline" / "index.html"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("<html>modio</html>", encoding="utf-8")

    db.upsert_mod(
        ModMetadata(
            published_file_id="91003",
            title="BiggerHarbour",
            offline_page_path=".info/index.html",
            managed_path=str(folder),
        )
    )
    panel = ModDetailPanel()
    panel.show_mod(folder)
    panel._metadata.offline_page_path = ".info/index.html"

    opened: list = []
    monkeypatch.setattr(
        "ui.mod_detail_panel.QDesktopServices.openUrl",
        lambda url: opened.append(url) or True,
    )
    panel._open_offline()
    assert len(opened) == 1
    assert Path(opened[0].toLocalFile()).resolve() == preferred.resolve()


def test_strip_modio_cookie_banner_removes_notice() -> None:
    html = """<!DOCTYPE html><html><body>
    <h1>Bigger Harbour</h1>
    <section class="description"><p>Harbor size boost.</p></section>
    <img src="cover.png" alt="cover">
    <div class="tw-fixed sm:tw-w-100 tw-bottom-3 tw-inset-x-3 md:tw-left-21 tw-z-10">
      <div class="tw-border-2 tw-rounded tw-border-primary tw-p-4">
        <p>mod.io uses essential cookies to make our site work. With your consent,
        we may also use non-essential cookies.</p>
        <button>Accept all</button>
      </div>
    </div>
    </body></html>"""
    cleaned = strip_modio_cookie_banner(html)
    assert "essential cookies" not in cleaned.lower()
    assert "Bigger Harbour" in cleaned
    assert "Harbor size boost" in cleaned
    assert 'src="cover.png"' in cleaned or "cover.png" in cleaned


def test_modio_archive_saves_without_cookie_banner(
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.mod_platform import PLATFORM_MODIO

    info = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="bigger-harbour",
        source_url="https://mod.io/g/anno-1800/m/bigger-harbour",
        title="Bigger Harbour",
        app_id=916440,
        game_name="Anno 1800",
    )
    mid = info.mod_id
    lib = tmp_path / "library"
    folder = lib / "Anno 1800" / "Bigger Harbour"
    info_dir = folder / INFO_DIR_NAME
    info_dir.mkdir(parents=True)
    (info_dir / "mod.json").write_text(
        json.dumps({"published_file_id": mid, "title": "Bigger Harbour"}),
        encoding="utf-8",
    )

    html = """<!DOCTYPE html><html><head>
    <title>Bigger Harbour - mod.io</title>
    <link rel="stylesheet" href="https://cdn.example/modio.css">
    </head><body>
    <div id="root">
      <h1>Bigger Harbour</h1>
      <section class="description"><p>Harbor size boost.</p></section>
      <button>Subscribe</button>
    </div>
    <div class="tw-fixed tw-bottom-3 tw-z-10">
      <p>mod.io uses essential cookies to make our site work.</p>
    </div>
    </body></html>"""

    def fake_capture(url: str) -> str:
        return html

    def fake_asset_get(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"Content-Type": "text/css"}
        resp.raise_for_status = MagicMock()
        resp.iter_content = MagicMock(return_value=[b"body{}"])
        resp.close = MagicMock()
        resp.content = b"body{}"
        return resp

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", fake_asset_get)
    monkeypatch.setattr(
        OfflinePageArchiver,
        "_perform_get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no html fetch")),
    )

    provider = ModioOfflineProvider(capture_func=fake_capture)
    result = provider.update_offline_page(mid, managed_path=folder, library_root=lib)
    index = folder / INFO_DIR_NAME / "offline" / "index.html"
    assert result.index_path == index
    text = index.read_text(encoding="utf-8")
    assert "essential cookies" not in text.lower()
    assert "Bigger Harbour" in text
    assert "Harbor size boost" in text
