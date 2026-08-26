"""Deployment Record snapshot must match Library deployed set (incl. app_id=0)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.game_info import GameInfo
from core.models import ModMetadata
from services import deployment_record as dr
from ui.library_query import (
    FILTER_DEPLOYMENT_RECORD,
    RECORD_STATUS_LABEL_EXTRA,
    ModFilterIndex,
    compute_record_relative_status,
    record_relative_badge_label,
)


ANNO = 916440
GAME_FOLDER = "Anno 1800"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deployment_record_snapshot.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _mod(
    db: DatabaseManager,
    mod_id: int,
    *,
    app_id: int,
    deployed: bool,
    last_known_path: str = "",
) -> None:
    db.upsert_mod(
        ModMetadata(
            published_file_id=str(mod_id),
            title=f"Mod {mod_id}",
            app_id=app_id,
        )
    )
    db.update_mod_deploy_status(
        mod_id,
        deploy_status=(
            DEPLOY_STATUS_DEPLOYED if deployed else DEPLOY_STATUS_NOT_DEPLOYED
        ),
        deploy_path="" if not deployed else f"/fake/{mod_id}",
    )
    if last_known_path:
        db.update_mod_backup_snapshot(
            mod_id,
            last_known_path=last_known_path,
            folder_present=True,
            backup_metadata_json="{}",
        )


def _index(mod_id: str, *, deployed: bool) -> ModFilterIndex:
    return ModFilterIndex(
        mod_id=mod_id,
        display_name=f"Mod {mod_id}",
        steam_name="",
        notes="",
        game_name=GAME_FOLDER,
        favorite=False,
        deployed=deployed,
        has_offline=True,
        mtime=0.0,
        sort_name=f"mod {mod_id}",
    )


def test_save_record_includes_app_id_zero_under_game_folder(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """
    Library view: A (app_id=game) + B (app_id=0) both deployed under Anno folder.
    Snapshot must contain A+B so B is not extra under Record Filter.
    """
    db.upsert_game(GameInfo(app_id=ANNO, name=GAME_FOLDER, folder_name=GAME_FOLDER))
    lib = tmp_path / "mod"
    path_a = lib / GAME_FOLDER / "ModA"
    path_b = lib / GAME_FOLDER / "ModB"
    path_a.mkdir(parents=True)
    path_b.mkdir(parents=True)

    # A: correct Steam app_id, path optional (legacy)
    _mod(db, 1001, app_id=ANNO, deployed=True)
    # B: app_id=0 but lives under the game folder — Library shows it for Anno
    _mod(
        db,
        1002,
        app_id=0,
        deployed=True,
        last_known_path=str(path_b),
    )

    # Old app_id-only query would miss B.
    assert "1002" not in db.list_deployed_mod_ids_for_app(ANNO)
    assert set(db.list_deployed_mod_ids_for_library_game(ANNO, game_folder=GAME_FOLDER)) >= {
        "1001",
        "1002",
    }

    record = dr.create_or_update_record(
        ANNO,
        "当前环境",
        game_folder=GAME_FOLDER,
        library_root=lib,
        db=db,
    )
    recorded = dr.get_record_mod_ids(record.id, db=db)
    assert recorded == {"1001", "1002"}

    # Re-enter Record Filter relative calc: B must not be extra.
    recorded_fs = frozenset(recorded)
    for mid in ("1001", "1002"):
        status = compute_record_relative_status(
            _index(mid, deployed=True), recorded_fs
        )
        assert status is not None
        assert status.recorded_and_deployed
        assert record_relative_badge_label(status) is None

    # Sanity: a third deployed-only mod still shows extra.
    extra = compute_record_relative_status(
        _index("1003", deployed=True), recorded_fs
    )
    assert record_relative_badge_label(extra) == RECORD_STATUS_LABEL_EXTRA


def test_create_or_update_accepts_explicit_library_mod_ids(
    db: DatabaseManager,
) -> None:
    """UI may pass deployed ids from ``_card_entries`` directly."""
    db.upsert_game(GameInfo(app_id=ANNO, name=GAME_FOLDER, folder_name=GAME_FOLDER))
    _mod(db, 2001, app_id=ANNO, deployed=True)
    _mod(db, 2002, app_id=0, deployed=True)
    record = dr.create_or_update_record(
        ANNO, "手动集合", mod_ids=["2001", "2002"], db=db
    )
    assert dr.get_record_mod_ids(record.id, db=db) == {"2001", "2002"}
    # Relative under FILTER_DEPLOYMENT_RECORD would treat both as recorded.
    recorded = frozenset(dr.get_record_mod_ids(record.id, db=db))
    assert FILTER_DEPLOYMENT_RECORD  # architecture peer still exists
    assert record_relative_badge_label(
        compute_record_relative_status(_index("2002", deployed=True), recorded)
    ) is None
