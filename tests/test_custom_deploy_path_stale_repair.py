"""Custom deploy path stale repair — clear / optional rebind, not identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from services.custom_deploy_path_stale import (
    ACTION_CLEAR_TO_INHERIT,
    STALE_DRIVE_MIGRATE,
    STALE_MISSING,
    apply_clear_custom_deploy_path,
    classify_custom_deploy_path,
    remapped_under_install,
)
from services.deploy import ModDeployer
from services.deploy_path_lifecycle import CUSTOM_DEPLOY_PATH_MISSING
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME

ANNO = 916440


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "stale_custom.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(
    db: DatabaseManager,
    library: Path,
    *,
    mid: str,
    install: Path,
    mods: Path,
    custom: str = "",
    title: str = "Loader",
) -> Path:
    install.mkdir(parents=True, exist_ok=True)
    mods.mkdir(parents=True, exist_ok=True)
    db.upsert_game(GameInfo(app_id=ANNO, name="Anno 1800", folder_name="Anno 1800"))
    db.update_game_deploy_config(
        ANNO,
        name="Anno 1800",
        install_path=str(install),
        mod_path=str(mods),
        deploy_type="folder_copy",
    )
    folder = library / "Anno 1800" / title
    folder.mkdir(parents=True)
    (folder / "payload.bin").write_bytes(b"x")
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        f'{{"internal_id":"{mid}","app_id":{ANNO},"title":"{title}"}}',
        encoding="utf-8",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id=mid,
            title=title,
            app_id=ANNO,
            game_name="Anno 1800",
            managed_path=str(folder),
        )
    )
    db.update_mod_identity_fields(
        mid,
        internal_id=mid,
        workspace_id="17864251756563492",
        last_known_path=str(folder.resolve()),
        folder_present=True,
    )
    if custom:
        db.update_mod_user_metadata(mid, {"custom_deploy_path": custom})
    return folder


def test_case1_live_custom_deploy_path_deploys(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "F_Steam" / "steamapps" / "common" / "Anno 1800"
    mods = install / "mods"
    custom = tmp_path / "custom_out"
    custom.mkdir(parents=True)
    _seed(db, library, mid="1238", install=install, mods=mods, custom=str(custom))

    finding = classify_custom_deploy_path(
        mod_id="1238",
        app_id=ANNO,
        custom_deploy_path=str(custom),
        game_install_path=str(install),
        game_mod_path=str(mods),
    )
    assert finding is None

    result = ModDeployer(library_root=library, db=db).deploy_mod("1238")
    assert result.get("success") is True, result
    assert (custom / "payload.bin").is_file()


def test_case2_missing_custom_returns_custom_deploy_path_missing(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "F_Steam" / "steamapps" / "common" / "Anno 1800"
    mods = install / "mods"
    missing = tmp_path / "nope" / "Bin" / "Win64"
    _seed(db, library, mid="1238", install=install, mods=mods, custom=str(missing))

    ctx, err, _ = ModDeployer(library_root=library, db=db)._resolve_context(
        "1238", require_target_exists=True, prepare_archives=False
    )
    assert ctx is None
    assert err is not None
    assert err.get("error_code") == CUSTOM_DEPLOY_PATH_MISSING or err.get(
        "error_kind"
    ) == CUSTOM_DEPLOY_PATH_MISSING
    assert "Mod自定义部署路径不存在" in str(err.get("error") or "")
    assert "请检查游戏设置" not in str(err.get("error") or "")


def test_case3_old_drive_letter_detected_as_stale_migrate(
    db: DatabaseManager, tmp_path: Path
) -> None:
    install_f = tmp_path / "F_SteamLibrary" / "steamapps" / "common" / "Anno 1800"
    mods = install_f / "mods"
    # Stale D: style path (never created)
    custom_d = (
        tmp_path
        / "D_SteamLibrary"
        / "steamapps"
        / "common"
        / "Anno 1800"
        / "Bin"
        / "Win64"
    )
    finding = classify_custom_deploy_path(
        mod_id="1238",
        internal_id="1238",
        app_id=ANNO,
        workspace_id="17864251756563492",
        custom_deploy_path=str(custom_d),
        game_install_path=str(install_f),
        game_mod_path=str(mods),
    )
    assert finding is not None
    assert finding.code == STALE_DRIVE_MIGRATE
    assert finding.recommended_action == ACTION_CLEAR_TO_INHERIT
    remapped = remapped_under_install(custom_d, install_f)
    assert remapped is not None
    assert remapped == install_f / "Bin" / "Win64"
    assert finding.remapped_candidate.replace("\\", "/") == str(remapped).replace(
        "\\", "/"
    )


def test_case4_repair_clear_then_deploy_uses_game_mod_path(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "F_SteamLibrary" / "steamapps" / "common" / "Anno 1800"
    mods = install / "mods"
    custom_d = (
        tmp_path
        / "D_SteamLibrary"
        / "steamapps"
        / "common"
        / "Anno 1800"
        / "Bin"
        / "Win64"
    )
    _seed(
        db,
        library,
        mid="1238",
        install=install,
        mods=mods,
        custom=str(custom_d),
        title="anno1800-mod-loader",
    )

    # Before repair: blocked
    ctx, err, _ = ModDeployer(library_root=library, db=db)._resolve_context(
        "1238", require_target_exists=True, prepare_archives=False
    )
    assert ctx is None
    assert err is not None
    assert CUSTOM_DEPLOY_PATH_MISSING in str(
        err.get("error_code") or err.get("error_kind") or err.get("error") or ""
    )

    assert apply_clear_custom_deploy_path(db, "1238") is True
    info = db.get_mod_display_info("1238")
    assert info is not None
    assert str(info.custom_deploy_path or "") == ""

    # After clear: inherits game roots (Anno → install/mods), no custom override
    result = ModDeployer(library_root=library, db=db).deploy_mod("1238")
    assert result.get("success") is True, result
    # Must land under game.mod_path, not under remapped Bin/Win64
    assert not (install / "Bin" / "Win64").exists() or not any(
        (install / "Bin" / "Win64").iterdir()
    )
    # Folder-copy / anno strategy places under mods/
    deployed = list(mods.rglob("payload.bin"))
    assert deployed, f"expected payload under {mods}, result={result}"


def test_missing_without_install_match_is_stale_missing() -> None:
    finding = classify_custom_deploy_path(
        mod_id="9",
        app_id=ANNO,
        custom_deploy_path=r"Z:\totally\gone\path",
        game_install_path=r"F:\SteamLibrary\steamapps\common\Anno 1800",
        game_mod_path=r"F:\SteamLibrary\steamapps\common\Anno 1800\mods",
    )
    assert finding is not None
    assert finding.code == STALE_MISSING
    assert finding.recommended_action == ACTION_CLEAR_TO_INHERIT
