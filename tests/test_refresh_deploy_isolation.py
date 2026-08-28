"""Refresh vs deploy isolation — regression guards for domain boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.db_manager import DatabaseManager, DEPLOY_STATUS_FAILED, DEPLOY_STATUS_NOT_DEPLOYED
from core.game_info import GameInfo
from core.mod_platform import FILE_TYPE_MAIN, PLATFORM_MODIO, ModFileEntry, ModFilesBundle
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_errors import DeploySourceError
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY
from services.local_file_index import has_local_mod_payload
from services.metadata_refresh import MetadataRefreshResult
from services.mod_refresh import refresh_mod, reconcile_local_state
from services.mod_source_integrity import has_deployable_source, validate_source


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "refresh_deploy_isolation.db")
    manager.upsert_game(GameInfo(app_id=100, name="Game", folder_name="Game"))
    manager.upsert_game(
        GameInfo(app_id=1086940, name="Baldur's Gate 3", folder_name="BG3")
    )
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _modio_folder(
    tmp_path: Path,
    db: DatabaseManager,
    *,
    mid: str,
    folder: str = "IsoMod",
) -> Path:
    library = tmp_path / "library"
    mod = library / "BG3" / folder
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": folder,
                "app_id": 1086940,
                "platform": PLATFORM_MODIO,
                "url": "https://mod.io/g/baldursgate3/m/example-mod",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id=mid,
            title=folder,
            app_id=1086940,
            game_name="BG3",
            source_type=PLATFORM_MODIO,
        )
    )
    db.update_mod_identity_fields(
        mid,
        folder_present=True,
        last_known_path=str(mod),
        platform=PLATFORM_MODIO,
    )
    return mod


def test_case1_metadata_refresh_without_local_files_succeeds(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Metadata-only folder: refresh must succeed; content_status may be missing."""
    mod = _modio_folder(tmp_path, db, mid="98001")
    provider_ok = MetadataRefreshResult(
        mod_id="98001",
        success=True,
        skipped=False,
        managed_path=mod,
        old_path=mod,
        title="Remote Title",
    )

    with patch(
        "services.modio_metadata_refresh.refresh_modio_mod_metadata",
        return_value=provider_ok,
    ):
        out = refresh_mod(
            "98001",
            mod,
            platform=PLATFORM_MODIO,
            library_root=tmp_path / "library",
            db=db,
        )

    assert out.success is True
    assert out.official_success is True
    local = reconcile_local_state("98001", mod, db=db)
    assert local.content_status == CONTENT_CONTENT_MISSING
    assert has_local_mod_payload(mod, mod_id="98001", db=db) is False


def test_case2_invalid_zip_refresh_succeeds(tmp_path: Path, db: DatabaseManager) -> None:
    mod = _modio_folder(tmp_path, db, mid="98002")
    (mod / "broken.zip").write_bytes(b"not-a-zip")
    db.set_mod_files(
        "98002",
        ModFilesBundle(
            files=[
                ModFileEntry(
                    name="broken.zip",
                    filename="broken.zip",
                    path="broken.zip",
                    type=FILE_TYPE_MAIN,
                    enabled=True,
                )
            ]
        ),
    )

    provider_ok = MetadataRefreshResult(
        mod_id="98002",
        success=True,
        skipped=False,
        managed_path=mod,
        old_path=mod,
        title="Remote",
    )
    with patch(
        "services.modio_metadata_refresh.refresh_modio_mod_metadata",
        return_value=provider_ok,
    ):
        out = refresh_mod(
            "98002",
            mod,
            platform=PLATFORM_MODIO,
            library_root=tmp_path / "library",
            db=db,
        )

    assert out.success is True
    local = reconcile_local_state("98002", mod, db=db)
    assert local.content_status == CONTENT_HEALTHY
    assert has_local_mod_payload(mod, mod_id="98002", db=db) is True
    assert has_deployable_source(mod, mod_id="98002", db=db) is False


def test_case3_invalid_zip_deploy_fails(tmp_path: Path, db: DatabaseManager) -> None:
    mod = _modio_folder(tmp_path, db, mid="98003")
    (mod / "broken.zip").write_bytes(b"not-a-zip")
    db.set_mod_files(
        "98003",
        ModFilesBundle(
            files=[
                ModFileEntry(
                    name="broken.zip",
                    filename="broken.zip",
                    path="broken.zip",
                    type=FILE_TYPE_MAIN,
                    enabled=True,
                )
            ]
        ),
    )
    db.update_game_deploy_config(100, name="Game", mod_path=str(tmp_path / "mods"))
    (tmp_path / "mods").mkdir()

    out = ModDeployer(library_root=tmp_path / "library", db=db).deploy_mod("98003")
    assert out["success"] is False
    assert out.get("reason") == "source_integrity"

    info = db.get_mod_deploy_info("98003")
    assert info is not None
    assert info.deploy_status in {DEPLOY_STATUS_FAILED, DEPLOY_STATUS_NOT_DEPLOYED}


def test_case4_deploy_source_error_does_not_enter_refresh_worker(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod = _modio_folder(tmp_path, db, mid="98004")
    provider_ok = MetadataRefreshResult(
        mod_id="98004",
        success=True,
        skipped=False,
        managed_path=mod,
        old_path=mod,
        title="OK",
    )

    from ui.metadata_refresh_thread import ModRefreshWorker

    worker = ModRefreshWorker(
        mod,
        mod_id="98004",
        library_root=tmp_path / "library",
        platform=PLATFORM_MODIO,
    )
    failed: list[str] = []
    worker.refresh_failed.connect(failed.append)

    with patch(
        "services.mod_refresh.refresh_mod",
        return_value=MagicMock(to_metadata_refresh_result=lambda: provider_ok),
    ):
        with patch(
            "services.mod_source_integrity.validate_source",
            side_effect=DeploySourceError("must never reach refresh"),
        ):
            worker.run()

    assert failed == []


def test_case5_local_file_exception_does_not_fail_metadata_refresh(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod = _modio_folder(tmp_path, db, mid="98005")
    provider_ok = MetadataRefreshResult(
        mod_id="98005",
        success=True,
        skipped=False,
        managed_path=mod,
        old_path=mod,
        title="After Local Error",
    )

    with patch(
        "services.local_file_index.reconcile_local_files",
        side_effect=RuntimeError("simulated filesystem reconcile failure"),
    ):
        with patch(
            "services.modio_metadata_refresh.refresh_modio_mod_metadata",
            return_value=provider_ok,
        ):
            out = refresh_mod(
                "98005",
                mod,
                platform=PLATFORM_MODIO,
                library_root=tmp_path / "library",
                db=db,
            )

    assert out.success is True
    assert out.official_success is True
    assert "local_file_reconcile_failed" in (out.local.notes if out.local else [])
