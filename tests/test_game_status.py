"""Phase 6-C: Game status aggregation from existing Mod content_status."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.game_library import resolve_games
from services.game_status import (
    OVERALL_CONFLICT,
    OVERALL_HEALTHY,
    OVERALL_MISSING,
    OVERALL_WARNING,
    aggregate_game_status,
    leading_icon_for_overall,
)
from services.library_reconcile import reconcile_library
from services.library_status import (
    CONTENT_CONTENT_MISSING,
    CONTENT_FOLDER_MISSING,
    CONTENT_HEALTHY,
    CONTENT_IDENTITY_CONFLICT,
    GAME_STATUS_MISSING_FOLDER,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "game_status.db")
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


def test_case1_all_healthy() -> None:
    summary = aggregate_game_status(
        "GameA",
        content_statuses=[CONTENT_HEALTHY, CONTENT_HEALTHY],
    )
    assert summary.overall_status == OVERALL_HEALTHY
    assert summary.healthy_count == 2
    assert summary.total_mods == 2
    assert leading_icon_for_overall(summary.overall_status) == "🎮"


def test_case2_partial_content_missing() -> None:
    summary = aggregate_game_status(
        "GameA",
        content_statuses=[CONTENT_HEALTHY, CONTENT_CONTENT_MISSING],
    )
    assert summary.overall_status == OVERALL_WARNING
    assert summary.content_missing_count == 1
    assert summary.healthy_count == 1
    assert leading_icon_for_overall(summary.overall_status) == "⚠"


def test_case3_identity_conflict() -> None:
    summary = aggregate_game_status(
        "GameA",
        content_statuses=[CONTENT_IDENTITY_CONFLICT, CONTENT_HEALTHY],
    )
    assert summary.overall_status == OVERALL_CONFLICT
    assert summary.conflict_count == 1
    assert leading_icon_for_overall(summary.overall_status) == "❌"


def test_case4_deleted_game_folder_shows_warning_ui(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameA" / "mod1"
    _write_info(
        folder,
        {
            "published_file_id": "981001",
            "title": "mod1",
            "game_name": "GameA",
            "source_type": "nexus",
            "workspace_id": "ws-981001",
        },
    )
    reconcile_library(library)
    shutil.rmtree(library / "GameA")
    reconcile_library(library)

    games = resolve_games(library)
    game = next(g for g in games if g.folder == "GameA")
    assert game.game_status == GAME_STATUS_MISSING_FOLDER
    assert game.status_summary is not None
    assert game.status_summary.overall_status == OVERALL_MISSING
    # UI uses warning-style mark for missing game folders
    assert leading_icon_for_overall(game.status_summary.overall_status) == "⚠"


def test_case5_conflict_outranks_warning() -> None:
    summary = aggregate_game_status(
        "GameA",
        content_statuses=[
            CONTENT_CONTENT_MISSING,
            CONTENT_IDENTITY_CONFLICT,
            CONTENT_FOLDER_MISSING,
        ],
        game_status=GAME_STATUS_MISSING_FOLDER,
    )
    assert summary.overall_status == OVERALL_CONFLICT
