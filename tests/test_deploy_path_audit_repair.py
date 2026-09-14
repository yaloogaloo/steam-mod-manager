"""Path Lifecycle Phase 2 — audit stale paths and safe repair."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from services.deploy import ModDeployer
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
from services.deploy_path_audit import (
    CUSTOM_DEPLOY_PATH_MISSING,
    GAME_CONFIG_PATH_MISSING,
    REPAIR_CLEAR_CUSTOM,
    REPAIR_IGNORE,
    REPAIR_UPDATE_CUSTOM,
    REPAIR_UPDATE_GAME,
    audit_deploy_paths,
    reaudit_after_repair,
    repair_deploy_path,
    reset_path_repair_history,
)
from services.deploy_path_lifecycle import CUSTOM_DEPLOY_PATH_MISSING as _CDM

assert CUSTOM_DEPLOY_PATH_MISSING == _CDM

ANNO = 916440
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    reset_path_repair_history()
    manager = DatabaseManager.instance(tmp_path / "path_audit.db")
    yield manager
    reset_path_repair_history()
    DatabaseManager.reset_instance()


def _seed_bg3_mod(
    db: DatabaseManager,
    library: Path,
    *,
    mid: str,
    install: Path,
    mods: Path,
    custom: str = "",
) -> tuple[Path, str]:
    install.mkdir(parents=True, exist_ok=True)
    mods.mkdir(parents=True, exist_ok=True)
    db.upsert_game(
        GameInfo(app_id=BG3, name="Baldurs Gate 3", folder_name="Baldurs Gate 3")
    )
    db.update_game_deploy_config(
        BG3,
        name="Baldurs Gate 3",
        install_path=str(install),
        mod_path=str(mods),
        deploy_type="folder_copy",
    )
    folder = library / "Baldurs Gate 3" / f"Mod_{mid}"
    folder.mkdir(parents=True)
    (folder / "payload.bin").write_bytes(b"x")
    created = create_steam_test_mod(
        db, external_id=mid, title=f"Mod_{mid}", app_id=BG3, game_name="Baldurs Gate 3"
    )
    pk = str(created.mod_id)
    prove_managed_folder(
        db, folder, handle=pk, title=f"Mod_{mid}", app_id=BG3, game_name="Baldurs Gate 3"
    )
    db.update_mod_identity_fields(pk, workspace_id="14717")
    if custom:
        db.update_mod_user_metadata(pk, {"custom_deploy_path": custom})
    return folder, pk


def test_case1_clear_stale_custom_inherits_game_mod_path(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """case1: stale custom_deploy_path → clear → inherit game.mod_path."""
    library = tmp_path / "mod"
    install = tmp_path / "F" / "Baldurs Gate 3"
    mods = install / "Mods"
    stale_custom = (
        tmp_path / "D_SteamLibrary" / "steamapps" / "common" / "Baldurs Gate 3" / "Mods"
    )
    folder, pk = _seed_bg3_mod(
        db, library, mid="1339", install=install, mods=mods, custom=str(stale_custom)
    )
    assert folder.is_dir()
    assert not stale_custom.exists()

    report = audit_deploy_paths(db=db)
    customs = [
        f
        for f in report.findings
        if f.path_field == "custom_deploy_path" and f.internal_id == pk
    ]
    assert len(customs) == 1
    assert customs[0].error_code == CUSTOM_DEPLOY_PATH_MISSING
    assert customs[0].app_id == BG3

    result = repair_deploy_path(
        action=REPAIR_CLEAR_CUSTOM,
        db=db,
        internal_id=pk,
    )
    assert result.success
    assert result.record is not None
    assert result.record.before_path
    assert result.record.after_path == ""
    assert result.record.timestamp
    assert result.still_missing is False

    display = db.get_mod_display_info(pk)
    assert display is not None
    assert (display.custom_deploy_path or "").strip() == ""
    assert db.get_mod(pk) is not None

    deployer = ModDeployer(library_root=library, db=db)
    ctx, err, _ = deployer._resolve_context(
        pk, require_target_exists=True, prepare_archives=False
    )
    assert err is None
    assert ctx is not None
    assert (ctx.custom_deploy_path or "").strip() == ""
    assert Path(ctx.config.mod_path).resolve() == mods.resolve()

    leftover = reaudit_after_repair(
        db=db, internal_id=pk, path_field="custom_deploy_path"
    )
    assert leftover == []


def test_case2_game_mod_path_stale_repair_revalidates(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """case2: stale game.mod_path → explicit repair → re-audit clean."""
    library = tmp_path / "mod"
    install = tmp_path / "F" / "Baldurs Gate 3"
    live_mods = install / "Mods"
    stale_mods = (
        tmp_path / "D_SteamLibrary" / "steamapps" / "common" / "Baldurs Gate 3" / "Mods"
    )
    _folder, pk = _seed_bg3_mod(db, library, mid="1340", install=install, mods=live_mods)
    db.update_game_deploy_config(
        BG3,
        install_path=str(install),
        mod_path=str(stale_mods),
        deploy_type="folder_copy",
    )
    assert not stale_mods.exists()
    assert live_mods.is_dir()

    report = audit_deploy_paths(db=db)
    game_hits = [
        f
        for f in report.findings
        if f.entity_kind == "game"
        and f.app_id == BG3
        and f.path_field == "mod_path"
    ]
    assert len(game_hits) == 1
    assert game_hits[0].error_code == GAME_CONFIG_PATH_MISSING
    assert str(stale_mods) in game_hits[0].configured_path.replace("/", "\\") or (
        str(stale_mods) in game_hits[0].configured_path
    )

    result = repair_deploy_path(
        action=REPAIR_UPDATE_GAME,
        db=db,
        app_id=BG3,
        path_field="mod_path",
        new_path=str(live_mods),
    )
    assert result.success
    assert result.record is not None
    assert result.record.before_path
    assert Path(result.record.after_path).resolve() == live_mods.resolve()
    assert result.record.timestamp
    assert result.still_missing is False

    cfg = db.get_game_deploy_config(BG3)
    assert cfg is not None
    assert Path(cfg.mod_path).resolve() == live_mods.resolve()

    leftover = reaudit_after_repair(db=db, app_id=BG3, path_field="mod_path")
    assert leftover == []

    deployer = ModDeployer(library_root=library, db=db)
    ctx, err, _ = deployer._resolve_context(
        pk, require_target_exists=True, prepare_archives=False
    )
    assert err is None
    assert ctx is not None


def test_case3_repair_does_not_modify_internal_id(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """case3: repair never rewrites internal_id / identity."""
    library = tmp_path / "mod"
    install = tmp_path / "F" / "Baldurs Gate 3"
    mods = install / "Mods"
    stale = tmp_path / "D" / "old" / "custom"
    _folder, pk = _seed_bg3_mod(
        db, library, mid="1341", install=install, mods=mods, custom=str(stale)
    )
    frozen = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET internal_id = ? WHERE mod_id = ?",
            (frozen, int(pk)),
        )
        db._conn.commit()

    before = db._conn.execute(
        "SELECT mod_id, internal_id, workspace_id FROM mods WHERE mod_id = ?",
        (int(pk),),
    ).fetchone()
    assert str(before["internal_id"]) == frozen
    assert str(before["workspace_id"]) == "14717"

    result = repair_deploy_path(
        action=REPAIR_CLEAR_CUSTOM, db=db, internal_id=pk
    )
    assert result.success

    after = db._conn.execute(
        "SELECT mod_id, internal_id, workspace_id, custom_deploy_path "
        "FROM mods WHERE mod_id = ?",
        (int(pk),),
    ).fetchone()
    assert str(after["mod_id"]) == pk
    assert str(after["internal_id"]) == frozen
    assert str(after["workspace_id"]) == "14717"
    assert str(after["custom_deploy_path"] or "") == ""

    live_custom = mods / "ArmoryOverride"
    live_custom.mkdir(parents=True)
    result2 = repair_deploy_path(
        action=REPAIR_UPDATE_CUSTOM,
        db=db,
        internal_id=pk,
        new_path=str(live_custom),
    )
    assert result2.success
    after2 = db._conn.execute(
        "SELECT mod_id, internal_id, workspace_id FROM mods WHERE mod_id = ?",
        (int(pk),),
    ).fetchone()
    assert str(after2["mod_id"]) == pk
    assert str(after2["internal_id"]) == frozen
    assert str(after2["workspace_id"]) == "14717"


def test_ignore_records_without_mutation(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "F" / "Baldurs Gate 3"
    mods = install / "Mods"
    stale = tmp_path / "D" / "gone"
    _folder, pk = _seed_bg3_mod(
        db, library, mid="1342", install=install, mods=mods, custom=str(stale)
    )
    before = db.get_mod_display_info(pk)
    assert before is not None
    before_path = (before.custom_deploy_path or "").strip()

    result = repair_deploy_path(
        action=REPAIR_IGNORE,
        db=db,
        internal_id=pk,
        path_field="custom_deploy_path",
    )
    assert result.success
    assert result.record is not None
    assert result.record.ignored is True
    assert result.record.before_path == before_path
    assert result.record.after_path == before_path
    assert result.record.timestamp

    after = db.get_mod_display_info(pk)
    assert after is not None
    assert (after.custom_deploy_path or "").strip() == before_path


def test_audit_skips_relative_and_empty_paths(
    db: DatabaseManager, tmp_path: Path
) -> None:
    db.upsert_game(GameInfo(app_id=BG3, name="Baldurs Gate 3", folder_name="BG3"))
    db.update_game_deploy_config(
        BG3,
        install_path="relative/not/absolute",
        mod_path="",
        deploy_type="folder_copy",
    )
    report = audit_deploy_paths(db=db)
    assert all(f.app_id != BG3 for f in report.findings) or all(
        f.path_field not in ("install_path", "mod_path") or f.app_id != BG3
        for f in report.findings
    )
    assert not any(
        f.app_id == BG3 and f.path_field == "install_path" for f in report.findings
    )
