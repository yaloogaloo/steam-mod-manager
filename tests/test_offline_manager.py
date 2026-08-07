"""OfflineManager routes by platform."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    PROVIDER_GITHUB_SNAPSHOT,
    PROVIDER_NEXUS_SNAPSHOT,
    PROVIDER_STEAM_ARCHIVE,
)
from core.models import ModMetadata
from services.archive import OfflinePageArchiver
from services.file_ops import INFO_DIR_NAME
from services.offline.manager import OfflineManager
from services.offline.snapshot import SnapshotResult, WebSnapshotDownloader


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "offline_mgr.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(lib: Path, *, mid: str, title: str, game: str = "Game") -> Path:
    folder = lib / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps({"published_file_id": mid, "title": title, "game_name": game}),
        encoding="utf-8",
    )
    return folder


def test_manager_selects_steam_nexus_github(
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "library"
    lib.mkdir()

    steam_folder = _seed(lib, mid="111", title="S")
    db.upsert_mod(ModMetadata(published_file_id="111", title="S", managed_path=str(steam_folder)))
    db.update_mod_platform_info("111", platform=PLATFORM_STEAM, external_id="111")

    nexus = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="336",
        source_url="https://www.nexusmods.com/palworld/mods/336",
        title="N",
        app_id=1623730,
        game_name="Palworld",
    )
    nexus_folder = _seed(lib, mid=nexus.mod_id, title="N", game="Palworld")

    github = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="o/r",
        source_url="https://github.com/o/r",
        title="G",
        app_id=1623730,
        game_name="Palworld",
    )
    github_folder = _seed(lib, mid=github.mod_id, title="G", game="Palworld")

    def tracking_ensure(self, info_dir, published_file_id, **kwargs):
        path = Path(info_dir) / "index.html"
        path.write_text(
            '<html><div id="smm-offline-banner">ok</div></html>',
            encoding="utf-8",
        )
        return path

    def fake_download(self, url: str, output_dir: Path):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        index = out / "index.html"
        index.write_text(f"<html>{url}</html>", encoding="utf-8")
        return SnapshotResult(success=True, html_path=index, asset_count=0)

    monkeypatch.setattr(OfflinePageArchiver, "ensure_offline_page", tracking_ensure)
    monkeypatch.setattr(WebSnapshotDownloader, "download", fake_download)

    mgr = OfflineManager(db=db, library_root=lib)

    assert mgr.get_provider_for_platform(PLATFORM_STEAM).get_provider_name() == (
        PROVIDER_STEAM_ARCHIVE
    )
    assert mgr.get_provider_for_platform(PLATFORM_NEXUS).get_provider_name() == (
        PROVIDER_NEXUS_SNAPSHOT
    )
    assert mgr.get_provider_for_platform(PLATFORM_GITHUB).get_provider_name() == (
        PROVIDER_GITHUB_SNAPSHOT
    )

    r1 = mgr.update_mod_offline("111", managed_path=steam_folder)
    r2 = mgr.update_mod_offline(nexus.mod_id, managed_path=nexus_folder)
    r3 = mgr.update_mod_offline(github.mod_id, managed_path=github_folder)

    assert r1.provider == PROVIDER_STEAM_ARCHIVE
    assert r2.provider == PROVIDER_NEXUS_SNAPSHOT
    assert r3.provider == PROVIDER_GITHUB_SNAPSHOT
    assert all(r.index_path.is_file() for r in (r1, r2, r3))
    assert "336" in r2.index_path.read_text(encoding="utf-8")
    assert "o/r" in r3.index_path.read_text(encoding="utf-8")
