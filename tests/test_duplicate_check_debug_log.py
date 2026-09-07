"""Temp: assert duplicate_check DEBUG logs include matched_by + input fields."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_NEXUS
from services.identity_service import create_mod_identity
from services.importers.duplicate_check import (
    _LAST_DUP_DIAG,
    check_import_duplicate,
    find_duplicate_mod,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "dup_diag.db")
    manager.update_game_deploy_config(1623730, name="Palworld")
    yield manager
    DatabaseManager.reset_instance()


def test_duplicate_debug_log_matched_by_external_id(
    db: DatabaseManager, caplog: pytest.LogCaptureFixture
) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="424242",
        source_url="https://www.nexusmods.com/palworld/mods/424242",
        title="Existing Nexus Mod",
        app_id=1623730,
        game_name="Palworld",
        operation="import",
    )
    folder = r"D:\Temp\zspace\Mods\fake\Existing Nexus Mod"
    with caplog.at_level(logging.DEBUG, logger="services.importers.duplicate_check"):
        hit = find_duplicate_mod(
            db,
            platform=PLATFORM_NEXUS,
            external_id="424242",
            source_url="",
            app_id=1623730,
            folder_path=folder,
        )
        assert hit is not None
        assert hit.mod_id == created.mod_id
        assert _LAST_DUP_DIAG.get("matched_by") == "external_id"
        assert "matched_by=external_id" in caplog.text
        assert f"matched_mod_id={created.mod_id}" in caplog.text
        assert "matched_title=" in caplog.text
        assert "matched_platform=nexus" in caplog.text
        assert "matched_app_id=1623730" in caplog.text
        assert "matched_external_id=424242" in caplog.text
        assert "input_external_id=424242" in caplog.text
        assert f"input_folder_path={folder}" in caplog.text

        caplog.clear()
        dup = check_import_duplicate(
            db,
            platform=PLATFORM_NEXUS,
            external_id="424242",
            source_url="",
            app_id=1623730,
            folder_path=folder,
        )
        assert dup is not None
        assert dup.is_duplicate
        assert "check_import_duplicate returning duplicate" in caplog.text
        assert "matched_by=external_id" in caplog.text


def test_duplicate_debug_log_matched_by_source_url(
    db: DatabaseManager, caplog: pytest.LogCaptureFixture
) -> None:
    url = "https://www.nexusmods.com/palworld/mods/777001"
    create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="777001",
        source_url=url,
        title="URL Hit Mod",
        app_id=1623730,
        game_name="Palworld",
        operation="import",
    )
    with caplog.at_level(logging.DEBUG, logger="services.importers.duplicate_check"):
        # Different external_id so match falls through to source_url.
        hit = find_duplicate_mod(
            db,
            platform=PLATFORM_NEXUS,
            external_id="999999",
            source_url=url,
            app_id=1623730,
            folder_path="/tmp/some-folder",
        )
        assert hit is not None
        assert _LAST_DUP_DIAG.get("matched_by") == "source_url"
        assert "matched_by=source_url" in caplog.text
        assert "input_source_url=" in caplog.text
