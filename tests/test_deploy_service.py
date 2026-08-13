"""Phase 3: ModDeployer — folder_copy deploy service (no UI)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DatabaseManager,
)
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_svc.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _make_managed_mod(
    library: Path,
    *,
    game: str,
    folder: str,
    mod_id: str,
    app_id: int,
) -> Path:
    mod_dir = library / game / folder
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True)
    (mod_dir / "file1.txt").write_text("one", encoding="utf-8")
    (mod_dir / "file2.txt").write_text("two", encoding="utf-8")
    (info / "index.html").write_text("<html>offline</html>", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mod_id}",\n'
        f'  "title": "{folder}",\n'
        f'  "app_id": {app_id},\n'
        f'  "game_name": "{game}"\n'
        "}\n",
        encoding="utf-8",
    )
    # Nested content under .info must never appear in target
    (info / "secret.bin").write_bytes(b"secret")
    return mod_dir


def test_deploy_copies_files_and_updates_status(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    game_mods = tmp_path / "GameInstall" / "Mods"
    game_mods.mkdir(parents=True)

    _make_managed_mod(
        library, game="Palworld", folder="ModTest", mod_id="9001", app_id=1623730
    )
    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=str(tmp_path / "GameInstall"),
        mod_path=str(game_mods),
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="9001",
            title="ModTest",
            app_id=1623730,
            game_name="Palworld",
        )
    )

    deployer = ModDeployer(library_root=library, db=db)
    result = deployer.deploy_mod(9001)

    assert result["success"] is True
    assert result["mod_id"] == "9001"
    assert result["copied_files"] == 2
    target = Path(result["target"])
    assert target == game_mods / "ModTest"
    assert (target / "file1.txt").read_text(encoding="utf-8") == "one"
    assert (target / "file2.txt").read_text(encoding="utf-8") == "two"

    info = db.get_mod_deploy_info(9001)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED
    assert info.deploy_path == str(target)
    assert info.deploy_time


def test_deploy_ignores_info_dirs(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    game_mods = tmp_path / "Mods"
    game_mods.mkdir()
    _make_managed_mod(
        library, game="Game", folder="ModA", mod_id="9002", app_id=10
    )
    # Also add legacy info/
    legacy = library / "Game" / "ModA" / "info"
    legacy.mkdir()
    (legacy / "legacy.txt").write_text("nope", encoding="utf-8")

    db.update_game_deploy_config(10, name="Game", mod_path=str(game_mods))
    deployer = ModDeployer(library_root=library, db=db)
    result = deployer.deploy_mod("9002")

    assert result["success"] is True
    target = Path(result["target"])
    assert not (target / ".info").exists()
    assert not (target / "info").exists()
    assert (target / "file1.txt").is_file()


def test_deploy_fails_without_mod_path_config(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    _make_managed_mod(
        library, game="Game", folder="ModB", mod_id="9003", app_id=11
    )
    # Game row exists but mod_path empty
    db.update_game_deploy_config(11, name="Game", install_path=str(tmp_path), mod_path="")

    deployer = ModDeployer(library_root=library, db=db)
    result = deployer.deploy_mod(9003)
    assert result["success"] is False
    assert "部署目录" in result["error"] or "配置" in result["error"]


def test_deploy_fails_when_source_missing(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    library.mkdir()
    game_mods = tmp_path / "Mods"
    game_mods.mkdir()
    db.update_game_deploy_config(12, name="Game", mod_path=str(game_mods))
    db.upsert_mod(
        ModMetadata(
            published_file_id="9004",
            title="Ghost",
            app_id=12,
        )
    )

    deployer = ModDeployer(library_root=library, db=db)
    result = deployer.deploy_mod(9004)
    assert result["success"] is False
    assert "不存在" in result["error"]


def test_redeploy_preserves_unrelated_target_files(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Phase 8: owned target may be updated; extra non-manifest files stay."""
    library = tmp_path / "mod"
    game_mods = tmp_path / "Mods"
    game_mods.mkdir()
    _make_managed_mod(
        library, game="Game", folder="ModC", mod_id="9005", app_id=13
    )
    db.update_game_deploy_config(13, name="Game", mod_path=str(game_mods))

    deployer = ModDeployer(library_root=library, db=db)
    first = deployer.deploy_mod(9005)
    assert first["success"] is True

    target = Path(first["target"])
    (target / "user_keep.txt").write_text("keep-me", encoding="utf-8")

    result = deployer.redeploy_mod(9005)
    assert result["success"] is True

    assert (target / "user_keep.txt").read_text(encoding="utf-8") == "keep-me"
    assert (target / "file1.txt").read_text(encoding="utf-8") == "one"
    assert (target / "file2.txt").is_file()
    assert not (target / ".info").exists()
