"""Nexus local offline HTML generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    FILE_TYPE_MAIN,
    FILE_TYPE_OPTIONAL,
    OFFLINE_STATUS_GENERATED,
    PLATFORM_NEXUS,
    PROVIDER_NEXUS_GENERATOR,
    ModFileEntry,
    ModFilesBundle,
)
from services.file_ops import INFO_DIR_NAME
from services.offline.nexus import NexusOfflineProvider


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "nexus_offline.db")
    yield manager
    DatabaseManager.reset_instance()


def test_nexus_generates_index_with_id_and_files(
    tmp_path: Path, db: DatabaseManager
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
    db.set_mod_files(
        mid,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    name="Main File",
                    filename="main.pak",
                    path="main.pak",
                    type=FILE_TYPE_MAIN,
                    enabled=True,
                ),
                ModFileEntry(
                    name="Optional Hat",
                    filename="optional.pak",
                    path="optional.pak",
                    type=FILE_TYPE_OPTIONAL,
                    enabled=False,
                ),
            ]
        ),
    )

    lib = tmp_path / "library"
    folder = lib / "Palworld" / "Cool Nexus Mod"
    info_dir = folder / INFO_DIR_NAME
    info_dir.mkdir(parents=True)
    (info_dir / "mod.json").write_text(
        json.dumps({"published_file_id": mid, "title": "Cool Nexus Mod"}),
        encoding="utf-8",
    )

    result = NexusOfflineProvider().update_offline_page(
        mid, managed_path=folder, library_root=lib
    )

    assert result.index_path == info_dir / "index.html"
    assert result.index_path.is_file()
    html = result.index_path.read_text(encoding="utf-8")
    assert "Nexus Mods" in html
    assert "336" in html
    assert "https://www.nexusmods.com/palworld/mods/336" in html
    assert "Main File" in html
    assert "Optional Hat" in html
    assert "✓" in html
    assert "□" in html
    assert result.status == OFFLINE_STATUS_GENERATED
    assert result.provider == PROVIDER_NEXUS_GENERATOR

    refreshed = db.get_mod_display_info(mid)
    assert refreshed is not None
    assert refreshed.offline_status == OFFLINE_STATUS_GENERATED
    assert refreshed.offline_provider == PROVIDER_NEXUS_GENERATOR
