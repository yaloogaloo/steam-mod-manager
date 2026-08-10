"""Platform field must survive Steam upsert, JSON round-trip, and library refresh."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    METADATA_SOURCE_TYPE_KEY,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    parse_metadata_platform,
)
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager
from services.info_sidecar import InfoSidecar, apply_sidecar_to_db, load_info_sidecar


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "plat.db")
    yield manager
    DatabaseManager.reset_instance()


def test_parse_metadata_platform_reads_canonical_and_legacy_keys() -> None:
    assert parse_metadata_platform({METADATA_SOURCE_TYPE_KEY: "github"}) == PLATFORM_GITHUB
    assert parse_metadata_platform({"platform": "nexus"}) == PLATFORM_NEXUS
    assert parse_metadata_platform({"source": "other"}) == "other"
    assert parse_metadata_platform({}) == ""
    assert parse_metadata_platform({"source_type": ""}) == ""


def test_info_sidecar_from_dict_never_defaults_missing_platform_to_steam() -> None:
    side = InfoSidecar.from_dict({"published_file_id": "1", "title": "T"})
    assert side.source_type == ""


def test_upsert_mod_preserves_non_steam_platform(db: DatabaseManager) -> None:
    from core.game_info import GameInfo

    mid = "4242"
    db.upsert_game(GameInfo(app_id=100, name="Game", folder_name="Game"))
    db.upsert_mod(ModMetadata(published_file_id=mid, title="Workshop Mod", app_id=100))
    db.update_mod_user_metadata(
        mid,
        {
            "display_name": "GH Mod",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
            "platform": PLATFORM_GITHUB,
            "source_url": "https://github.com/a/b",
        },
    )
    before = db.get_mod_display_info(mid)
    assert before is not None
    assert before.platform == PLATFORM_GITHUB

    db.upsert_mod(
        ModMetadata(
            published_file_id=mid,
            title="Updated Steam Title",
            app_id=100,
            preview_url="http://x",
        )
    )
    after = db.get_mod_display_info(mid)
    assert after is not None
    assert after.platform == PLATFORM_GITHUB
    assert after.source_url == "https://github.com/a/b"


def test_apply_sidecar_without_source_type_keeps_db_platform(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "Mod"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": "8801",
                "title": "Mod",
                "display_name": "Pretty",
            }
        ),
        encoding="utf-8",
    )
    db.upsert_mod(ModMetadata(published_file_id="8801", title="Mod"))
    db.update_mod_user_metadata(
        "8801",
        {
            "display_name": "Pretty",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
            "platform": PLATFORM_NEXUS,
            "source_url": "https://www.nexusmods.com/x/mods/1",
        },
    )
    assert apply_sidecar_to_db(folder, mod_id="8801", db=db)
    info2 = db.get_mod_display_info("8801")
    assert info2 is not None
    assert info2.platform == PLATFORM_NEXUS


def test_load_metadata_reads_source_type_from_json(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "Mod"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": "8802",
                "title": "Mod",
                METADATA_SOURCE_TYPE_KEY: PLATFORM_GITHUB,
            }
        ),
        encoding="utf-8",
    )
    meta = ModFileManager(tmp_path).load_metadata(folder)
    assert meta is not None
    assert meta.source_type == PLATFORM_GITHUB


def test_write_sidecar_roundtrip_source_type(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "Mod"
    folder.mkdir()
    db.upsert_mod(ModMetadata(published_file_id="8803", title="Mod"))
    db.update_mod_user_metadata(
        "8803",
        {
            "display_name": "X",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
            "platform": PLATFORM_GITHUB,
            "source_url": "https://github.com/a/b",
        },
    )
    from services.info_sidecar import write_sidecar_for_mod

    write_sidecar_for_mod(folder, "8803", db=db)
    raw = json.loads((folder / INFO_DIR_NAME / METADATA_FILENAME).read_text())
    assert raw.get(METADATA_SOURCE_TYPE_KEY) == PLATFORM_GITHUB
    loaded = load_info_sidecar(folder)
    assert loaded is not None
    assert loaded.source_type == PLATFORM_GITHUB
