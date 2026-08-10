# -*- coding: utf-8 -*-
"""Tests for archive-only Files scan + .info/metadata.json sidecar."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import PLATFORM_GITHUB, DatabaseManager
from core.models import ModMetadata
from core.mod_platform import (
    FILE_ROLE_GITHUB_RELEASE_ASSET,
    FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
    FILE_TYPE_MAIN,
    FILE_TYPE_OPTIONAL,
    ModFileEntry,
    ModFilesBundle,
    SOURCE_TYPE_GITHUB,
)
from services.file_ops import (
    INFO_DIR_NAME,
    LEGACY_METADATA_FILENAME,
    METADATA_FILENAME,
    ModFileManager,
)
from services.importers.local_scanner import ARCHIVE_SUFFIXES, scan_mod_directory
from services.info_sidecar import (
    InfoSidecar,
    ROLE_MAIN,
    ROLE_SOURCE,
    apply_sidecar_to_db,
    load_info_sidecar,
    write_sidecar_for_mod,
)
from services.mod_files import ModFileManager as JsonModFiles


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "sidecar.db")
    yield manager
    DatabaseManager.reset_instance()


def test_scan_archives_only_ignores_loose_files(tmp_path: Path) -> None:
    folder = tmp_path / "mod"
    folder.mkdir()
    (folder / "a.pak").write_bytes(b"pak")
    (folder / "b.json").write_text("{}", encoding="utf-8")
    (folder / "c.ini").write_text("x=1", encoding="utf-8")
    (folder / "nested").mkdir()
    (folder / "nested" / "d.pak").write_bytes(b"p")
    assert scan_mod_directory(folder).files == []

    (folder / "main.zip").write_bytes(b"PK")
    (folder / "extra.7z").write_bytes(b"7z")
    (folder / "old.rar").write_bytes(b"Rar")
    names = {f.filename for f in scan_mod_directory(folder).files}
    assert names == {"main.zip", "extra.7z", "old.rar"}
    assert ARCHIVE_SUFFIXES == {".zip", ".7z", ".rar"}


def test_info_sidecar_roundtrip(db: DatabaseManager, tmp_path: Path) -> None:
    folder = tmp_path / "library" / "Game" / "Mod"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    legacy = info / LEGACY_METADATA_FILENAME
    legacy.write_text(
        json.dumps({"published_file_id": "8801", "title": "Mod"}),
        encoding="utf-8",
    )
    assert not (info / METADATA_FILENAME).exists()
    (folder / "release.zip").write_bytes(b"PK")
    (folder / "source.zip").write_bytes(b"PK")

    db.upsert_mod(
        ModMetadata(published_file_id="8801", title="Mod", managed_path=str(folder))
    )
    db.update_mod_platform_info(
        "8801",
        platform=PLATFORM_GITHUB,
        source_url="https://github.com/a/b",
    )
    db.update_mod_user_metadata(
        "8801",
        {
            "display_name": "Pretty Name",
            "custom_description": "Hello world",
            "user_notes": "",
            "favorite": False,
            "custom_deploy_path": "D:/mods/out",
        },
    )
    db.set_mod_files(
        "8801",
        ModFilesBundle(
            files=[
                ModFileEntry(
                    id="m",
                    filename="release.zip",
                    file_role=FILE_ROLE_GITHUB_RELEASE_ASSET,
                    source_type=SOURCE_TYPE_GITHUB,
                    type=FILE_TYPE_MAIN,
                    selected_for_deploy=True,
                ),
                ModFileEntry(
                    id="s",
                    filename="source.zip",
                    file_role=FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
                    source_type=SOURCE_TYPE_GITHUB,
                    type=FILE_TYPE_OPTIONAL,
                    selected_for_deploy=False,
                ),
            ]
        ),
    )

    path = write_sidecar_for_mod(folder, "8801", db=db)
    assert path is not None and path.is_file()
    assert path.name == METADATA_FILENAME
    assert not legacy.exists()
    loaded = load_info_sidecar(folder)
    assert loaded is not None
    assert loaded.display_name == "Pretty Name"
    assert loaded.description == "Hello world"
    assert loaded.source_type == PLATFORM_GITHUB
    assert loaded.url == "https://github.com/a/b"
    assert loaded.custom_deploy_path == "D:/mods/out"
    assert loaded.file_roles["release.zip"] == ROLE_MAIN
    assert loaded.file_roles["source.zip"] == ROLE_SOURCE

    db.update_mod_user_metadata(
        "8801",
        {
            "display_name": "Wiped",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
            "custom_deploy_path": "",
            "source_url": "",
            "platform": PLATFORM_GITHUB,
        },
    )
    db.set_mod_files("8801", ModFilesBundle())
    assert apply_sidecar_to_db(folder, mod_id="8801", db=db, rescan_archives=True)
    info2 = db.get_mod_display_info("8801")
    assert info2 is not None
    assert info2.display_name == "Pretty Name"
    assert info2.custom_description == "Hello world"
    assert info2.custom_deploy_path == "D:/mods/out"
    files = {f.filename: f for f in JsonModFiles(db).get_files("8801")}
    assert "release.zip" in files
    assert files["release.zip"].file_role == FILE_ROLE_GITHUB_RELEASE_ASSET
    assert files["source.zip"].file_role == FILE_ROLE_GITHUB_SOURCE_ARCHIVE


def test_save_metadata_migrates_legacy_mod_json(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "Mod"
    folder.mkdir()
    info = folder / INFO_DIR_NAME
    info.mkdir()
    legacy = info / LEGACY_METADATA_FILENAME
    legacy.write_text(
        json.dumps({"published_file_id": "8802", "title": "Legacy"}),
        encoding="utf-8",
    )
    mgr = ModFileManager(tmp_path)
    meta = mgr.load_metadata(folder)
    assert meta is not None
    assert meta.title == "Legacy"
    meta.description = "Updated"
    mgr.save_metadata(meta, folder)
    assert (info / METADATA_FILENAME).is_file()
    assert not legacy.exists()
    reloaded = mgr.load_metadata(folder)
    assert reloaded is not None
    assert reloaded.description == "Updated"


def test_info_sidecar_to_from_dict() -> None:
    raw = {
        "display_name": "A",
        "description": "B",
        "source_type": "nexus",
        "url": "https://x",
        "workspace_id": "1",
        "custom_deploy_path": "C:/d",
        "offline_page_path": ".info/offline/index.html",
        "cover_path": "cover.png",
        "published_file_id": "99",
        "file_roles": {"a.zip": "Main", "b.zip": "Source"},
    }
    side = InfoSidecar.from_dict(raw)
    assert side.to_dict()["file_roles"]["a.zip"] == "Main"
    assert InfoSidecar.from_dict(side.to_dict()).description == "B"
