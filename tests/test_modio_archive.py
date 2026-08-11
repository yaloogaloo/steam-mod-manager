"""mod.io offline webpage archive (Playwright SPA render)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    PLATFORM_MODIO,
    PLATFORM_STEAM,
    PROVIDER_MODIO_ARCHIVE,
    PROVIDER_STEAM_ARCHIVE,
)
from core.models import ModMetadata
from services import archive as archive_mod
from services.archive import OfflinePageArchiver, normalize_page_url
from services.file_ops import INFO_DIR_NAME
from services.offline.manager import OfflineManager
from services.offline.modio import ModioOfflineProvider
from services.offline.modio_browser_snapshot import (
    looks_like_rendered_modio_page,
    validate_modio_dom,
)
from services.offline.steam import SteamOfflineProvider


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "modio_archive.db")
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


def test_normalize_page_url_strips_fragment() -> None:
    assert (
        normalize_page_url(
            "https://mod.io/g/anno-1800/m/bigger-harbour#description"
        )
        == "https://mod.io/g/anno-1800/m/bigger-harbour"
    )
    assert (
        normalize_page_url("https://mod.io/g/anno-1800/m/bigger-harbour")
        == "https://mod.io/g/anno-1800/m/bigger-harbour"
    )
    assert normalize_page_url("") == ""


def _ok_bytes(payload: bytes, *, content_type: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Type": content_type}
    resp.raise_for_status = MagicMock()
    resp.iter_content = MagicMock(return_value=[payload])
    resp.close = MagicMock()
    resp.content = payload
    return resp


_RENDERED_MODIO_HTML = """<!DOCTYPE html>
<html><head>
<title>Bigger Harbour [Spice It Up] for Anno 1800 - mod.io</title>
<link rel="stylesheet" href="https://cdn.example/modio.css">
</head><body>
<div id="root">
  <h1>Bigger Harbour [Spice It Up]</h1>
  <section class="description">
    <h2>Description</h2>
    <p>Increases the harbor area.</p>
  </section>
  <img src="https://cdn.example/cover.png" alt="cover">
  <button>Subscribe</button>
</div>
</body></html>
"""


def test_modio_archive_rendered_html_contains_title_and_description(
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rendered Playwright HTML must keep page title + mod description content."""
    info = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="bigger-harbour",
        source_url="https://mod.io/g/anno-1800/m/bigger-harbour#description",
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

    captured_urls: list[str] = []

    def fake_capture(url: str) -> str:
        captured_urls.append(url)
        return _RENDERED_MODIO_HTML

    def fake_asset_get(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        if url.endswith(".css"):
            return _ok_bytes(b"body{color:#111}", content_type="text/css")
        return _ok_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, content_type="image/png")

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", fake_asset_get)

    # Guard: curl_cffi HTML path must not be used for mod.io anymore.
    def boom_get(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        raise AssertionError(f"mod.io must not curl_cffi fetch HTML: {url}")

    monkeypatch.setattr(OfflinePageArchiver, "_perform_get", boom_get)

    provider = ModioOfflineProvider(capture_func=fake_capture)
    result = provider.update_offline_page(mid, managed_path=folder, library_root=lib)

    index = folder / INFO_DIR_NAME / "offline" / "index.html"
    assert result.index_path == index
    assert index.is_file()
    text = index.read_text(encoding="utf-8")
    assert "Bigger Harbour" in text
    assert "Increases the harbor area." in text or "Description" in text
    assert "./assets/" in text
    assert (folder / INFO_DIR_NAME / "offline" / "assets").is_dir()
    assert list((folder / INFO_DIR_NAME / "offline" / "assets").iterdir())
    assert captured_urls == ["https://mod.io/g/anno-1800/m/bigger-harbour"]
    assert result.status == OFFLINE_STATUS_ARCHIVED
    assert result.provider == PROVIDER_MODIO_ARCHIVE
    assert looks_like_rendered_modio_page(text)
    assert validate_modio_dom(_RENDERED_MODIO_HTML) is None


def test_empty_spa_shell_rejected() -> None:
    shell = (
        "<!DOCTYPE html><html><head><title>mod.io</title></head>"
        '<body><div id="root"></div></body></html>'
    )
    assert validate_modio_dom(shell) == "UNRENDERED_SPA_SHELL"


def test_manager_routes_modio_without_affecting_steam(
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "library"
    lib.mkdir()

    steam_folder = lib / "Game" / "SteamMod"
    steam_info = steam_folder / INFO_DIR_NAME
    steam_info.mkdir(parents=True)
    (steam_info / "mod.json").write_text(
        json.dumps({"published_file_id": "111", "title": "SteamMod"}),
        encoding="utf-8",
    )

    db.upsert_mod(
        ModMetadata(
            published_file_id="111",
            title="SteamMod",
            managed_path=str(steam_folder),
        )
    )
    db.update_mod_platform_info("111", platform=PLATFORM_STEAM, external_id="111")

    modio = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="bigger-harbour",
        source_url="https://mod.io/g/anno-1800/m/bigger-harbour",
        title="BH",
        app_id=916440,
        game_name="Anno 1800",
    )
    modio_folder = lib / "Anno 1800" / "BH"
    (modio_folder / INFO_DIR_NAME).mkdir(parents=True)

    steam_calls: list[str] = []

    def tracking_ensure(self: Any, info_dir: Any, published_file_id: Any, **kwargs: Any) -> Path:
        steam_calls.append(str(published_file_id))
        path = Path(info_dir) / "index.html"
        path.write_text(
            '<html><div id="smm-offline-banner">ok</div></html>',
            encoding="utf-8",
        )
        return path

    monkeypatch.setattr(OfflinePageArchiver, "ensure_offline_page", tracking_ensure)

    def fake_capture(url: str) -> str:
        return _RENDERED_MODIO_HTML

    def fake_asset_get(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
        if url.endswith(".css"):
            return _ok_bytes(b"body{}", content_type="text/css")
        return _ok_bytes(b"\x89PNG\r\n", content_type="image/png")

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", fake_asset_get)

    mgr = OfflineManager(
        library_root=lib,
        providers=(
            SteamOfflineProvider(),
            ModioOfflineProvider(capture_func=fake_capture),
        ),
    )
    assert isinstance(mgr.get_provider_for_platform(PLATFORM_STEAM), SteamOfflineProvider)
    assert isinstance(mgr.get_provider_for_platform(PLATFORM_MODIO), ModioOfflineProvider)

    steam_result = mgr.update_mod_offline("111", managed_path=steam_folder)
    assert steam_calls == ["111"]
    assert steam_result.provider == PROVIDER_STEAM_ARCHIVE

    modio_result = mgr.update_mod_offline(
        modio.mod_id, managed_path=modio_folder, platform=PLATFORM_MODIO
    )
    assert modio_result.provider == PROVIDER_MODIO_ARCHIVE
    text = (modio_folder / INFO_DIR_NAME / "offline" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "Bigger Harbour" in text
    assert "Increases the harbor area." in text
    assert steam_calls == ["111"]
