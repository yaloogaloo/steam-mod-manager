"""metadata.json incremental merge must not wipe paths on partial sidecar save."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager, PLATFORM_GITHUB
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager
from services.info_sidecar import InfoSidecar, save_info_sidecar, write_sidecar_for_mod


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "meta.db")
    yield manager
    DatabaseManager.reset_instance()


def test_save_info_sidecar_preserves_existing_paths(tmp_path: Path) -> None:
    folder = tmp_path / "Mod"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    existing = {
        "published_file_id": "9001",
        "title": "Keep Title",
        "url": "https://github.com/a/b",
        "offline_page_path": ".info/index.html",
        "source_path": "D:/workshop/content/9001",
        "managed_path": str(folder),
    }
    (info / METADATA_FILENAME).write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    sidecar = InfoSidecar(
        display_name="New Name",
        description="New desc",
        source_type=PLATFORM_GITHUB,
        published_file_id="9001",
    )
    save_info_sidecar(folder, sidecar)

    loaded = json.loads((info / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert loaded["display_name"] == "New Name"
    assert loaded["description"] == "New desc"
    assert loaded["source_type"] == PLATFORM_GITHUB
    assert loaded["url"] == "https://github.com/a/b"
    assert loaded["offline_page_path"] == ".info/index.html"
    assert loaded["source_path"] == "D:/workshop/content/9001"


def test_save_metadata_preserves_existing_fields(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "Mod"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": "9002",
                "title": "T",
                "url": "https://example.com/mod",
                "offline_page_path": ".info/page.html",
            }
        ),
        encoding="utf-8",
    )
    meta = ModMetadata(
        published_file_id="9002",
        title="T",
        description="Only desc changed",
        managed_path=str(folder),
    )
    ModFileManager(tmp_path).save_metadata(meta, folder)
    loaded = json.loads((info / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert loaded["description"] == "Only desc changed"
    assert loaded["url"] == "https://example.com/mod"
    assert loaded["offline_page_path"] == ".info/page.html"


def test_write_sidecar_for_mod_does_not_clear_url(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "Mod"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": "9003",
                "title": "Mod",
                "url": "https://nexusmods.com/x/mods/1",
            }
        ),
        encoding="utf-8",
    )
    db.upsert_mod(ModMetadata(published_file_id="9003", title="Mod"))
    db.update_mod_user_metadata(
        "9003",
        {
            "display_name": "Renamed",
            "custom_description": "Note",
            "user_notes": "",
            "favorite": False,
            "platform": PLATFORM_GITHUB,
            "source_url": "",
        },
    )
    write_sidecar_for_mod(folder, "9003", db=db)
    loaded = json.loads((info / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert loaded["url"] == "https://nexusmods.com/x/mods/1"
    assert loaded["display_name"] == "Renamed"
