"""Phase 4: library reconcile — external import, rename, rebuild."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import is_internal_mod_id
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, persist_unified_metadata_dict
from services.library_reconcile import (
    LIBRARY_STATUS_IMPORTED,
    LIBRARY_STATUS_MISSING,
    reconcile_library,
    resolve_library_games,
)
from services.metadata_backup import backup_root, load_backup
from services.mod_identity import INTERNAL_ID_KEY, ensure_mod_identity, read_internal_id
from services.mod_library_cache import build_library_snapshot
from services.mod_metadata_resolver import list_visible_mods, resolve_mod_metadata


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "reconcile.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    monkeypatch.setattr("core.paths.data_dir", lambda: root)
    return root


def _write_info(folder: Path, payload: dict) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (folder / "content.pak").write_bytes(b"pak")


def test_case1_external_copy_full_game_detected(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameX" / "ModA"
    _write_info(
        folder,
        {
            "published_file_id": "960001",
            "title": "ModA",
            "display_name": "ModA",
            "game_name": "GameX",
            "source_type": "nexus",
            "url": "https://example.com/960001",
            "workspace_id": "ws-960001",
            "external_id": "960001",
        },
    )

    result = reconcile_library(library)
    assert result.scanned >= 1
    assert result.imported >= 1
    assert db.get_mod("960001") is not None
    assert load_backup("960001") is not None
    visible = list_visible_mods(library, "GameX")
    assert any(m.published_file_id == "960001" for m in visible)
    row = db.get_mod_backup_row("960001")
    assert row is not None
    assert int(row["folder_present"]) == 1
    assert str(row.get("library_status") or "") in (
        LIBRARY_STATUS_IMPORTED,
        "normal",
        "",
    )
    # Phase 5: sticky source survives; first disk discovery → external
    assert str(row.get("source_type") or "") == "external"
    assert str(row.get("content_status") or "") == "healthy"


def test_case2_delete_game_still_visible(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameY" / "ModB"
    _write_info(
        folder,
        {
            "published_file_id": "960002",
            "title": "ModB",
            "game_name": "GameY",
            "source_type": "github",
            "url": "https://example.com/960002",
            "workspace_id": "ws-960002",
        },
    )
    reconcile_library(library)
    shutil.rmtree(library / "GameY")
    result = reconcile_library(library)
    assert result.missing >= 1

    games = resolve_library_games(library)
    assert any(g["folder"] == "GameY" for g in games)
    game = next(g for g in games if g["folder"] == "GameY")
    assert int(game["count"]) >= 1
    resolved = resolve_mod_metadata("960002", folder)
    assert resolved is not None
    assert resolved.folder_present is False
    row = db.get_mod_backup_row("960002")
    assert row is not None
    assert str(row.get("library_status") or "") == LIBRARY_STATUS_MISSING


def test_case3_restore_info_overwrites_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameZ" / "ModC"
    _write_info(
        folder,
        {
            "published_file_id": "960003",
            "title": "Old",
            "display_name": "Old",
            "game_name": "GameZ",
            "source_type": "nexus",
            "url": "https://example.com/960003",
            "workspace_id": "ws-960003",
        },
    )
    reconcile_library(library)
    archive = tmp_path / "archive" / "GameZ"
    shutil.copytree(library / "GameZ", archive)
    shutil.rmtree(library / "GameZ")
    reconcile_library(library)

    shutil.copytree(archive, library / "GameZ")
    restored = library / "GameZ" / "ModC"
    meta_path = restored / INFO_DIR_NAME / METADATA_FILENAME
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    data["title"] = "New"
    data["display_name"] = "New"
    meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    reconcile_library(library)
    resolved = resolve_mod_metadata("960003", restored)
    assert resolved is not None
    assert (resolved.display_name or resolved.title) == "New"
    snap = load_backup("960003")
    assert snap is not None
    assert snap.metadata.get("title") == "New" or snap.metadata.get("display_name") == "New"


def test_case4_rename_does_not_duplicate(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "Anno1800" / "BetterHarbor"
    _write_info(
        folder,
        {
            "published_file_id": "960004",
            "title": "BetterHarbor",
            "game_name": "Anno1800",
            "source_type": "nexus",
            "url": "https://example.com/960004",
            "workspace_id": "ws-960004",
            INTERNAL_ID_KEY: "fixed-uuid-960004",
        },
    )
    reconcile_library(library)
    renamed = library / "Anno1800" / "Better Harbor New"
    folder.rename(renamed)
    result = reconcile_library(library)
    assert result.renamed >= 1

    visible = list_visible_mods(library, "Anno1800")
    ids = [m.published_file_id for m in visible]
    assert ids.count("960004") == 1
    row = db.get_mod_backup_row("960004")
    assert row is not None
    assert Path(str(row["last_known_path"])).samefile(renamed)


def test_case5_generates_uuid_without_published_file_id(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameU" / "LocalMod"
    _write_info(
        folder,
        {
            "title": "LocalMod",
            "display_name": "LocalMod",
            "game_name": "GameU",
            "source_type": "github",
            "url": "https://github.com/a/b",
            "app_id": 1623730,
            "external_id": "a/b",
        },
    )
    result = reconcile_library(library)
    assert result.imported >= 1

    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert read_internal_id(data)
    assert str(data.get("published_file_id") or "").isdigit()
    mid = str(data["published_file_id"])
    assert load_backup(mid) is not None
    # Stable on second pass
    internal = read_internal_id(data)
    reconcile_library(library)
    data2 = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert read_internal_id(data2) == internal
    assert str(data2.get("published_file_id")) == mid


def test_case6_rebuild_from_info_without_database(
    tmp_path: Path, data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New PC: only mod/ + .info; empty DB → rebuild."""
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "fresh.db")
    library = tmp_path / "mod"
    folder = library / "Palworld" / "Imported"
    _write_info(
        folder,
        {
            "published_file_id": "960006",
            "title": "Imported",
            "game_name": "Palworld",
            "source_type": "nexus",
            "url": "https://example.com/960006",
            "workspace_id": "ws-960006",
            "external_id": "336",
        },
    )
    assert db.get_mod("960006") is None
    result = reconcile_library(library)
    assert result.imported >= 1
    assert db.get_mod("960006") is not None
    assert (backup_root("960006") / "metadata.json").is_file()
    snap = build_library_snapshot(library)
    assert any(c.id == "960006" for c in snap.cards)
    DatabaseManager.reset_instance()


def test_ensure_mod_identity_does_not_use_folder_name(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "SomeFolderName"
    folder.mkdir()
    (folder / INFO_DIR_NAME).mkdir()
    payload = {"title": "X", "source_type": "github"}
    mid, out, changed = ensure_mod_identity(folder, payload)
    assert changed is True
    assert mid == ""
    assert out.get("identity_status") == "unresolved"
    assert not str(out.get("published_file_id") or "").isdigit() or not is_internal_mod_id(
        out.get("published_file_id")
    )
    assert read_internal_id(out)
