"""Mod status model reduction — delete old permanent status tokens."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.content_status_eval import evaluate_content_status
from services.library_status import (
    CONTENT_CONTENT_MISSING,
    CONTENT_HEALTHY,
    compute_content_status,
)
from services.status_authority import (
    DELETED_CONTENT_STATUS_TOKENS,
    IDENTITY_STATUS_CONFLICT,
    STATUS_MODEL_CLEANUP_V2_FLAG,
    SUPPORTED_CONTENT_STATUSES,
    normalize_content_axis,
)
from services.status_recovery import run_status_model_cleanup_v2
from services.user_annotation import set_conflict_annotation
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
from ui.library_query import (
    FILTER_CONFLICT,
    FILTER_CONTENT_MISSING,
    RECORD_STATUS_LABEL_EXTRA,
    RECORD_STATUS_LABEL_MISSING,
    ModFilterIndex,
    RecordRelativeStatus,
    matches_status_filter,
    record_relative_badge_label,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "status_reduction.db")
    yield manager
    DatabaseManager.reset_instance()


def _index(**kwargs) -> ModFilterIndex:
    base = dict(
        mod_id="1",
        display_name="X",
        steam_name="",
        notes="",
        game_name="G",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=0.0,
        sort_name="X",
        content_status=CONTENT_HEALTHY,
        identity_status="ok",
        conflict=False,
        conflict_status="none",
        enabled=True,
    )
    base.update(kwargs)
    return ModFilterIndex(**base)


def test_no_illegal_permanent_content_statuses() -> None:
    assert SUPPORTED_CONTENT_STATUSES == (CONTENT_HEALTHY, CONTENT_CONTENT_MISSING)
    for token in (
        "folder_missing",
        "metadata_missing",
        "backup_invalid",
        "identity_conflict",
    ):
        assert token in DELETED_CONTENT_STATUS_TOKENS
        assert token not in SUPPORTED_CONTENT_STATUSES


def test_content_status_only_healthy_or_content_missing() -> None:
    assert evaluate_content_status(folder_present=False) == CONTENT_CONTENT_MISSING
    assert (
        compute_content_status(
            folder_present=True,
            backup_status="invalid",
            metadata_missing=True,
        )
        == CONTENT_HEALTHY
    )
    assert normalize_content_axis("folder_missing") == CONTENT_HEALTHY
    assert normalize_content_axis("backup_invalid") == CONTENT_HEALTHY
    assert normalize_content_axis("identity_conflict") == CONTENT_HEALTHY


def test_writer_rejects_deleted_tokens(db: DatabaseManager) -> None:
    db.update_game_deploy_config(1, name="G")
    created = create_steam_test_mod(db, external_id="501", title="T", app_id=1)
    pk = str(created.mod_id)
    with pytest.raises(ValueError, match="illegal content_status"):
        db.update_mod_content_status(pk, content_status="folder_missing")
    with pytest.raises(ValueError, match="illegal content_status"):
        db.update_mod_content_status(pk, content_status="backup_invalid")


def test_identity_status_does_not_create_mod_conflict() -> None:
    idx = _index(
        content_status=CONTENT_HEALTHY,
        identity_status=IDENTITY_STATUS_CONFLICT,
        conflict_status="none",
    )
    assert not matches_status_filter(idx, FILTER_CONFLICT)
    assert matches_status_filter(
        _index(conflict_status="conflict"), FILTER_CONFLICT
    )


def test_backup_does_not_affect_mod_status() -> None:
    assert (
        compute_content_status(folder_present=True, backup_status="invalid")
        == CONTENT_HEALTHY
    )
    assert not matches_status_filter(
        _index(content_status=CONTENT_HEALTHY), "backup_invalid"
    )


def test_deleted_filters_never_match() -> None:
    assert not matches_status_filter(
        _index(content_status="folder_missing"), "folder_missing"
    )
    assert not matches_status_filter(
        _index(content_status="backup_invalid"), "backup_invalid"
    )
    assert matches_status_filter(
        _index(content_status=CONTENT_CONTENT_MISSING), FILTER_CONTENT_MISSING
    )


def test_cleanup_v2_reevaluates_without_mapping(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "Game" / "Alive"
    folder.mkdir(parents=True)
    (folder / "payload.bin").write_bytes(b"x")
    db.update_game_deploy_config(1, name="Game")
    created_alive = create_steam_test_mod(db, external_id="601", title="Alive", app_id=1)
    pk_alive = str(created_alive.mod_id)
    prove_managed_folder(
        db, folder, handle=pk_alive, title="Alive", app_id=1, game_name="Game"
    )
    created_gone = create_steam_test_mod(db, external_id="602", title="Gone", app_id=1)
    pk_gone = str(created_gone.mod_id)
    db.update_mod_identity_fields(
        pk_gone,
        folder_present=False,
        last_known_path=str(library / "Game" / "Gone"),
    )
    db.update_mod_deploy_status(pk_alive, deploy_status="deployed")
    set_conflict_annotation(pk_alive, db=db)

    with db._lock:
        db._conn.execute(
            "UPDATE mods SET content_status = ?, deploy_status = ? WHERE mod_id = ?",
            ("folder_missing", "deployed", int(pk_alive)),
        )
        db._conn.execute(
            "UPDATE mods SET content_status = ? WHERE mod_id = ?",
            ("backup_invalid", int(pk_gone)),
        )
        db._conn.execute(
            "DELETE FROM schema_flags WHERE flag = ?",
            (STATUS_MODEL_CLEANUP_V2_FLAG,),
        )
        db._conn.commit()

    deploy_before = db.get_mod_deploy_info(pk_alive)
    conflict_before = str(
        (db.get_mod_backup_row(pk_alive) or {}).get("conflict_status") or ""
    )

    result = run_status_model_cleanup_v2(db, library, force=True)
    assert result.content_reevaluated >= 1

    row1 = db.get_mod_backup_row(pk_alive) or {}
    row2 = db.get_mod_backup_row(pk_gone) or {}
    assert str(row1.get("content_status") or "") in SUPPORTED_CONTENT_STATUSES
    assert str(row2.get("content_status") or "") == CONTENT_CONTENT_MISSING
    assert str(row1.get("content_status") or "") != "folder_missing"
    assert str(row2.get("content_status") or "") != "backup_invalid"

    deploy_after = db.get_mod_deploy_info(pk_alive)
    assert deploy_before is not None and deploy_after is not None
    assert deploy_before.deploy_status == deploy_after.deploy_status == "deployed"
    assert (
        str((db.get_mod_backup_row(pk_alive) or {}).get("conflict_status") or "")
        == conflict_before
    )


def test_deploy_does_not_write_mod_content_status() -> None:
    from services import deploy as deploy_mod

    src = inspect.getsource(deploy_mod)
    assert "update_mod_content_status" not in src


def test_deployment_record_overlay_labels() -> None:
    missing = RecordRelativeStatus(recorded=True, deployed=False)
    extra = RecordRelativeStatus(recorded=False, deployed=True)
    assert record_relative_badge_label(missing) == RECORD_STATUS_LABEL_MISSING
    assert record_relative_badge_label(extra) == RECORD_STATUS_LABEL_EXTRA
    assert RECORD_STATUS_LABEL_MISSING not in SUPPORTED_CONTENT_STATUSES


def test_card_does_not_compose_legacy_status_tokens() -> None:
    from ui import mod_card as mod_card_mod

    src = inspect.getsource(mod_card_mod)
    assert "folder_missing" not in src
    assert "backup_invalid" not in src
