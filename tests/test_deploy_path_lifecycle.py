"""Deploy path lifecycle — game / custom / source path errors must stay distinct."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from services.deploy import ModDeployer, _normalize_deploy_error
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
from services.deploy_path_lifecycle import (
    CUSTOM_DEPLOY_PATH_MISSING,
    DEPLOY_ERR_CUSTOM_PATH_MISSING_PREFIX,
    DEPLOY_ERR_GAME_INSTALL_MISSING_PREFIX,
    DEPLOY_ERR_GAME_MOD_PATH_MISSING_PREFIX,
    DEPLOY_ERR_IDENTITY_PREFIX,
    FORBIDDEN_VAGUE_MOD_PATH_COPY,
    GAME_CONFIG_PATH_MISSING,
    SOURCE_MOD_PATH_MISSING,
    custom_deploy_path_missing_error,
    resolve_entity_internal_id,
    validate_custom_deploy_target,
)
from ui.mod_detail_panel import humanize_deploy_error

ANNO = 916440
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "path_life.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed_mod(
    db: DatabaseManager,
    library: Path,
    *,
    mid: str,
    title: str,
    app_id: int,
    game_name: str,
    install: Path,
    mods: Path,
    custom: str = "",
    workspace_id: str = "",
    deploy_type: str = "folder_copy",
    create_source: bool = True,
) -> tuple[Path, str]:
    install.mkdir(parents=True, exist_ok=True)
    mods.mkdir(parents=True, exist_ok=True)
    db.upsert_game(
        GameInfo(app_id=app_id, name=game_name, folder_name=game_name)
    )
    db.update_game_deploy_config(
        app_id,
        name=game_name,
        install_path=str(install),
        mod_path=str(mods),
        deploy_type=deploy_type,
    )
    folder = library / game_name / title
    created = create_steam_test_mod(
        db, external_id=mid, title=title, app_id=app_id, game_name=game_name
    )
    pk = str(created.mod_id)
    if create_source:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "payload.bin").write_bytes(b"x")
        prove_managed_folder(
            db, folder, handle=pk, title=title, app_id=app_id, game_name=game_name
        )
    else:
        db.update_mod_identity_fields(
            pk,
            last_known_path=str(folder),
            folder_present=False,
        )
    if workspace_id:
        db.update_mod_identity_fields(pk, workspace_id=workspace_id)
    if custom:
        db.update_mod_user_metadata(pk, {"custom_deploy_path": custom})
    return folder, pk


def test_case1_game_mod_path_d_drive_missing_returns_game_config_code(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """case1: game.mod_path on missing D:-like root → GAME_CONFIG_PATH_MISSING."""
    library = tmp_path / "mod"
    install_ok = tmp_path / "F_Steam" / "Baldurs Gate 3"
    mods_missing = (
        tmp_path / "D_SteamLibrary" / "steamapps" / "common" / "Baldurs Gate 3" / "Mods"
    )
    install_ok.mkdir(parents=True, exist_ok=True)
    folder, pk = _seed_mod(
        db,
        library,
        mid="1339",
        title="Armory",
        app_id=BG3,
        game_name="Baldurs Gate 3",
        install=install_ok,
        mods=tmp_path / "placeholder_mods",
        workspace_id="14717",
        deploy_type="folder_copy",
    )
    db.update_game_deploy_config(
        BG3,
        install_path=str(install_ok),
        mod_path=str(mods_missing),
        deploy_type="folder_copy",
    )
    assert folder.is_dir()
    assert not mods_missing.exists()

    deployer = ModDeployer(library_root=library, db=db)
    ctx, err, _ = deployer._resolve_context(
        pk, require_target_exists=True, prepare_archives=False
    )
    assert ctx is None
    assert err is not None
    assert err.get("error_code") == GAME_CONFIG_PATH_MISSING
    assert err.get("error_kind") == GAME_CONFIG_PATH_MISSING
    assert err.get("path_field") == "mod_path"
    assert err.get("app_id") == BG3
    assert str(mods_missing) in str(err.get("configured_path") or "") or str(
        mods_missing
    ) in str(err.get("error") or "")
    msg = str(err.get("error") or "")
    assert msg.startswith(DEPLOY_ERR_GAME_MOD_PATH_MISSING_PREFIX)
    assert f"app_id={BG3}" in msg
    assert GAME_CONFIG_PATH_MISSING in msg
    assert FORBIDDEN_VAGUE_MOD_PATH_COPY not in msg
    assert "请检查游戏设置" not in msg
    assert humanize_deploy_error(msg) == msg
    assert _normalize_deploy_error(msg) == msg


def test_case2_custom_deploy_path_d_drive_missing_returns_custom_code(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """case2: custom_deploy_path missing → CUSTOM_DEPLOY_PATH_MISSING."""
    library = tmp_path / "mod"
    install_f = tmp_path / "F_SteamLibrary" / "steamapps" / "common" / "Anno 1800"
    mods_f = install_f / "mods"
    custom_d = (
        tmp_path
        / "D_SteamLibrary"
        / "steamapps"
        / "common"
        / "Anno 1800"
        / "Bin"
        / "Win64"
    )
    folder, pk = _seed_mod(
        db,
        library,
        mid="1238",
        title="anno1800-mod-loader",
        app_id=ANNO,
        game_name="Anno 1800",
        install=install_f,
        mods=mods_f,
        custom=str(custom_d),
        workspace_id="17864251756563492",
    )
    assert install_f.is_dir()
    assert mods_f.is_dir()
    assert folder.is_dir()
    assert not custom_d.exists()
    assert not custom_d.parent.exists()

    deployer = ModDeployer(library_root=library, db=db)
    ctx, err, _ = deployer._resolve_context(
        pk, require_target_exists=True, prepare_archives=False
    )
    assert ctx is None
    assert err is not None
    assert err.get("error_code") == CUSTOM_DEPLOY_PATH_MISSING
    assert err.get("error_kind") == CUSTOM_DEPLOY_PATH_MISSING
    assert err.get("path_field") == "custom_deploy_path"
    assert err.get("app_id") == ANNO
    msg = str(err.get("error") or "")
    assert msg.startswith(DEPLOY_ERR_CUSTOM_PATH_MISSING_PREFIX)
    assert CUSTOM_DEPLOY_PATH_MISSING in msg
    assert f"app_id={ANNO}" in msg
    assert FORBIDDEN_VAGUE_MOD_PATH_COPY not in msg
    assert DEPLOY_ERR_GAME_INSTALL_MISSING_PREFIX not in msg
    assert DEPLOY_ERR_GAME_MOD_PATH_MISSING_PREFIX not in msg
    assert humanize_deploy_error(msg) == msg
    assert _normalize_deploy_error(msg) == msg


def test_case3_source_mod_missing_returns_source_code(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """case3: source mod directory missing → SOURCE_MOD_PATH_MISSING."""
    library = tmp_path / "mod"
    install = tmp_path / "F" / "Baldurs Gate 3"
    mods = install / "Mods"
    install.mkdir(parents=True)
    mods.mkdir(parents=True)
    ghost = library / "Baldurs Gate 3" / "GhostMod"
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
    created = create_steam_test_mod(
        db, external_id="7100", title="GhostMod", app_id=BG3, game_name="Baldurs Gate 3"
    )
    pk = str(created.mod_id)
    db.update_mod_identity_fields(
        pk,
        workspace_id="14717",
        last_known_path=str(ghost),
        folder_present=False,
    )
    assert not ghost.exists()

    deployer = ModDeployer(library_root=library, db=db)
    ctx, err, _ = deployer._resolve_context(
        pk, require_target_exists=True, prepare_archives=False
    )
    assert ctx is None
    assert err is not None
    assert err.get("error_code") == SOURCE_MOD_PATH_MISSING
    assert err.get("error_kind") == SOURCE_MOD_PATH_MISSING
    assert err.get("path_field") == "source"
    msg = str(err.get("error") or "")
    assert SOURCE_MOD_PATH_MISSING in msg
    assert f"internal_id={pk}" in msg
    assert FORBIDDEN_VAGUE_MOD_PATH_COPY not in msg
    assert GAME_CONFIG_PATH_MISSING not in msg
    assert CUSTOM_DEPLOY_PATH_MISSING not in msg
    assert humanize_deploy_error(msg) == msg


def test_stale_custom_path_d_drive_does_not_blame_game_f(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Regression alias of case2 with Anno + live F: game roots."""
    test_case2_custom_deploy_path_d_drive_missing_returns_custom_code(db, tmp_path)


