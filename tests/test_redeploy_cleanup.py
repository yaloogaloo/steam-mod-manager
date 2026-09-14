"""Redeploy must remove files that disappeared from the new source set."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_TYPE_FOLDER_COPY,
    DatabaseManager,
)
from services.deploy import ModDeployer
from services.deploy_rules import load_manifest


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "redeploy.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed(
    db: DatabaseManager,
    library: Path,
    *,
    mid: str,
    title: str,
    files: dict[str, str],
    game: str = "G",
    app_id: int = 42,
) -> tuple[Path, str]:
    mod = library / game / title
    mod.mkdir(parents=True)
    for name, text in files.items():
        (mod / name).write_text(text, encoding="utf-8")
    created = create_steam_test_mod(
        db, external_id=mid, title=title, app_id=app_id, game_name=game
    )
    pk = prove_managed_folder(
        db,
        mod,
        handle=created.mod_id,
        title=title,
        app_id=app_id,
        game_name=game,
    )
    return mod, pk


def test_redeploy_removes_stale_files_and_rewrites_manifest(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()

    db.update_game_deploy_config(
        42, name="G", mod_path=str(mods_root), deploy_type=DEPLOY_TYPE_FOLDER_COPY
    )
    mod, pk = _seed(
        db,
        library,
        mid="96001",
        title="ShrinkMod",
        files={"A.txt": "A", "B.txt": "B"},
    )

    deployer = ModDeployer(library_root=library, db=db)
    first = deployer.deploy_mod("96001")
    assert first["success"] is True

    target = mods_root / "ShrinkMod"
    assert (target / "A.txt").is_file()
    assert (target / "B.txt").is_file()
    old_man = load_manifest(mod)
    assert old_man is not None
    assert len(old_man.files) == 2

    # New version drops B.txt
    (mod / "B.txt").unlink()

    red = deployer.redeploy_mod("96001")
    assert red["success"] is True
    assert (target / "A.txt").is_file()
    assert not (target / "B.txt").exists()

    new_man = load_manifest(mod)
    assert new_man is not None
    assert len(new_man.files) == 1
    assert new_man.files[0].target.endswith("A.txt")

    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED


def test_redeploy_aborts_when_undeploy_fails(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    db.update_game_deploy_config(42, name="G", mod_path=str(mods_root))
    _seed(db, library, mid="96002", title="M", files={"x.txt": "x"})

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("96002")["success"]

    monkeypatch.setattr(
        deployer,
        "undeploy_mod",
        lambda *_a, **_k: {"success": False, "error": "simulated undeploy fail"},
    )
    red = deployer.redeploy_mod("96002")
    assert red["success"] is False
    assert "重新部署中止" in red["error"]
