"""ModUpdateChecker + update_sources."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_GITHUB, PLATFORM_NEXUS, PLATFORM_STEAM
from core.models import ModMetadata
from services.mod_update import ModUpdateChecker
from services.update_sources.github import GithubUpdateSource, parse_github_repo
from services.update_sources.steam import SteamUpdateSource


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "update.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_steam_unsupported(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="1", title="S"))
    db.update_mod_platform_fields = getattr(db, "update_mod_user_metadata", None)
    # mark steam platform via register path
    report = ModUpdateChecker(db=db).check_mod(1, persist=True)
    assert report.supported is False or report.error == "unsupported"
    assert report.has_update is False
    steam = SteamUpdateSource().check_version(mod_id="1")
    assert steam.supported is False
    assert steam.error == "unsupported"


def test_github_parse_and_release(db: DatabaseManager, monkeypatch) -> None:
    assert parse_github_repo(external_id="owner/proj") == ("owner", "proj")
    assert parse_github_repo(source_url="https://github.com/a/b") == ("a", "b")

    payload = json.dumps({"tag_name": "v1.4.0", "name": "1.4.0"}).encode()

    def fake_opener(req):
        return payload

    src = GithubUpdateSource(opener=fake_opener)
    result = src.check_version(
        mod_id="9",
        source_url="https://github.com/owner/proj",
        external_id="owner/proj",
    )
    assert result.supported is True
    assert result.latest == "1.4.0"

    info = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="owner/proj",
        source_url="https://github.com/owner/proj",
        title="GH Mod",
            app_id=1623730,
        game_name="Palworld",
)
    db.update_mod_version(info.mod_id, installed_version="1.0.0")
    checker = ModUpdateChecker(db=db)
    # inject fake source via monkeypatch get_update_source
    monkeypatch.setattr(
        "services.mod_update.get_update_source",
        lambda platform: GithubUpdateSource(opener=fake_opener),
    )
    report = checker.check_mod(info.mod_id, persist=True)
    assert report.has_update is True
    assert report.current == "1.0.0"
    assert report.latest == "1.4.0"
    assert db.get_mod_version(info.mod_id).mod_version == "1.4.0"
    assert db.get_mod_version(info.mod_id).installed_version == "1.0.0"


def test_nexus_stub(db: DatabaseManager) -> None:
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="42",
        source_url="https://www.nexusmods.com/x/mods/42",
        title="Nexus",
            app_id=1623730,
        game_name="Palworld",
)
    report = ModUpdateChecker(db=db).check_mod(info.mod_id)
    assert report.supported is False
    assert report.source == "nexus"
