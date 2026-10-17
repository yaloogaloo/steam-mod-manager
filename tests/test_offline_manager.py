"""OfflineManager routes by platform."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
from core.mod_platform import (
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    PROVIDER_GITHUB_SNAPSHOT,
    PROVIDER_NEXUS_MANUAL_IMPORT,
    PROVIDER_STEAM_ARCHIVE,
)
from services.archive import OfflinePageArchiver
from services.offline.layout_snapshot import LayoutSnapshotResult
from services.offline.manager import OfflineManager
from services.offline.github import GithubOfflineProvider
from services.offline.nexus_manual import NexusManualOfflineProvider
from services.offline.steam import SteamOfflineProvider


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "offline_mgr.db")
    yield manager
    DatabaseManager.reset_instance()


def _folder(lib: Path, *, title: str, game: str = "Game") -> Path:
    folder = lib / game / title
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_manager_selects_steam_nexus_github(
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "library"
    lib.mkdir()

    steam_folder = _folder(lib, title="S")
    steam = create_steam_test_mod(db, external_id="111", title="S")
    prove_managed_folder(
        db, steam_folder, handle=steam.mod_id, title="S", game_name="Game"
    )

    db.update_mod_platform_info(steam.mod_id, platform=PLATFORM_STEAM, external_id="111")

    nexus = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="336",
        source_url="https://www.nexusmods.com/palworld/mods/336",
        title="N",
        app_id=1623730,
        game_name="Palworld",
    )
    nexus_folder = _folder(lib, title="N", game="Palworld")
    prove_managed_folder(
        db,
        nexus_folder,
        handle=nexus.mod_id,
        title="N",
        app_id=1623730,
        game_name="Palworld",
    )

    github = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="o/r",
        source_url="https://github.com/o/r",
        title="G",
        app_id=1623730,
        game_name="Palworld",
    )
    github_folder = _folder(lib, title="G", game="Palworld")
    prove_managed_folder(
        db,
        github_folder,
        handle=github.mod_id,
        title="G",
        app_id=1623730,
        game_name="Palworld",
    )
    def tracking_ensure(self, info_dir, published_file_id, **kwargs):
        path = Path(info_dir) / "index.html"
        path.write_text(
            '<html><div id="smm-offline-banner">ok</div></html>',
            encoding="utf-8",
        )
        return path

    monkeypatch.setattr(OfflinePageArchiver, "ensure_offline_page", tracking_ensure)

    class FakeLayout:
        def snapshot(self, url: str, output_dir: Path | str) -> LayoutSnapshotResult:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            index = out / "index.html"
            index.write_text(f"<html>{url}</html>", encoding="utf-8")
            return LayoutSnapshotResult(
                success=True, html_path=index, backend="layout"
            )

    mgr = OfflineManager(
        db=db,
        library_root=lib,
        providers=(
            SteamOfflineProvider(),
            NexusManualOfflineProvider(),
            GithubOfflineProvider(layout_provider=FakeLayout()),
        ),
    )

    assert mgr.get_provider_for_platform(PLATFORM_STEAM).get_provider_name() == (
        PROVIDER_STEAM_ARCHIVE
    )
    assert mgr.get_provider_for_platform(PLATFORM_NEXUS).get_provider_name() == (
        PROVIDER_NEXUS_MANUAL_IMPORT
    )
    assert mgr.get_provider_for_platform(PLATFORM_GITHUB).get_provider_name() == (
        PROVIDER_GITHUB_SNAPSHOT
    )

    r1 = mgr.update_mod_offline(steam.mod_id, managed_path=steam_folder)
    html = tmp_path / "n.html"
    html.write_text("<html><body>336</body></html>", encoding="utf-8")
    r2 = mgr.import_mod_offline_html(nexus.mod_id, html, managed_path=nexus_folder)
    r3 = mgr.update_mod_offline(github.mod_id, managed_path=github_folder)

    assert r1.provider == PROVIDER_STEAM_ARCHIVE
    assert r2.provider == PROVIDER_NEXUS_MANUAL_IMPORT
    assert r3.provider == PROVIDER_GITHUB_SNAPSHOT
    assert all(r.index_path.is_file() for r in (r1, r2, r3))
    assert "336" in r2.index_path.read_text(encoding="utf-8")
    assert "o/r" in r3.index_path.read_text(encoding="utf-8")
