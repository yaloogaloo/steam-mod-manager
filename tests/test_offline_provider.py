"""OfflineProvider routing — Steam archive / Nexus+GitHub snapshot ids."""

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
    PROVIDER_NEXUS_SNAPSHOT,
    PROVIDER_STEAM_ARCHIVE,
)
from core.models import ModMetadata
from services.archive import OfflinePageArchiver
from services.file_ops import INFO_DIR_NAME
from services.offline.base import OfflineProvider
from services.offline.github import GithubOfflineProvider
from services.offline.manager import OfflineManager
from services.offline.nexus import NexusOfflineProvider
from services.offline.snapshot import SnapshotResult, WebSnapshotDownloader
from services.offline.steam import SteamOfflineProvider


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "offline_provider.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(lib: Path, *, mid: str, title: str) -> Path:
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


def test_steam_provider_is_offline_provider() -> None:
    assert isinstance(SteamOfflineProvider(), OfflineProvider)
    assert SteamOfflineProvider().get_provider_name() == PROVIDER_STEAM_ARCHIVE


def test_provider_names_by_platform() -> None:
    assert SteamOfflineProvider().get_provider_name() == PROVIDER_STEAM_ARCHIVE
    assert NexusOfflineProvider().get_provider_name() == PROVIDER_NEXUS_SNAPSHOT
    assert GithubOfflineProvider().get_provider_name() == PROVIDER_GITHUB_SNAPSHOT

    mgr = OfflineManager(providers=None)
    assert mgr.get_provider_for_platform(PLATFORM_STEAM).get_provider_name() == (
        PROVIDER_STEAM_ARCHIVE
    )
    assert mgr.get_provider_for_platform(PLATFORM_NEXUS).get_provider_name() == (
        PROVIDER_NEXUS_SNAPSHOT
    )
    assert mgr.get_provider_for_platform(PLATFORM_GITHUB).get_provider_name() == (
        PROVIDER_GITHUB_SNAPSHOT
    )


def test_steam_provider_calls_ensure_offline_page(
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Steam provider must delegate to archive.ensure_offline_page (not a stub rewrite)."""
    lib = tmp_path / "mod"
    lib.mkdir()
    folder = _seed(lib, mid="3761838546", title="SteamMod")
    db.upsert_mod(
        ModMetadata(
            published_file_id="3761838546",
            title="SteamMod",
            managed_path=str(folder),
        )
    )
    db.update_mod_platform_info(
        "3761838546",
        platform=PLATFORM_STEAM,
        external_id="3761838546",
    )

    calls: list[tuple] = []
    original = OfflinePageArchiver.ensure_offline_page

    def tracking_ensure(self, info_dir, published_file_id, **kwargs):
        calls.append((str(info_dir), str(published_file_id)))
        path = Path(info_dir) / "index.html"
        path.write_text(
            '<html><div id="smm-offline-banner">offline</div></html>',
            encoding="utf-8",
        )
        return path

    monkeypatch.setattr(OfflinePageArchiver, "ensure_offline_page", tracking_ensure)

    provider = SteamOfflineProvider()
    result = provider.update_offline_page(
        "3761838546",
        managed_path=folder,
        library_root=lib,
    )

    assert calls, "SteamOfflineProvider must call OfflinePageArchiver.ensure_offline_page"
    assert calls[0][1] == "3761838546"
    assert result.provider == PROVIDER_STEAM_ARCHIVE
    assert result.status == OFFLINE_STATUS_ARCHIVED
    assert result.index_path.is_file()
    assert "smm-offline-banner" in result.index_path.read_text(encoding="utf-8")

    info = db.get_mod_display_info("3761838546")
    assert info is not None
    assert info.offline_status == OFFLINE_STATUS_ARCHIVED
    assert info.offline_provider == PROVIDER_STEAM_ARCHIVE
    assert callable(original)


def test_nexus_and_github_use_snapshot_providers(
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "library"
    lib.mkdir()

    nexus = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="336",
        source_url="https://www.nexusmods.com/palworld/mods/336",
        title="NexusMod",
        app_id=1623730,
        game_name="Palworld",
    )
    nexus_folder = _seed(lib, mid=nexus.mod_id, title="NexusMod")

    github = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="UE4SS-RE/RE-UE4SS",
        source_url="https://github.com/UE4SS-RE/RE-UE4SS",
        title="GithubMod",
        app_id=1623730,
        game_name="Palworld",
    )
    github_folder = _seed(lib, mid=github.mod_id, title="GithubMod")

    calls: list[str] = []

    def fake_download(self, url: str, output_dir: Path):
        calls.append(url)
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "assets").mkdir(exist_ok=True)
        index = out / "index.html"
        index.write_text(f"<html><body>{url}</body></html>", encoding="utf-8")
        return SnapshotResult(success=True, html_path=index, asset_count=1, error=None)

    monkeypatch.setattr(WebSnapshotDownloader, "download", fake_download)

    nr = NexusOfflineProvider().update_offline_page(
        nexus.mod_id, managed_path=nexus_folder, library_root=lib
    )
    gr = GithubOfflineProvider().update_offline_page(
        github.mod_id, managed_path=github_folder, library_root=lib
    )

    assert nr.provider == PROVIDER_NEXUS_SNAPSHOT
    assert gr.provider == PROVIDER_GITHUB_SNAPSHOT
    assert nr.status == OFFLINE_STATUS_ARCHIVED
    assert gr.status == OFFLINE_STATUS_ARCHIVED
    assert nr.index_path.as_posix().endswith(".info/offline/index.html")
    assert gr.index_path.as_posix().endswith(".info/offline/index.html")
    assert len(calls) == 2
