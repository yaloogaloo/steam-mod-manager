"""Phase 6.3: Deploy UUID runtime trace + Detail UI must not leak SQLite PK."""

from __future__ import annotations

import inspect
import logging
import shutil
from pathlib import Path

import pytest
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import get_db
from core.game_info import GameInfo
from services.deploy import ModDeployer
from services.deploy_identity import is_frozen_internal_uuid
from services.deploy_path_lifecycle import SOURCE_MOD_PATH_MISSING
from services.deploy_paths import resolve_deploy_managed_path
from services.managed_path_cache import invalidate_managed_path_cache
from services.path_lifecycle import resolve_managed_folder
from ui.mod_detail_panel import ModDetailPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


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
    return library, mod_dir, frozen, str(pk)


def test_detail_panel_source_has_no_sqlite_pk_label() -> None:
    src = inspect.getsource(ModDetailPanel)
    assert "SQLite mod_id" not in src
    assert "Internal Database ID" not in src


def test_detail_workspace_line_shows_workspace_id_only(
    qapp: QApplication, tmp_path: Path
) -> None:
    library, mod_dir, frozen, _pk = _seed(tmp_path)
    panel = ModDetailPanel()
    panel.show_mod(mod_dir, mod_id=frozen)
    text = panel.meta_workspace_line.text()
    assert "Workspace ID" in text
    assert "SQLite" not in text
    assert "mod_id:" not in text
    assert "Internal ID" not in text
    assert frozen not in text


def test_hint_heals_last_known_path_for_deploy_without_ui_hint(tmp_path: Path) -> None:
    library, mod_dir, frozen, pk = _seed(tmp_path)
    db = get_db()
    db.update_mod_identity_fields(pk, last_known_path="", folder_present=True)
    invalidate_managed_path_cache()

    resolved = resolve_managed_folder(
        frozen, hint_path=mod_dir, library_root=library, db=db
    )
    assert resolved.path is not None
    assert resolved.path.resolve() == mod_dir.resolve()
    row = db.get_mod_backup_row(pk) or {}
    healed = Path(str(row.get("last_known_path") or "")).resolve()
    assert healed == mod_dir.resolve()

    deploy = resolve_deploy_managed_path(frozen, db=db, library_root=library)
    assert deploy is not None
    assert deploy.resolve() == mod_dir.resolve()


def test_deploy_context_uuid_and_existing_source(tmp_path: Path) -> None:
    library, mod_dir, frozen, pk = _seed(tmp_path)
    deployer = ModDeployer(library_root=library, db=get_db())
    ctx, err, _cleanup = deployer._resolve_context(
        frozen, require_target_exists=True, prepare_archives=False
    )
    assert err is None
    assert ctx is not None
    assert ctx.internal_id == frozen
    assert str(ctx.mod_pk) == pk
    assert Path(ctx.source).resolve() == mod_dir.resolve()


def test_deploy_mod_entry_keeps_uuid_and_skips_source_missing(tmp_path: Path) -> None:
    library, _mod_dir, frozen, pk = _seed(tmp_path)
    deployer = ModDeployer(library_root=library, db=get_db())
    result = deployer.deploy_mod(frozen)
    assert result.get("error_code") != SOURCE_MOD_PATH_MISSING, result
    assert result.get("error_code") != "invalid_internal_uuid", result
    assert result.get("internal_id") == frozen, result
    assert int(result.get("mod_pk") or 0) == int(pk)


def test_deploy_mod_passes_source_gate(tmp_path: Path) -> None:
    from services.deploy_identity import resolve_deploy_entity

    library, mod_dir, frozen, pk = _seed(tmp_path)
    db = get_db()
    deployer = ModDeployer(library_root=library, db=db)
    entity = resolve_deploy_entity(frozen, db=db)
    out = deployer._deploy_mod_body(entity, _skip_target_ownership_check=True)
    assert out.get("error_code") != SOURCE_MOD_PATH_MISSING, out
    assert out.get("internal_id") == frozen, out
    assert int(out.get("mod_pk") or 0) == int(pk)
    assert Path(mod_dir).is_dir()


def test_source_missing_logs_internal_id_mod_pk_source_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    library, mod_dir, frozen, pk = _seed(tmp_path)
    shutil.rmtree(mod_dir)
    db = get_db()
    db.update_mod_identity_fields(
        pk, last_known_path=str(mod_dir), folder_present=False
    )
    invalidate_managed_path_cache()
    deployer = ModDeployer(library_root=library, db=db)
    with caplog.at_level(logging.WARNING):
        result = deployer.deploy_mod(frozen)
    assert result.get("error_code") == SOURCE_MOD_PATH_MISSING, result
    assert result.get("internal_id") == frozen
    assert int(result.get("mod_pk") or 0) == int(pk)
    source_path = str(
        result.get("source_path") or result.get("configured_path") or ""
    )
    assert str(mod_dir) in source_path
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert f"internal_id={frozen}" in joined
    assert f"mod_pk={pk}" in joined
    assert "source_path=" in joined
