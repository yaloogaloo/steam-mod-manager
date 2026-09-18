"""Phase 6.2: Detail and Deploy share one Mod folder Path Authority."""

from __future__ import annotations

import inspect
import shutil
from pathlib import Path

from core.db_manager import get_db
from core.game_info import GameInfo
from services.deploy_identity import is_frozen_internal_uuid
from services.deploy_paths import resolve_deploy_managed_path
from services.managed_path_cache import invalidate_managed_path_cache, reset_managed_path_cache_stats
from services.path_lifecycle import resolve_managed_folder, resolve_mod_folder_by_internal_id
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
from ui.mod_detail_panel import ModDetailPanel


def _seed(tmp_path: Path) -> tuple[Path, Path, str, str]:
    db = get_db()
    db.upsert_game(GameInfo(app_id=99, name="TestGame", folder_name="TestGame"))
    library = tmp_path / "mod"
    mod_dir = library / "TestGame" / "DeployMe"
    mod_dir.mkdir(parents=True)
    (mod_dir / "pak.txt").write_text("data", encoding="utf-8")
    created = create_steam_test_mod(
        db, external_id="8001", title="DeployMe", app_id=99, game_name="TestGame"
    )
    pk = prove_managed_folder(
        db,
        mod_dir,
        handle=created.mod_id,
        title="DeployMe",
        app_id=99,
        game_name="TestGame",
    )
    frozen = str(created.internal_id)
    assert is_frozen_internal_uuid(frozen)
    return library, mod_dir, frozen, str(pk)


def test_detail_and_deploy_use_same_resolver() -> None:
    deploy_src = inspect.getsource(resolve_deploy_managed_path)
    folder_src = inspect.getsource(resolve_mod_folder_by_internal_id)
    open_src = inspect.getsource(ModDetailPanel._open_folder)
    assert "resolve_managed_folder" in deploy_src
    assert "resolve_deploy_identity(" not in deploy_src
    assert "resolve_mod_pk(" not in deploy_src
    assert "resolve_managed_folder" in folder_src
    assert "resolve_managed_folder" in open_src


def test_uuid_finds_same_source_path_for_detail_and_deploy(tmp_path: Path) -> None:
    library, mod_dir, frozen, _pk = _seed(tmp_path)
    db = get_db()
    reset_managed_path_cache_stats()
    invalidate_managed_path_cache()

    detail = resolve_managed_folder(
        frozen, hint_path=mod_dir, library_root=library, db=db
    )
    deploy = resolve_deploy_managed_path(frozen, db=db, library_root=library)
    facade = resolve_mod_folder_by_internal_id(frozen, library_root=library, db=db)

    assert detail.path is not None
    assert deploy is not None
    assert facade is not None
    want = mod_dir.resolve()
    assert detail.path.resolve() == want
    assert deploy.resolve() == want
    assert facade.resolve() == want


def test_moved_folder_resolves_consistently(tmp_path: Path) -> None:
    library, mod_dir, frozen, pk = _seed(tmp_path)
    db = get_db()
    moved = library / "TestGame" / "MovedDeployMe"
    shutil.move(str(mod_dir), str(moved))
    db.update_mod_identity_fields(
        pk, last_known_path=str(mod_dir), folder_present=True
    )
    invalidate_managed_path_cache()

    detail = resolve_managed_folder(
        frozen, hint_path=mod_dir, library_root=library, db=db
    )
    deploy = resolve_deploy_managed_path(frozen, db=db, library_root=library)
    want = moved.resolve()
    assert detail.path is not None
    assert deploy is not None
    assert detail.path.resolve() == want
    assert deploy.resolve() == want


def test_deleted_folder_missing_for_all_modules(tmp_path: Path) -> None:
    library, mod_dir, frozen, pk = _seed(tmp_path)
    db = get_db()
    shutil.rmtree(mod_dir)
    db.update_mod_identity_fields(
        pk, last_known_path=str(mod_dir), folder_present=False
    )
    invalidate_managed_path_cache()

    detail = resolve_managed_folder(
        frozen, hint_path=mod_dir, library_root=library, db=db
    )
    deploy = resolve_deploy_managed_path(frozen, db=db, library_root=library)
    assert detail.path is None
    assert deploy is None


def test_deploy_uuid_finds_source_not_source_missing(tmp_path: Path) -> None:
    from services.deploy import ModDeployer
    from services.deploy_path_lifecycle import SOURCE_MOD_PATH_MISSING

    library, mod_dir, frozen, _pk = _seed(tmp_path)
    db = get_db()
    install = tmp_path / "Install"
    mods = tmp_path / "GameMods"
    install.mkdir()
    mods.mkdir()
    db.update_game_deploy_config(
        99,
        name="TestGame",
        install_path=str(install),
        mod_path=str(mods),
        deploy_type="folder_copy",
    )
    deployer = ModDeployer(library_root=library, db=db)
    ctx, err, _cleanup = deployer._resolve_context(
        frozen, require_target_exists=True, prepare_archives=False
    )
    assert err is None or err.get("error_code") != SOURCE_MOD_PATH_MISSING
    assert ctx is not None
    assert Path(ctx.source).resolve() == mod_dir.resolve()
