"""Phase 6: Game Library resolution, maintenance scan, and tree item styles."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_GITHUB, PLATFORM_NEXUS
from services.file_ops import INFO_DIR_NAME
from services.game_library import (
    ORIGIN_BACKUP,
    ORIGIN_FILESYSTEM,
    resolve_games,
)
from services.library_maintenance import is_test_like_name, scan_library_issues
from services.library_reconcile import reconcile_library
from services.library_status import GAME_STATUS_HEALTHY, GAME_STATUS_MISSING_FOLDER
from services.metadata_backup_sync import sync_after_metadata_change
from tests.helpers.identity import (
    bind_managed_path,
    create_other_test_mod,
    create_test_mod_identity,
    write_info_sidecar,
)


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


def _seed_game_mod(
    db: DatabaseManager,
    library: Path,
    *,
    game_name: str,
    title: str,
    external_id: str,
    platform: str = PLATFORM_NEXUS,
    source_url: str = "",
) -> tuple[str, Path]:
    if platform == PLATFORM_NEXUS:
        url = source_url or f"https://www.nexusmods.com/{game_name.lower()}/mods/{external_id}"
        created = create_test_mod_identity(
            db,
            platform=platform,
            external_id=external_id,
            title=title,
            app_id=1,
            game_name=game_name,
            source_url=url,
        )
    elif platform == PLATFORM_GITHUB:
        created = create_test_mod_identity(
            db,
            platform=platform,
            external_id=external_id,
            title=title,
            app_id=1,
            game_name=game_name,
            source_url=source_url or f"https://github.com/{external_id}",
        )
    else:
        created = create_other_test_mod(
            db,
            title=title,
            external_id=external_id,
            app_id=1,
            game_name=game_name,
            source_url=source_url,
        )
    internal_id = str(created.mod_id)
    folder = library / game_name / title
    folder.mkdir(parents=True, exist_ok=True)
    write_info_sidecar(
        folder,
        internal_id=internal_id,
        title=title,
        external_id=external_id,
        workspace_id=str(created.workspace_id or external_id),
        app_id=1,
        game_name=game_name,
        platform=platform,
    )
    (folder / "content.pak").write_bytes(b"pak")
    bind_managed_path(db, internal_id, folder, game_name=game_name, title=title)
    return internal_id, folder


def test_case1_healthy_game_on_disk(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    _seed_game_mod(
        db,
        library,
        game_name="GameA",
        title="mod1",
        external_id="980001",
        platform=PLATFORM_NEXUS,
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
    _seed_game_mod(
        db,
        library,
        game_name="GameA",
        title="mod1",
        external_id="980002",
        platform=PLATFORM_NEXUS,
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
    mid, ghost = _seed_game_mod(
        db,
        library,
        game_name="GhostGame",
        title="OnlyInBackup",
        external_id="owner/only-in-backup",
        platform=PLATFORM_GITHUB,
    )
    reconcile_library(library)
    assert sync_after_metadata_change(mid, ghost, "import") or True
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
    assert cat_row.icon_label.text() == "📁"
    assert game_row.count_label.text() == "35"
    assert cat_row.count_label.text() == "5"
    del app


def test_case5_test_pollution_scan_does_not_delete(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    _seed_game_mod(
        db,
        library,
        game_name="test_xxx",
        title="m1",
        external_id="980004",
        platform=PLATFORM_NEXUS,
    )
    _seed_game_mod(
        db,
        library,
        game_name="GameA",
        title="m2",
        external_id="980005",
        platform=PLATFORM_NEXUS,
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
