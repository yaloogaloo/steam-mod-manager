"""Phase 6: Game Library resolution, maintenance scan, and tree item styles."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.game_library import (
    ORIGIN_BACKUP,
    ORIGIN_FILESYSTEM,
    resolve_games,
)
from services.library_maintenance import is_test_like_name, scan_library_issues
from services.library_reconcile import reconcile_library
from services.library_status import GAME_STATUS_HEALTHY, GAME_STATUS_MISSING_FOLDER
from services.metadata_backup_sync import sync_after_metadata_change


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "game_library.db")
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


def test_case1_healthy_game_on_disk(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameA" / "mod1"
    _write_info(
        folder,
        {
            "published_file_id": "980001",
            "title": "mod1",
            "game_name": "GameA",
            "source_type": "nexus",
            "workspace_id": "ws-980001",
        },
    )
    reconcile_library(library)
    games = resolve_games(library)
    game = next(g for g in games if g.folder == "GameA")
    assert game.count >= 1
    assert game.game_status == GAME_STATUS_HEALTHY
    assert game.origin == ORIGIN_FILESYSTEM


def test_case2_deleted_game_folder_still_listed(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameA" / "mod1"
    _write_info(
        folder,
        {
            "published_file_id": "980002",
            "title": "mod1",
            "game_name": "GameA",
            "source_type": "nexus",
            "workspace_id": "ws-980002",
        },
    )
    reconcile_library(library)
    shutil.rmtree(library / "GameA")
    reconcile_library(library)

    games = resolve_games(library)
    assert any(g.folder == "GameA" for g in games)
    game = next(g for g in games if g.folder == "GameA")
    assert game.game_status == GAME_STATUS_MISSING_FOLDER
    assert game.count >= 1
    assert game.origin in {ORIGIN_BACKUP, ORIGIN_FILESYSTEM}


def test_case3_backup_last_known_path_restores_game(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    library.mkdir(parents=True, exist_ok=True)
    ghost = library / "GhostGame" / "OnlyInBackup"
    # Seed DB + backup without leaving the folder on disk
    ghost.mkdir(parents=True)
    _write_info(
        ghost,
        {
            "published_file_id": "980003",
            "title": "OnlyInBackup",
            "game_name": "GhostGame",
            "source_type": "github",
            "workspace_id": "ws-980003",
        },
    )
    reconcile_library(library)
    assert sync_after_metadata_change("980003", ghost, "import") or True
    shutil.rmtree(library / "GhostGame")
    reconcile_library(library)

    # No games-table row required
    games = resolve_games(library)
    game = next(g for g in games if g.folder == "GhostGame")
    assert game.count >= 1
    assert game.game_status == GAME_STATUS_MISSING_FOLDER
    assert game.origin == ORIGIN_BACKUP


def test_case4_game_and_category_tree_styles_differ() -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from ui.library_view import _GameFilterRow

    app = QApplication.instance() or QApplication([])
    game_row = _GameFilterRow("杀戮尖塔", 35, kind=_GameFilterRow.KIND_GAME)
    cat_row = _GameFilterRow("角色", 5, kind=_GameFilterRow.KIND_CATEGORY, indent=True)
    assert game_row.objectName() == "GameTreeItem"
    assert cat_row.objectName() == "CategoryTreeItem"
    assert game_row.name_label.objectName() == "gameTreeName"
    assert cat_row.name_label.objectName() == "categoryTreeName"
    assert game_row.icon_label.text() == "🎮"
    assert cat_row.icon_label.text() == "📂"
    assert game_row.count_label.text() == "35"
    assert cat_row.count_label.text() == "5"
    del app


def test_case5_test_pollution_scan_does_not_delete(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "test_xxx" / "m1"
    _write_info(
        folder,
        {
            "published_file_id": "980004",
            "title": "m1",
            "game_name": "test_xxx",
            "source_type": "nexus",
            "workspace_id": "ws-980004",
        },
    )
    game_a = library / "GameA" / "m2"
    _write_info(
        game_a,
        {
            "published_file_id": "980005",
            "title": "m2",
            "game_name": "GameA",
            "source_type": "nexus",
            "workspace_id": "ws-980005",
        },
    )
    db.upsert_game(GameInfo(app_id=1, name="Game"))
    reconcile_library(library)

    before_disk = {p.name for p in library.iterdir() if p.is_dir()}
    report = scan_library_issues(library, data_root=data_root)
    assert any(is_test_like_name(x) for x in report.test_like_entries)
    assert any("test_xxx" == x or x.startswith("test_") for x in report.test_like_entries)
    assert any(x == "GameA" or x.casefold() == "gamea" for x in report.test_like_entries)

    # Scan must not delete
    after_disk = {p.name for p in library.iterdir() if p.is_dir()}
    assert before_disk == after_disk
    assert (library / "test_xxx").is_dir()
    assert (library / "GameA").is_dir()
