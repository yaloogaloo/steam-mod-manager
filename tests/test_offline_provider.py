"""OfflineProvider interface + Steam archive wrap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    PLATFORM_STEAM,
    PROVIDER_STEAM_ARCHIVE,
)
from core.models import ModMetadata
from services.archive import OfflinePageArchiver
from services.file_ops import INFO_DIR_NAME
from services.offline.base import OfflineProvider
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
        # Successful mirror marker required by is_stub_offline_page
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
    # keep reference so flake8/ruff don't flag unused if import kept for clarity
    assert callable(original)
