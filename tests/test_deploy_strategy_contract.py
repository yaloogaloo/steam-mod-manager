"""Strategy contract tests for all registered deploy strategies."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.deploy_rules import (
    DEPLOY_TYPE_ANNO_1800,
    DEPLOY_TYPE_DUCKOV,
    DEPLOY_TYPE_FOLDER_COPY,
    DEPLOY_TYPE_PALWORLD_PAK,
    DEPLOY_TYPE_SLAY_THE_SPIRE,
    DEPLOY_TYPE_STARDEW_VALLEY,
    get_strategy,
    supported_deploy_types,
)
from services.deploy_rules.base import DeployContext
from services.deploy_rules.generic import FolderCopyStrategy


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "contract_all.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _make_ctx(
    tmp_path: Path,
    db: DatabaseManager,
    *,
    app_id: int,
    deploy_type: str,
    source: Path,
    install_path: str = "",
    mod_path: str = "",
) -> DeployContext:
    db.update_game_deploy_config(
        app_id,
        name=f"Game{app_id}",
        install_path=install_path,
        mod_path=mod_path,
        deploy_type=deploy_type,
    )
    cfg = db.get_game_deploy_config(app_id)
    assert cfg is not None
    return DeployContext(
        mod_id="10001",
        app_id=app_id,
        source=source,
        deploy_type=deploy_type,
        config=cfg,
        allowed_rel_paths=None,
        managed_path=source,
        custom_deploy_path="",
    )


def test_all_strategies_registered() -> None:
    types = set(supported_deploy_types())
    for key in (
        DEPLOY_TYPE_FOLDER_COPY,
        DEPLOY_TYPE_PALWORLD_PAK,
        DEPLOY_TYPE_ANNO_1800,
        DEPLOY_TYPE_SLAY_THE_SPIRE,
        DEPLOY_TYPE_STARDEW_VALLEY,
        DEPLOY_TYPE_DUCKOV,
    ):
        assert key in types
        assert get_strategy(key) is not None


def test_folder_copy_plan_and_deploy(tmp_path: Path, db: DatabaseManager) -> None:
    src = tmp_path / "lib" / "ModA"
    src.mkdir(parents=True)
    (src / "a.txt").write_text("x", encoding="utf-8")
    mods = tmp_path / "Mods"
    mods.mkdir()
    ctx = _make_ctx(
        tmp_path,
        db,
        app_id=1,
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        source=src,
        mod_path=str(mods),
    )
    strategy = FolderCopyStrategy()
    plan = strategy.plan(ctx)
    assert plan.success and plan.files
    result = strategy.deploy(ctx)
    assert result.success and result.manifest
    und = strategy.undeploy(ctx, result.manifest)
    assert und.success


def test_palworld_plan_with_pak(tmp_path: Path, db: DatabaseManager) -> None:
    src = tmp_path / "lib" / "PakMod"
    src.mkdir(parents=True)
    (src / "LogicMods").mkdir()
    (src / "LogicMods" / "x.pak").write_bytes(b"pak")
    install = tmp_path / "Palworld"
    (install / "Pal" / "Content" / "Paks").mkdir(parents=True)
    mods = tmp_path / "Mods"
    mods.mkdir()
    ctx = _make_ctx(
        tmp_path,
        db,
        app_id=1623730,
        deploy_type=DEPLOY_TYPE_PALWORLD_PAK,
        source=src,
        install_path=str(install),
        mod_path=str(mods),
    )
    strategy = get_strategy(DEPLOY_TYPE_PALWORLD_PAK, app_id=1623730)
    assert strategy is not None
    plan = strategy.plan(ctx)
    assert plan.success is True
    assert plan.files
    result = strategy.deploy(ctx)
    assert result.success is True


def test_duckov_requires_info_ini(tmp_path: Path, db: DatabaseManager) -> None:
    src = tmp_path / "lib" / "DuckMod"
    src.mkdir(parents=True)
    (src / "data.txt").write_text("x", encoding="utf-8")
    mods = tmp_path / "Mods"
    mods.mkdir()
    ctx = _make_ctx(
        tmp_path,
        db,
        app_id=3167020,
        deploy_type=DEPLOY_TYPE_DUCKOV,
        source=src,
        mod_path=str(mods),
    )
    strategy = get_strategy(DEPLOY_TYPE_DUCKOV, app_id=3167020)
    assert strategy is not None
    plan = strategy.plan(ctx)
    assert plan.success is False
    assert "info.ini" in (plan.error or "").lower() or "info.ini" in (plan.error or "")


def test_duckov_deploy_layout(tmp_path: Path, db: DatabaseManager) -> None:
    src = tmp_path / "lib" / "DuckMod"
    src.mkdir(parents=True)
    (src / "info.ini").write_text("[mod]\nname=x\n", encoding="utf-8")
    (src / "data.txt").write_text("x", encoding="utf-8")
    mods = tmp_path / "Mods"
    mods.mkdir()
    ctx = _make_ctx(
        tmp_path,
        db,
        app_id=3167020,
        deploy_type=DEPLOY_TYPE_DUCKOV,
        source=src,
        mod_path=str(mods),
    )
    strategy = get_strategy(DEPLOY_TYPE_DUCKOV, app_id=3167020)
    assert strategy is not None
    result = strategy.deploy(ctx)
    assert result.success is True
    target = Path(result.target)
    assert (target / "info.ini").is_file()
    assert not (target / "DuckMod" / "info.ini").exists()


def test_stardew_requires_manifest(tmp_path: Path, db: DatabaseManager) -> None:
    src = tmp_path / "lib" / "SdMod"
    src.mkdir(parents=True)
    (src / "dll.bin").write_bytes(b"x")
    mods = tmp_path / "Mods"
    mods.mkdir()
    ctx = _make_ctx(
        tmp_path,
        db,
        app_id=413150,
        deploy_type=DEPLOY_TYPE_STARDEW_VALLEY,
        source=src,
        mod_path=str(mods),
    )
    strategy = get_strategy(DEPLOY_TYPE_STARDEW_VALLEY, app_id=413150)
    assert strategy is not None
    plan = strategy.plan(ctx)
    assert plan.success is False


def test_stardew_deploy_with_manifest(tmp_path: Path, db: DatabaseManager) -> None:
    src = tmp_path / "lib" / "CoolMod"
    src.mkdir(parents=True)
    (src / "manifest.json").write_text('{"Name":"Cool"}', encoding="utf-8")
    (src / "CoolMod.dll").write_bytes(b"dll")
    mods = tmp_path / "Mods"
    mods.mkdir()
    ctx = _make_ctx(
        tmp_path,
        db,
        app_id=413150,
        deploy_type=DEPLOY_TYPE_STARDEW_VALLEY,
        source=src,
        mod_path=str(mods),
    )
    strategy = get_strategy(DEPLOY_TYPE_STARDEW_VALLEY, app_id=413150)
    assert strategy is not None
    result = strategy.deploy(ctx)
    assert result.success is True
    assert (mods / "CoolMod" / "manifest.json").is_file()


def test_sts_jars_to_mods(tmp_path: Path, db: DatabaseManager) -> None:
    src = tmp_path / "lib" / "JarMod"
    src.mkdir(parents=True)
    (src / "mod.jar").write_bytes(b"jar")
    install = tmp_path / "STS"
    install.mkdir()
    ctx = _make_ctx(
        tmp_path,
        db,
        app_id=646570,
        deploy_type=DEPLOY_TYPE_SLAY_THE_SPIRE,
        source=src,
        install_path=str(install),
    )
    strategy = get_strategy(DEPLOY_TYPE_SLAY_THE_SPIRE, app_id=646570)
    assert strategy is not None
    result = strategy.deploy(ctx)
    assert result.success is True
    assert (install / "mods" / "mod.jar").is_file()


def test_anno_folder_copy_into_mods(tmp_path: Path, db: DatabaseManager) -> None:
    src = tmp_path / "lib" / "AnnoMod"
    src.mkdir(parents=True)
    (src / "moddata.txt").write_text("x", encoding="utf-8")
    install = tmp_path / "Anno"
    install.mkdir()
    ctx = _make_ctx(
        tmp_path,
        db,
        app_id=916440,
        deploy_type=DEPLOY_TYPE_ANNO_1800,
        source=src,
        install_path=str(install),
    )
    strategy = get_strategy(DEPLOY_TYPE_ANNO_1800, app_id=916440)
    assert strategy is not None
    result = strategy.deploy(ctx)
    assert result.success is True
    assert (install / "mods" / "AnnoMod" / "moddata.txt").is_file()


def test_plan_fails_fast_on_missing_source(tmp_path: Path, db: DatabaseManager) -> None:
    missing = tmp_path / "gone"
    mods = tmp_path / "Mods"
    mods.mkdir()
    ctx = _make_ctx(
        tmp_path,
        db,
        app_id=1,
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        source=missing,
        mod_path=str(mods),
    )
    result = FolderCopyStrategy().deploy(ctx)
    assert result.success is False
