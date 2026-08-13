"""Deploy failure must never mutate content_status / mark existing payload missing."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.deploy import MISSING_CONTENT_DEPLOY_ERROR, ModDeployer
from services.deploy_rules.anno import ANNO_1800_APP_ID
from services.file_ops import (
    INFO_DIR_NAME,
    METADATA_FILENAME,
    MISSING_CONTENT_METADATA_KEY,
    apply_missing_content_marker,
    is_missing_mod_content,
    read_info_metadata_dict,
    read_is_missing_content,
    set_is_missing_content,
)
from services.library_reconcile import reconcile_library
from services.library_status import (
    CONTENT_CONTENT_MISSING,
    CONTENT_FOLDER_MISSING,
    CONTENT_HEALTHY,
    row_content_status,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "deploy_cs.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _write_meta(folder: Path, *, mod_id: str, app_id: int, game: str, **extra: object) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    payload = {
        "published_file_id": mod_id,
        "title": folder.name,
        "app_id": app_id,
        "game_name": game,
        **extra,
    }
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _content_status(db: DatabaseManager, mod_id: str) -> str:
    row = db.get_mod_backup_row(mod_id) or {}
    return row_content_status(row)


def test_stale_missing_flag_healed_when_payload_exists(tmp_path: Path) -> None:
    folder = tmp_path / "mod"
    folder.mkdir()
    _write_meta(folder, mod_id="1", app_id=0, game="G", is_missing_content=True)
    archive = folder / "payload.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("mod/assets.xml", "<Mod/>")
    assert not is_missing_mod_content(folder)
    assert read_is_missing_content(folder) is False
    meta = read_info_metadata_dict(folder) or {}
    assert meta.get(MISSING_CONTENT_METADATA_KEY) is False


def test_deploy_failure_does_not_mark_existing_mod_content_missing(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Case 3: strategy returns failure — content_status stays healthy."""
    from services.deploy_rules.base import StrategyResult

    library = tmp_path / "mod"
    game = "Game"
    mod_id = "920001"
    folder = library / game / "HasPayload"
    folder.mkdir(parents=True)
    (folder / "content.txt").write_text("ok", encoding="utf-8")
    _write_meta(folder, mod_id=mod_id, app_id=424242, game=game)

    mods_root = tmp_path / "game" / "mods"
    mods_root.mkdir(parents=True)
    db.update_game_deploy_config(
        424242,
        name=game,
        install_path=str(tmp_path / "game"),
        mod_path=str(mods_root),
        deploy_type="folder_copy",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id=mod_id,
            title="HasPayload",
            app_id=424242,
            game_name=game,
            managed_path=str(folder),
        )
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=str(folder),
        library_status="normal",
    )
    set_is_missing_content(folder, True)  # stale sticky

    assert _content_status(db, mod_id) == CONTENT_HEALTHY

    deployer = ModDeployer(library_root=library, db=db)
    with patch(
        "services.deploy_rules.generic.FolderCopyStrategy.deploy",
        return_value=StrategyResult(
            success=False,
            error="Permission denied：simulated",
            deploy_type="folder_copy",
        ),
    ):
        out = deployer.deploy_mod(mod_id)

    assert out.get("success") is False
    assert MISSING_CONTENT_DEPLOY_ERROR not in str(out.get("error") or "")
    assert _content_status(db, mod_id) == CONTENT_HEALTHY
    assert int((db.get_mod_backup_row(mod_id) or {}).get("folder_present") or 0) == 1
    assert not read_is_missing_content(folder)


