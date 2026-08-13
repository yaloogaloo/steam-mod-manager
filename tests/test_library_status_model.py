"""Phase 5: sticky source_type + recomputed content_status / game_status."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_reconcile import reconcile_library, resolve_library_games
from services.library_status import (
    CONTENT_FOLDER_MISSING,
    CONTENT_HEALTHY,
    GAME_STATUS_HEALTHY,
    GAME_STATUS_MISSING_FOLDER,
    SOURCE_EXTERNAL,
    SOURCE_STEAM,
)
from services.mod_library_cache import build_library_snapshot


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "status_model.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    monkeypatch.setattr("core.paths.data_dir", lambda: root)
    return root


def _write_info(folder: Path, payload: dict, *, with_content: bool = True) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if with_content:
        (folder / "content.pak").write_bytes(b"pak")


def test_steam_mod_source_healthy(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "Anno1800" / "WorkshopMod"
    _write_info(
        folder,
        {
            "published_file_id": "1610173938",
            "title": "Steam Mod",
            "game_name": "Anno1800",
            "source_type": "steam",
            "url": "https://steamcommunity.com/sharedfiles/filedetails/?id=1610173938",
            "external_id": "1610173938",
        },
    )
    reconcile_library(library)
    row = db.get_mod_backup_row("1610173938")
    assert row is not None
    assert str(row.get("source_type") or "") == SOURCE_STEAM
    assert str(row.get("content_status") or "") == CONTENT_HEALTHY

    snap = build_library_snapshot(library)
    card = next(c for c in snap.cards if c.id == "1610173938")
    assert card.source_type == SOURCE_STEAM
    assert card.content_status == CONTENT_HEALTHY


def test_external_source_sticky_across_refreshes(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameX" / "CopiedMod"
    _write_info(
        folder,
        {
            "published_file_id": "970001",
            "title": "Copied",
            "game_name": "GameX",
            "source_type": "nexus",
            "external_id": "970001",
            "workspace_id": "ws-970001",
        },
    )
    reconcile_library(library)
    row = db.get_mod_backup_row("970001")
    assert row is not None
    assert str(row.get("source_type") or "") == SOURCE_EXTERNAL

    for _ in range(10):
        reconcile_library(library)

    row = db.get_mod_backup_row("970001")
    assert row is not None
    assert str(row.get("source_type") or "") == SOURCE_EXTERNAL
    assert str(row.get("content_status") or "") == CONTENT_HEALTHY


def test_delete_mod_folder_keeps_source_marks_missing(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameY" / "ExtMod"
    _write_info(
        folder,
        {
            "published_file_id": "970002",
            "title": "Ext",
            "game_name": "GameY",
            "source_type": "github",
            "workspace_id": "ws-970002",
        },
    )
    reconcile_library(library)
    assert str(db.get_mod_backup_row("970002")["source_type"]) == SOURCE_EXTERNAL

    shutil.rmtree(folder)
    reconcile_library(library)
    row = db.get_mod_backup_row("970002")
    assert row is not None
    assert str(row.get("source_type") or "") == SOURCE_EXTERNAL
    assert str(row.get("content_status") or "") == CONTENT_FOLDER_MISSING
    assert int(row["folder_present"]) == 0


def test_delete_game_folder_game_status_missing(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "Anno1800" / "ModA"
    _write_info(
        folder,
        {
            "published_file_id": "970003",
            "title": "ModA",
            "game_name": "Anno1800",
            "source_type": "nexus",
            "workspace_id": "ws-970003",
        },
    )
    reconcile_library(library)
    games = resolve_library_games(library)
    game = next(g for g in games if g["folder"] == "Anno1800")
    assert game["game_status"] == GAME_STATUS_HEALTHY

    shutil.rmtree(library / "Anno1800")
    reconcile_library(library)
    games = resolve_library_games(library)
    assert any(g["folder"] == "Anno1800" for g in games)
    game = next(g for g in games if g["folder"] == "Anno1800")
    assert int(game["count"]) >= 1
    assert game["game_status"] == GAME_STATUS_MISSING_FOLDER


def test_restore_folder_returns_healthy(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameZ" / "ModR"
    _write_info(
        folder,
        {
            "published_file_id": "970004",
            "title": "ModR",
            "game_name": "GameZ",
            "source_type": "nexus",
            "workspace_id": "ws-970004",
        },
    )
    reconcile_library(library)
    archive = tmp_path / "archive" / "GameZ"
    shutil.copytree(library / "GameZ", archive)
    shutil.rmtree(library / "GameZ")
    reconcile_library(library)
    row = db.get_mod_backup_row("970004")
    assert str(row.get("content_status") or "") == CONTENT_FOLDER_MISSING
    assert str(row.get("source_type") or "") == SOURCE_EXTERNAL

    shutil.copytree(archive, library / "GameZ")
    reconcile_library(library)
    row = db.get_mod_backup_row("970004")
    assert str(row.get("source_type") or "") == SOURCE_EXTERNAL
    assert str(row.get("content_status") or "") == CONTENT_HEALTHY