def test_game_install_missing_is_distinct_from_custom(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "missing_install"
    mods = tmp_path / "missing_mods"
    _folder, pk = _seed_mod(
        db,
        library,
        mid="9001",
        title="PlainAnnoMod",
        app_id=ANNO,
        game_name="Anno 1800",
        install=tmp_path / "placeholder_created_by_seed",
        mods=tmp_path / "placeholder_mods",
    )
    db.update_game_deploy_config(
        ANNO,
        install_path=str(install),
        mod_path=str(mods),
        deploy_type="folder_copy",
    )
    db.update_mod_user_metadata(pk, {"custom_deploy_path": ""})

    deployer = ModDeployer(library_root=library, db=db)
    ctx, err, _ = deployer._resolve_context(
        pk, require_target_exists=True, prepare_archives=False
    )
    assert ctx is None
    assert err is not None
    assert err.get("error_code") == GAME_CONFIG_PATH_MISSING
    msg = str(err.get("error") or "")
    assert msg.startswith(DEPLOY_ERR_GAME_INSTALL_MISSING_PREFIX)
    assert err.get("path_field") == "install_path"
    assert DEPLOY_ERR_CUSTOM_PATH_MISSING_PREFIX not in msg
    assert FORBIDDEN_VAGUE_MOD_PATH_COPY not in msg


def test_workspace_id_token_is_identity_failure_not_source(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "F" / "Anno 1800"
    mods = install / "mods"
    _seed_mod(
        db,
        library,
        mid="1238",
        title="anno1800-mod-loader",
        app_id=ANNO,
        game_name="Anno 1800",
        install=install,
        mods=mods,
        # Keep workspace non-digit so soft-resolve get_mod cannot treat a
        # bare workspace digit token as a live PK handle.
        workspace_id="ws-17864251756563492",
    )
    orphan_ws = "17864251756563492"
    mid, err = resolve_entity_internal_id(orphan_ws, db=db)
    assert mid == ""
    assert err is not None
    assert err.startswith(DEPLOY_ERR_IDENTITY_PREFIX)

    deployer = ModDeployer(library_root=library, db=db)
    ctx, deploy_err, _ = deployer._resolve_context(
        orphan_ws,
        require_target_exists=True,
        prepare_archives=False,
    )
    assert ctx is None
    assert deploy_err is not None
    msg = str(deploy_err.get("error") or "")
    assert msg.startswith(DEPLOY_ERR_IDENTITY_PREFIX)
    assert SOURCE_MOD_PATH_MISSING not in msg
    assert FORBIDDEN_VAGUE_MOD_PATH_COPY not in msg


def test_validate_custom_helper_and_error_shape(tmp_path: Path) -> None:
    missing = tmp_path / "D_drive" / "Anno" / "Bin" / "Win64"
    err = validate_custom_deploy_target(missing, app_id=ANNO)
    assert err == custom_deploy_path_missing_error(missing.expanduser(), app_id=ANNO)
    assert CUSTOM_DEPLOY_PATH_MISSING in (err or "")
    live = tmp_path / "live" / "target"
    live.parent.mkdir(parents=True)
    assert validate_custom_deploy_target(live) is None


def test_humanize_never_maps_path_lifecycle_to_vague_settings() -> None:
    msg = (
        f"{DEPLOY_ERR_GAME_MOD_PATH_MISSING_PREFIX}: D:/gone/Mods "
        f"(field=mod_path, app_id={BG3}, code={GAME_CONFIG_PATH_MISSING})"
    )
    assert humanize_deploy_error(msg) == msg
    assert humanize_deploy_error("Target mod directory does not exist") != (
        FORBIDDEN_VAGUE_MOD_PATH_COPY
    )
