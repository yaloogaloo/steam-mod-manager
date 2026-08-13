"""Phase 8: Deploy lifecycle — folder_copy closed loop with real temp dirs."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_status import (
    DEPLOY_BLOCKED_BACKUP_INVALID,
    DEPLOY_BLOCKED_FOLDER_MISSING,
    DEPLOY_ERR_TARGET_FOREIGN,
    DEPLOYMENT_DEPLOYED,
    DEPLOYMENT_NOT_DEPLOYED,
    DEPLOYMENT_OUTDATED,
    content_fingerprint,
    install_path_missing,
    resolve_deployment_status,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_status import (
    CONTENT_BACKUP_INVALID,
    CONTENT_FOLDER_MISSING,
    CONTENT_HEALTHY,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_life.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _make_mod(
    library: Path,
    *,
    game: str = "Game",
    folder: str = "TestMod",
    mod_id: str = "91001",
    app_id: int = 424242,
) -> Path:
    mod_dir = library / game / folder
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True)
    (mod_dir / "file1.txt").write_text("one", encoding="utf-8")
    (mod_dir / "file2.txt").write_text("two", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mod_id}",\n'
        f'  "title": "{folder}",\n'
        f'  "app_id": {app_id},\n'
        f'  "game_name": "{game}"\n'
        "}\n",
        encoding="utf-8",
    )
    (info / "secret.bin").write_bytes(b"manager-only")
    return mod_dir


def _setup_game(
    db: DatabaseManager,
    tmp_path: Path,
    *,
    app_id: int = 424242,
    install_ok: bool = True,
) -> tuple[Path, Path]:
    install = tmp_path / "fake_game"
    mods = install / "mods"
    mods.mkdir(parents=True)
    install_path = str(install if install_ok else tmp_path / "missing_game_install")
    if install_ok:
        install.mkdir(parents=True, exist_ok=True)
    db.update_game_deploy_config(
        app_id,
        name="Game",
        install_path=install_path,
        mod_path=str(mods),
        deploy_type="folder_copy",
    )
    return install, mods


def _register_mod(
    db: DatabaseManager,
    *,
    mod_id: str = "91001",
    app_id: int = 424242,
    content_status: str = CONTENT_HEALTHY,
    folder_present: bool = True,
    path: str = "",
) -> None:
    db.upsert_mod(
        ModMetadata(
            published_file_id=mod_id,
            title="TestMod",
            app_id=app_id,
            game_name="Game",
        )
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=content_status,
        folder_present=folder_present,
        last_known_path=path,
        library_status=content_status,
    )


# ---------------------------------------------------------------------------
# Cases 1–10
# ---------------------------------------------------------------------------


def test_case1_deploy_creates_target(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    _install, mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library)
    _register_mod(db, path=str(mod_dir))

    result = ModDeployer(library_root=library, db=db).deploy_mod("91001")
    assert result["success"] is True
    target = mods / "TestMod"
    assert target.is_dir()
    assert (target / "file1.txt").read_text(encoding="utf-8") == "one"
    assert not (target / ".info").exists()
    assert (mod_dir / "file1.txt").is_file()  # Library untouched


def test_case2_deployment_status_deployed(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    _setup_game(db, tmp_path)
    mod_dir = _make_mod(library)
    _register_mod(db, path=str(mod_dir))
    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("91001")["success"] is True
    assert deployer.deployment_status("91001") == DEPLOYMENT_DEPLOYED
    info = db.get_mod_deploy_info("91001")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED


def test_case3_library_change_marks_outdated(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    _setup_game(db, tmp_path)
    mod_dir = _make_mod(library)
    _register_mod(db, path=str(mod_dir))
    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("91001")["success"] is True
    time.sleep(0.05)
    (mod_dir / "file1.txt").write_text("changed", encoding="utf-8")
    assert deployer.deployment_status("91001") == DEPLOYMENT_OUTDATED


def test_case4_redeploy_updates_and_clears_outdated(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    _install, mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library)
    _register_mod(db, path=str(mod_dir))
    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("91001")["success"] is True
    (mod_dir / "file1.txt").write_text("v2", encoding="utf-8")
    assert deployer.deployment_status("91001") == DEPLOYMENT_OUTDATED
    out = deployer.redeploy_mod("91001")
    assert out["success"] is True
    assert (mods / "TestMod" / "file1.txt").read_text(encoding="utf-8") == "v2"
    assert deployer.deployment_status("91001") == DEPLOYMENT_DEPLOYED


def test_case5_foreign_target_is_conflict(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    _install, mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library)
    _register_mod(db, path=str(mod_dir))
    foreign = mods / "TestMod"
    foreign.mkdir(parents=True)
    (foreign / "other.txt").write_text("not ours", encoding="utf-8")

    result = ModDeployer(library_root=library, db=db).deploy_mod("91001")
    assert result["success"] is False
    assert result["error"] == DEPLOY_ERR_TARGET_FOREIGN
    assert result.get("deployment_status") == "conflict"
    assert (foreign / "other.txt").read_text(encoding="utf-8") == "not ours"
    assert not (foreign / "file1.txt").exists()


def test_case6_undeploy_removes_target_keeps_library(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    _install, mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library)
    _register_mod(db, path=str(mod_dir))
    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("91001")["success"] is True
    target = mods / "TestMod"
    assert target.is_dir()
    assert deployer.undeploy_mod("91001")["success"] is True
    assert not (target / "file1.txt").exists()
    assert (mod_dir / "file1.txt").is_file()
    info = db.get_mod_deploy_info("91001")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_NOT_DEPLOYED
    assert deployer.deployment_status("91001") == DEPLOYMENT_NOT_DEPLOYED


def test_case7_folder_missing_blocks_deploy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    _setup_game(db, tmp_path)
    _register_mod(
        db,
        content_status=CONTENT_FOLDER_MISSING,
        folder_present=False,
        path=str(library / "Game" / "Gone"),
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod("91001")
    assert result["success"] is False
    assert result["error"] == DEPLOY_BLOCKED_FOLDER_MISSING


def test_case8_backup_invalid_blocks_deploy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    _setup_game(db, tmp_path)
    mod_dir = _make_mod(library)
    _register_mod(
        db,
        content_status=CONTENT_BACKUP_INVALID,
        folder_present=False,
        path=str(mod_dir),
    )
    # Even if folder exists on disk, sticky content_status must block
    result = ModDeployer(library_root=library, db=db).deploy_mod("91001")
    assert result["success"] is False
    assert result["error"] == DEPLOY_BLOCKED_BACKUP_INVALID


def test_case9_relocate_restores_deploy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    _setup_game(db, tmp_path)
    mod_dir = _make_mod(library)
    _register_mod(
        db,
        content_status=CONTENT_FOLDER_MISSING,
        folder_present=False,
        path=str(library / "Game" / "OldPath"),
    )
    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("91001")["success"] is False

    # Simulate successful relocate: point back to real folder + healthy
    db.update_mod_identity_fields(
        "91001",
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=str(mod_dir),
        library_status=CONTENT_HEALTHY,
    )
    out = deployer.deploy_mod("91001")
    assert out["success"] is True
    assert deployer.deployment_status("91001") == DEPLOYMENT_DEPLOYED


def test_case10_missing_install_warns_without_touching_content_status(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    install, mods = _setup_game(db, tmp_path, install_ok=False)
    mod_dir = _make_mod(library)
    _register_mod(db, path=str(mod_dir), content_status=CONTENT_HEALTHY)

    cfg = db.get_game_deploy_config(424242)
    assert cfg is not None
    assert install_path_missing(cfg.install_path) is True
    # Mod install dir (mods/) still exists — deploy works; content_status unchanged
    assert mods.is_dir()
    result = ModDeployer(library_root=library, db=db).deploy_mod("91001")
    assert result["success"] is True
    row = db.get_mod_backup_row("91001")
    assert str(row.get("content_status") or "") == CONTENT_HEALTHY
    del install


def test_real_scenario_library_and_game_isolated(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """End-to-end: deploy → edit → redeploy → undeploy → delete library."""
    library = tmp_path / "library"
    _install, mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library)
    _register_mod(db, path=str(mod_dir))
    deployer = ModDeployer(library_root=library, db=db)

    assert deployer.deploy_mod("91001")["success"] is True
    target = mods / "TestMod"
    assert (target / "file1.txt").read_text(encoding="utf-8") == "one"
    fp1 = content_fingerprint(mod_dir)

    (mod_dir / "file1.txt").write_text("mutated", encoding="utf-8")
    assert content_fingerprint(mod_dir) != fp1
    assert resolve_deployment_status(
        "91001", library_root=library, db=db, managed_path=mod_dir
    ) == DEPLOYMENT_OUTDATED

    assert deployer.redeploy_mod("91001")["success"] is True
    assert (target / "file1.txt").read_text(encoding="utf-8") == "mutated"

    assert deployer.undeploy_mod("91001")["success"] is True
    assert not (target / "file1.txt").exists()
    assert (mod_dir / "file1.txt").is_file()

    # Delete Library only — must not resurrect / touch game targets
    import shutil

    shutil.rmtree(mod_dir)
    assert not mod_dir.exists()
    assert mods.is_dir()
    assert list(mods.iterdir()) == [] or not (mods / "TestMod" / "file1.txt").exists()
