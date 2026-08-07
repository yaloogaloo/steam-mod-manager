"""Nexus offline webpage snapshot."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    PLATFORM_NEXUS,
    PROVIDER_NEXUS_SNAPSHOT,
)
from services.file_ops import INFO_DIR_NAME
from services.offline.nexus import NexusOfflineProvider
from services.offline.snapshot import SnapshotResult, WebSnapshotDownloader


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "nexus_offline.db")
    yield manager
    DatabaseManager.reset_instance()


def test_nexus_snapshots_source_url(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="336",
        source_url="https://www.nexusmods.com/palworld/mods/336",
        title="Cool Nexus Mod",
        app_id=1623730,
        game_name="Palworld",
    )
    mid = info.mod_id

    lib = tmp_path / "library"
    folder = lib / "Palworld" / "Cool Nexus Mod"
    info_dir = folder / INFO_DIR_NAME
    info_dir.mkdir(parents=True)
    (info_dir / "mod.json").write_text(
        json.dumps({"published_file_id": mid, "title": "Cool Nexus Mod"}),
        encoding="utf-8",
    )

    def fake_download(self, url: str, output_dir: Path):
        assert url == "https://www.nexusmods.com/palworld/mods/336"
        out = Path(output_dir)
        assets = out / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "main.css").write_text("body{}", encoding="utf-8")
        index = out / "index.html"
        index.write_text(
            '<html><link href="assets/main.css"><h1>Nexus Real Page</h1></html>',
            encoding="utf-8",
        )
        return SnapshotResult(success=True, html_path=index, asset_count=1)

    monkeypatch.setattr(WebSnapshotDownloader, "download", fake_download)

    result = NexusOfflineProvider().update_offline_page(
        mid, managed_path=folder, library_root=lib
    )

    assert result.index_path == info_dir / "offline" / "index.html"
    assert result.index_path.is_file()
    html = result.index_path.read_text(encoding="utf-8")
    assert "Nexus Real Page" in html
    assert 'href="assets/main.css"' in html
    assert (info_dir / "offline" / "assets" / "main.css").is_file()
    assert result.status == OFFLINE_STATUS_ARCHIVED
    assert result.provider == PROVIDER_NEXUS_SNAPSHOT

    refreshed = db.get_mod_display_info(mid)
    assert refreshed is not None
    assert refreshed.offline_status == OFFLINE_STATUS_ARCHIVED
    assert refreshed.offline_provider == PROVIDER_NEXUS_SNAPSHOT
