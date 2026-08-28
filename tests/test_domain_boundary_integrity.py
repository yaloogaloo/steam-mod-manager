"""Domain boundary — Library refresh must not depend on Deployment validation."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import FILE_TYPE_MAIN, PLATFORM_MODIO, PLATFORM_NEXUS, PLATFORM_STEAM, ModFileEntry, ModFilesBundle
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
    manager = DatabaseManager(tmp_path / "domain_boundary.db")
    manager.upsert_game(GameInfo(app_id=100, name="Game", folder_name="Game"))
    manager.upsert_game(
        GameInfo(app_id=1086940, name="Baldur's Gate 3", folder_name="BG3")
    )
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _write_zip(path: Path, *, inner: str = "mod.txt", data: bytes = b"payload") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(inner, data)


def _setup_modio_stub(
    tmp_path: Path,
    db: DatabaseManager,
    *,
    mid: str = "17878835583808244",
    folder: str = "ModioStub",
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
                "source_type": PLATFORM_MODIO,
                "url": "https://mod.io/g/baldursgate3/m/example-mod",
                "modio_mod_id": 12345,
                "modio_game_id": 6715,
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
        library_status="healthy",
        platform=PLATFORM_MODIO,
    )
    return mod


def test_case1_modio_refresh_without_local_archive_succeeds(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """mod.io refresh with metadata-only folder must not call deploy validation."""
    mod = _setup_modio_stub(tmp_path, db)
    provider_ok = MetadataRefreshResult(
        mod_id="17878835583808244",
        success=True,
        skipped=False,
        managed_path=mod,
        old_path=mod,
        title="Synced Title",
        message="ok",
    )

    with patch(
        "services.modio_metadata_refresh.refresh_modio_mod_metadata",
        return_value=provider_ok,
    ) as provider:
        with patch(
            "services.mod_source_integrity.validate_source",
            side_effect=DeploySourceError("must not run during refresh"),
        ):
            out = refresh_mod(
                "17878835583808244",
                mod,
                platform=PLATFORM_MODIO,
                library_root=tmp_path / "library",
                db=db,
            )

    provider.assert_called_once()
    assert out.success is True
    assert out.official_success is True
    compat = out.to_metadata_refresh_result()
    assert compat.success is True


def test_case2_metadata_only_library_healthy_deploy_blocked(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod = _setup_modio_stub(tmp_path, db, mid="97020", folder="MetaOnly")
    local = reconcile_local_state("97020", mod, db=db)
    assert local.folder_present is True
    assert local.content_status == CONTENT_CONTENT_MISSING
    assert has_local_mod_payload(mod, mod_id="97020", db=db) is False
    assert has_deployable_source(mod, mod_id="97020", db=db) is False

    db.update_game_deploy_config(100, name="Game", mod_path=str(tmp_path / "mods"))
    (tmp_path / "mods").mkdir()
    deploy = ModDeployer(library_root=tmp_path / "library", db=db).deploy_mod("97020")
    assert deploy["success"] is False
    assert deploy.get("is_missing_content") or deploy.get("reason") in {
        "source_integrity",
        None,
    }


def test_case3_invalid_zip_refresh_ok_deploy_fails(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod = _setup_modio_stub(tmp_path, db, mid="97021", folder="BadZip")
    bad = mod / "broken.zip"
    bad.write_bytes(b"not-a-zip")
    db.set_mod_files(
        "97021",
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

    local = reconcile_local_state("97021", mod, db=db)
    assert local.folder_present is True
    assert local.content_status == CONTENT_HEALTHY
    assert has_local_mod_payload(mod, mod_id="97021", db=db) is True
    assert has_deployable_source(mod, mod_id="97021", db=db) is False

    db.update_game_deploy_config(100, name="Game", mod_path=str(tmp_path / "mods"))
    (tmp_path / "mods").mkdir()
    deploy = ModDeployer(library_root=tmp_path / "library", db=db).deploy_mod("97021")
    assert deploy["success"] is False

    with pytest.raises(DeploySourceError):
        validate_source("97021", managed_path=mod, db=db, auto_reconcile=False)


def test_case4_deploy_source_error_never_reaches_refresh_worker(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod = _setup_modio_stub(tmp_path, db, mid="97022", folder="WorkerSafe")
    provider_ok = MetadataRefreshResult(
        mod_id="97022",
        success=True,
        skipped=False,
        managed_path=mod,
        old_path=mod,
        title="Worker Title",
    )

    from ui.metadata_refresh_thread import ModRefreshWorker

    worker = ModRefreshWorker(
        mod,
        mod_id="97022",
        library_root=tmp_path / "library",
        platform=PLATFORM_MODIO,
    )
    emitted: list[object] = []
    failed: list[str] = []

    worker.refresh_finished.connect(emitted.append)
    worker.refresh_failed.connect(failed.append)

    with patch(
        "services.mod_refresh.refresh_mod",
        return_value=MagicMock(
            to_metadata_refresh_result=lambda: provider_ok,
        ),
    ):
        with patch(
            "services.mod_source_integrity.validate_source",
            side_effect=DeploySourceError("must not propagate"),
        ):
            worker.run()

    assert failed == []
    assert len(emitted) == 1
    assert getattr(emitted[0], "success", False) is True


@pytest.mark.parametrize(
    ("platform", "provider_path", "provider_name"),
    [
        (PLATFORM_STEAM, "services.metadata_refresh.refresh_steam_mod_metadata", "steam"),
        (PLATFORM_NEXUS, None, "nexus"),
        (PLATFORM_MODIO, "services.modio_metadata_refresh.refresh_modio_mod_metadata", "modio"),
    ],
)
def test_case5_refresh_providers_independent_of_deploy_validator(
    tmp_path: Path,
    db: DatabaseManager,
    platform: str,
    provider_path: str | None,
    provider_name: str,
) -> None:
    ids = {"steam": "970231", "nexus": "970232", "modio": "970233"}
    mid = ids[provider_name]
    mod = _setup_modio_stub(tmp_path, db, mid=mid, folder=f"Refresh_{provider_name}")
    if platform == PLATFORM_STEAM:
        db.update_mod_identity_fields(mid, platform=PLATFORM_STEAM)

    validate_patch = patch(
        "services.mod_source_integrity.validate_source",
        side_effect=DeploySourceError("deploy validator must stay out of refresh"),
    )

    if provider_path is None:
        with validate_patch:
            out = refresh_mod(
                mid,
                mod,
                platform=platform,
                library_root=tmp_path / "library",
                db=db,
            )
        assert out.success is True
        return

    provider_ok = MetadataRefreshResult(
        mod_id=mid,
        success=True,
        skipped=False,
        managed_path=mod,
        old_path=mod,
        title="Provider OK",
    )
    with patch(provider_path, return_value=provider_ok):
        with validate_patch:
            out = refresh_mod(
                mid,
                mod,
                platform=platform,
                library_root=tmp_path / "library",
                db=db,
            )
    assert out.success is True
    assert out.official_success is True
