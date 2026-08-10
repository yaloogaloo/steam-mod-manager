"""Nexus offline pages via manual browser HTML import."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    PROVIDER_GITHUB_SNAPSHOT,
    PROVIDER_NEXUS_MANUAL_IMPORT,
    PROVIDER_STEAM_ARCHIVE,
)
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME
from services.offline.github import GithubOfflineProvider
from services.offline.html_rewriter import companion_files_dir, rewrite_imported_html
from services.offline.manager import OfflineManager
from services.offline.nexus_manual import NexusManualOfflineProvider, store_snapshot
from services.offline.steam import SteamOfflineProvider


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "nexus_manual.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(lib: Path, *, mid: str, title: str, game: str = "Palworld") -> Path:
    folder = lib / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps({"published_file_id": mid, "title": title, "game_name": game}),
        encoding="utf-8",
    )
    return folder


def test_html_import_writes_offline_index(tmp_path: Path, db: DatabaseManager) -> None:
    lib = tmp_path / "library"
    lib.mkdir()
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="336",
        source_url="https://www.nexusmods.com/palworld/mods/336",
        title="Cool Nexus Mod",
        app_id=1623730,
        game_name="Palworld",
    )
    folder = _seed(lib, mid=info.mod_id, title="Cool Nexus Mod")
    html = tmp_path / "test.html"
    html.write_text(
        "<!DOCTYPE html><html><head><title>T</title></head>"
        "<body><h1>Imported Nexus</h1></body></html>",
        encoding="utf-8",
    )

    result = NexusManualOfflineProvider().import_offline_page(
        info.mod_id,
        html,
        managed_path=folder,
        library_root=lib,
    )

    assert result.status == OFFLINE_STATUS_ARCHIVED
    assert result.provider == PROVIDER_NEXUS_MANUAL_IMPORT
    assert result.index_path == folder / INFO_DIR_NAME / "offline" / "index.html"
    assert result.index_path.is_file()
    assert "Imported Nexus" in result.index_path.read_text(encoding="utf-8")

    meta = json.loads(
        (folder / INFO_DIR_NAME / "offline" / "metadata.json").read_text(encoding="utf-8")
    )
    assert meta["provider"] == PROVIDER_NEXUS_MANUAL_IMPORT
    assert meta["source"] == "manual_browser_save"
    assert "nexusmods.com" in meta["original_url"]

    row = db.get_mod_display_info(info.mod_id)
    assert row is not None
    assert row.offline_status == OFFLINE_STATUS_ARCHIVED
    assert row.offline_provider == PROVIDER_NEXUS_MANUAL_IMPORT


def test_browser_save_directory_copies_assets(tmp_path: Path) -> None:
    page = tmp_path / "PalAnalyzer.html"
    files = tmp_path / "PalAnalyzer_files"
    files.mkdir()
    css = files / "style.css"
    css.write_text("body{color:red}", encoding="utf-8")
    img = files / "cover.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    page.write_text(
        "<!DOCTYPE html><html><head>"
        '<link rel="stylesheet" href="PalAnalyzer_files/style.css">'
        "</head><body>"
        '<img src="PalAnalyzer_files/cover.png" alt="c">'
        "<p>ok</p></body></html>",
        encoding="utf-8",
    )

    assert companion_files_dir(page) == files

    out = tmp_path / "offline"
    index, count = store_snapshot(page, out, source_url="https://www.nexusmods.com/x/mods/1")
    assert index.is_file()
    assert count >= 2
    # Companion folder is imported; refs rewrite into typed asset dirs when resolved.
    assert (out / "assets" / "files" / "style.css").is_file()
    assert (out / "assets" / "files" / "cover.png").is_file()
    html = index.read_text(encoding="utf-8")
    assert "assets/" in html
    assert "style.css" in html
    assert "cover.png" in html
    assert "PalAnalyzer_files/" not in html


def test_file_url_rewrite_when_asset_exists(tmp_path: Path) -> None:
    css = tmp_path / "local.css"
    css.write_text(".x{}", encoding="utf-8")
    page = tmp_path / "page.html"
    # Use a path that resolve can find (POSIX-style on Windows still works via Path)
    href = css.resolve().as_uri()
    page.write_text(
        f'<!DOCTYPE html><html><head><link href="{href}" rel="stylesheet"></head>'
        "<body>hi</body></html>",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    out.mkdir()
    rewritten, n = rewrite_imported_html(
        page.read_text(encoding="utf-8"),
        html_path=page,
        output_dir=out,
    )
    assert n >= 1
    assert "./assets/css/" in rewritten
    assert "file://" not in rewritten or "file://" not in rewritten.lower().split("link")[1][:80]


def test_provider_status_via_manager(tmp_path: Path, db: DatabaseManager) -> None:
    lib = tmp_path / "library"
    lib.mkdir()
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="99",
        source_url="https://www.nexusmods.com/palworld/mods/99",
        title="M",
        app_id=1623730,
        game_name="Palworld",
    )
    folder = _seed(lib, mid=info.mod_id, title="M")
    html = tmp_path / "page.htm"
    html.write_text("<html><body>manual</body></html>", encoding="utf-8")

    mgr = OfflineManager(db=db, library_root=lib)
    assert mgr.get_provider_for_platform(PLATFORM_NEXUS).get_provider_name() == (
        PROVIDER_NEXUS_MANUAL_IMPORT
    )

    with pytest.raises(RuntimeError, match="手动导入"):
        mgr.update_mod_offline(info.mod_id, managed_path=folder)

    result = mgr.import_mod_offline_html(
        info.mod_id, html, managed_path=folder, platform=PLATFORM_NEXUS
    )
    assert result.provider == PROVIDER_NEXUS_MANUAL_IMPORT
    assert result.status == OFFLINE_STATUS_ARCHIVED

    row = db.get_mod_display_info(info.mod_id)
    assert row is not None
    assert row.offline_provider == PROVIDER_NEXUS_MANUAL_IMPORT
    assert row.offline_status == OFFLINE_STATUS_ARCHIVED


def test_steam_provider_unchanged() -> None:
    assert SteamOfflineProvider().get_provider_name() == PROVIDER_STEAM_ARCHIVE
    assert isinstance(SteamOfflineProvider(), type(SteamOfflineProvider()))


def test_github_provider_unchanged() -> None:
    assert GithubOfflineProvider().get_provider_name() == PROVIDER_GITHUB_SNAPSHOT


def test_steam_and_github_still_routed(
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.archive import OfflinePageArchiver
    from services.offline.layout_snapshot import LayoutSnapshotResult

    lib = tmp_path / "library"
    lib.mkdir()

    steam_folder = _seed(lib, mid="111", title="S", game="Game")
    db.upsert_mod(
        ModMetadata(published_file_id="111", title="S", managed_path=str(steam_folder))
    )
    db.update_mod_platform_info("111", platform=PLATFORM_STEAM, external_id="111")

    github = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="o/r",
        source_url="https://github.com/o/r",
        title="G",
        app_id=1623730,
        game_name="Palworld",
    )
    github_folder = _seed(lib, mid=github.mod_id, title="G")

    def tracking_ensure(self, info_dir, published_file_id, **kwargs):
        path = Path(info_dir) / "index.html"
        path.write_text('<html><div id="smm-offline-banner">ok</div></html>', encoding="utf-8")
        return path

    monkeypatch.setattr(OfflinePageArchiver, "ensure_offline_page", tracking_ensure)

    class FakeLayout:
        def snapshot(self, url: str, output_dir: Path | str) -> LayoutSnapshotResult:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            index = out / "index.html"
            index.write_text(f"<html>{url}</html>", encoding="utf-8")
            return LayoutSnapshotResult(success=True, html_path=index, backend="layout")

    mgr = OfflineManager(
        db=db,
        library_root=lib,
        providers=(
            SteamOfflineProvider(),
            NexusManualOfflineProvider(),
            GithubOfflineProvider(layout_provider=FakeLayout()),
        ),
    )
    r1 = mgr.update_mod_offline("111", managed_path=steam_folder)
    r3 = mgr.update_mod_offline(github.mod_id, managed_path=github_folder)
    assert r1.provider == PROVIDER_STEAM_ARCHIVE
    assert r3.provider == PROVIDER_GITHUB_SNAPSHOT
    assert "smm-offline-banner" in r1.index_path.read_text(encoding="utf-8")
    assert "o/r" in r3.index_path.read_text(encoding="utf-8")