def test_deploy_blocks_only_when_payload_truly_missing(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mod_id = "920002"
    folder = library / "Game" / "EmptyOnly"
    folder.mkdir(parents=True)
    _write_meta(folder, mod_id=mod_id, app_id=424242, game="Game")
    apply_missing_content_marker(folder)

    mods_root = tmp_path / "game" / "mods"
    mods_root.mkdir(parents=True)
    db.update_game_deploy_config(
        424242,
        name="Game",
        install_path=str(tmp_path / "game"),
        mod_path=str(mods_root),
        deploy_type="folder_copy",
    )
    db.upsert_mod(
        ModMetadata(published_file_id=mod_id, title="EmptyOnly", app_id=424242)
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_CONTENT_MISSING,
        folder_present=True,
        last_known_path=str(folder),
    )

    out = ModDeployer(library_root=library, db=db).deploy_mod(mod_id)
    assert out.get("success") is False
    assert MISSING_CONTENT_DEPLOY_ERROR in str(out.get("error") or "")
    assert out.get("is_missing_content") is True


def test_reconcile_folder_missing_and_restore(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mod_id = "920003"
    folder = library / "Game" / "Gone"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("x", encoding="utf-8")
    _write_meta(folder, mod_id=mod_id, app_id=424242, game="Game")
    db.update_game_deploy_config(424242, name="Game")
    db.upsert_mod(
        ModMetadata(
            published_file_id=mod_id,
            title="Gone",
            app_id=424242,
            managed_path=str(folder),
        )
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=str(folder),
    )

    # Remove source → folder_missing
    import shutil

    shutil.rmtree(folder)
    reconcile_library(library)
    assert _content_status(db, mod_id) == CONTENT_FOLDER_MISSING

    # Restore → healthy
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("x", encoding="utf-8")
    _write_meta(folder, mod_id=mod_id, app_id=424242, game="Game")
    reconcile_library(library)
    assert _content_status(db, mod_id) == CONTENT_HEALTHY
    assert int((db.get_mod_backup_row(mod_id) or {}).get("folder_present") or 0) == 1


def test_reconcile_empty_payload_is_content_missing(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mod_id = "920004"
    folder = library / "Game" / "EmptyPayload"
    folder.mkdir(parents=True)
    _write_meta(folder, mod_id=mod_id, app_id=424242, game="Game")
    db.update_game_deploy_config(424242, name="Game")
    db.upsert_mod(
        ModMetadata(published_file_id=mod_id, title="EmptyPayload", app_id=424242)
    )
    reconcile_library(library)
    assert _content_status(db, mod_id) == CONTENT_CONTENT_MISSING


def test_anno_mod_stale_flag_and_app_id_zero_can_deploy(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Regression for workspace-style Anno stubs (app_id=0 + stale missing flag)."""
    library = tmp_path / "mod"
    game = "Anno 1800"
    mod_id = "9000000000009028"
    folder = library / game / "Increased Golden Ticket Rewards"
    folder.mkdir(parents=True)
    archive = folder / "increased-golden-tickets.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("increased-golden-tickets/modinfo.json", '{"ModName":"x"}')
        zf.writestr("increased-golden-tickets/data/config/export/main/asset/assets.xml", "<A/>")
    _write_meta(
        folder,
        mod_id=mod_id,
        app_id=0,
        game=game,
        is_missing_content=True,
        workspace_id="17866204912338831",
        source_type="modio",
    )

    install = tmp_path / "AnnoInstall"
    (install / "mods").mkdir(parents=True)
    db.update_game_deploy_config(
        ANNO_1800_APP_ID,
        name=game,
        install_path=str(install),
        mod_path="",
        deploy_type="folder_copy",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id=mod_id,
            title="Increased Golden Ticket Rewards",
            app_id=0,
            game_name=game,
            managed_path=str(folder),
            source_type="modio",
        )
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=str(folder),
        workspace_id="17866204912338831",
        library_status="normal",
    )

    before = _content_status(db, mod_id)
    assert before == CONTENT_HEALTHY
    assert read_is_missing_content(folder) is False  # heals stale sticky

    out = ModDeployer(library_root=library, db=db).deploy_mod(mod_id)
    assert out.get("success") is True, out
    assert _content_status(db, mod_id) == CONTENT_HEALTHY
    # Inferred AppID persisted
    row = db.get_mod_backup_row(mod_id) or {}
    assert int(row.get("app_id") or 0) == ANNO_1800_APP_ID


def test_deploy_exception_path_does_not_touch_content_status(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mod_id = "920005"
    folder = library / "Game" / "Boom"
    folder.mkdir(parents=True)
    (folder / "x.txt").write_text("1", encoding="utf-8")
    _write_meta(folder, mod_id=mod_id, app_id=424242, game="Game")
    mods_root = tmp_path / "game" / "mods"
    mods_root.mkdir(parents=True)
    db.update_game_deploy_config(
        424242,
        name="Game",
        install_path=str(tmp_path / "game"),
        mod_path=str(mods_root),
        deploy_type="folder_copy",
    )
    db.upsert_mod(
        ModMetadata(published_file_id=mod_id, title="Boom", app_id=424242)
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=str(folder),
    )

    deployer = ModDeployer(library_root=library, db=db)
    with patch(
        "services.deploy_rules.generic.FolderCopyStrategy.deploy",
        side_effect=OSError("disk full"),
    ):
        with pytest.raises(OSError):
            deployer.deploy_mod(mod_id)

    assert _content_status(db, mod_id) == CONTENT_HEALTHY
    assert int((db.get_mod_backup_row(mod_id) or {}).get("folder_present") or 0) == 1
