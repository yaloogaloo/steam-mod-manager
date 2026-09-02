"""Refresh policy: local reconcile first, one-shot official sync, metadata ownership."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from core.mod_platform import PLATFORM_MODIO, PLATFORM_STEAM
from core.steam_api import SteamWorkshopClient
from services.file_ops import INFO_DIR_NAME, ModFileManager, apply_missing_content_marker
from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_FOLDER_MISSING, CONTENT_HEALTHY
from services.metadata_ownership import FIELD_COVER, FIELD_DESCRIPTION, FIELD_DISPLAY_NAME
from services.metadata_refresh import refresh_steam_mod_metadata
from services.mod_refresh import refresh_mod
from services.modio_api import ModioModDetails
from services.modio_metadata_refresh import refresh_modio_mod_metadata


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "refresh_policy.db")
    yield manager
    DatabaseManager.reset_instance()


def _steam_folder(lib: Path, mid: str, *, name: str = "", with_zip: bool = True) -> Path:
    folder = lib / "Game" / (name or f"Unknown_Mod_{mid}")
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": name or f"Unknown_Mod_{mid}",
                "display_name": name or f"Unknown_Mod_{mid}",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if with_zip:
        with zipfile.ZipFile(folder / "payload.zip", "w") as zf:
            zf.writestr("mod.xml", "<Mod/>")
    return folder


def test_case1_new_import_first_official_sync(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = "3591452001"
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, mid)
    db.upsert_mod(ModMetadata(published_file_id=mid, title=f"Unknown_Mod_{mid}"))
    assert not db.is_official_metadata_synced(mid)

    fresh = ModMetadata(
        published_file_id=mid,
        title="Official Workshop Title",
        description="Official desc",
    )
    provider = MagicMock(return_value=[fresh])
    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", provider)
    monkeypatch.setattr(SteamWorkshopClient, "fetch_and_save_cover", lambda *a, **k: None)

    result = refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    assert result.success
    assert result.official_success
    assert db.is_official_metadata_synced(mid)
    assert provider.call_count == 1
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.steam_name == "Official Workshop Title"


def test_case2_synced_mod_refresh_skips_provider(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = "3591452002"
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, mid, name="Synced Mod")
    db.upsert_mod(ModMetadata(published_file_id=mid, title="Synced Mod"))
    db.set_official_metadata_synced(mid, True)

    provider = MagicMock()
    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", provider)

    result = refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    assert result.success
    assert not result.official_attempted
    assert provider.call_count == 0


def test_case3_synced_mod_delete_payload_marks_content_missing(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = "3591452003"
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, mid, name="Payload Mod")
    db.upsert_mod(ModMetadata(published_file_id=mid, title="Payload Mod"))
    db.set_official_metadata_synced(mid, True)

    provider = MagicMock()
    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", provider)
    (folder / "payload.zip").unlink()

    result = refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    assert provider.call_count == 0
    assert result.local is not None
    assert result.local.content_status == CONTENT_CONTENT_MISSING


def test_case4_restore_payload_back_to_healthy(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = "3591452004"
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, mid, name="Restore Mod", with_zip=False)
    apply_missing_content_marker(folder, sync_backup=False)
    db.upsert_mod(ModMetadata(published_file_id=mid, title="Restore Mod"))
    db.set_official_metadata_synced(mid, True)

    with zipfile.ZipFile(folder / "payload.zip", "w") as zf:
        zf.writestr("mod.xml", "<Mod/>")

    provider = MagicMock()
    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", provider)

    result = refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    assert provider.call_count == 0
    assert result.local is not None
    assert result.local.content_status == CONTENT_HEALTHY


def test_case5_first_sync_failure_stays_unsynced(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = "3591452005"
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, mid)
    db.upsert_mod(ModMetadata(published_file_id=mid, title=f"Unknown_Mod_{mid}"))

    def _timeout(self, ids, **kwargs):
        raise TimeoutError("steam timeout")

    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", _timeout)

    result = refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    assert not db.is_official_metadata_synced(mid)
    assert result.official_attempted
    assert not result.official_success

    provider = MagicMock(side_effect=TimeoutError("again"))
    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", provider)
    refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    assert provider.call_count == 1


def test_case6_user_display_name_not_overwritten(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = "3591452006"
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, mid)
    db.upsert_mod(ModMetadata(published_file_id=mid, title="Original"))
    db.set_official_metadata_synced(mid, True)
    db.update_mod_user_metadata(mid, {"display_name": "My Custom Name"})
    overrides = db.get_user_override_fields(mid)
    assert overrides.get(FIELD_DISPLAY_NAME) is True

    fresh = ModMetadata(published_file_id=mid, title="New Official Title")
    provider = MagicMock(return_value=[fresh])
    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", provider)

    refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    assert provider.call_count == 0

    # Direct official merge path must still respect override.
    provider.reset_mock()
    db.set_official_metadata_synced(mid, False)
    refresh_steam_mod_metadata(
        mid, folder, library_root=lib, force=True, db=db, allow_official_sync=True
    )
    assert provider.call_count == 1
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.display_name == "My Custom Name"


def test_case7_user_description_not_overwritten(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = "3591452007"
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, mid, name="Desc Mod")
    db.upsert_mod(ModMetadata(published_file_id=mid, title="Desc Mod", description="Old official"))
    db.update_mod_user_metadata(
        mid,
        {"display_name": "Desc Mod", "custom_description": "My Custom Description"},
    )
    db.set_official_metadata_synced(mid, False)

    fresh = ModMetadata(
        published_file_id=mid,
        title="Desc Mod",
        description="New Official Description",
    )
    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", lambda *a, **k: [fresh])
    monkeypatch.setattr(SteamWorkshopClient, "fetch_and_save_cover", lambda *a, **k: None)

    refresh_steam_mod_metadata(
        mid, folder, library_root=lib, force=True, db=db, allow_official_sync=True
    )
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.custom_description == "My Custom Description"
    assert info.steam_description == "New Official Description"


def test_case8_user_cover_not_overwritten(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = "3591452008"
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, mid, name="Cover Mod")
    info_dir = folder / INFO_DIR_NAME
    user_cover = info_dir / "cover.png"
    user_cover.write_bytes(b"\x89PNG\r\n")
    db.upsert_mod(ModMetadata(published_file_id=mid, title="Cover Mod"))
    db.update_mod_cover_path(mid, ".info/cover.png")
    db.set_user_override_field(mid, FIELD_COVER, overridden=True)
    db.set_official_metadata_synced(mid, False)

    cover_calls: list[str] = []

    def _cover(self, metadata, dest_dir, *, filename="cover"):
        cover_calls.append("called")
        return None

    fresh = ModMetadata(
        published_file_id=mid,
        title="Cover Mod",
        preview_url="https://example.com/new.png",
    )
    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", lambda *a, **k: [fresh])
    monkeypatch.setattr(SteamWorkshopClient, "fetch_and_save_cover", _cover)

    refresh_steam_mod_metadata(
        mid, folder, library_root=lib, force=True, db=db, allow_official_sync=True
    )
    assert cover_calls == []
    row = db.get_mod_display_info(mid)
    assert row is not None
    assert row.cover_path == ".info/cover.png"


def test_case9_placeholder_replaced_on_first_sync(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = "3591452009"
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, mid)
    db.upsert_mod(ModMetadata(published_file_id=mid, title=f"Unknown_Mod_{mid}"))

    fresh = ModMetadata(published_file_id=mid, title="Bigger Harbour")
    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", lambda *a, **k: [fresh])
    monkeypatch.setattr(SteamWorkshopClient, "fetch_and_save_cover", lambda *a, **k: None)

    refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.steam_name == "Bigger Harbour"
    assert info.display_name == "Bigger Harbour"


def test_case10_missing_folder_local_only(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = "3591452010"
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, mid, name="Gone Mod")
    db.upsert_mod(ModMetadata(published_file_id=mid, title="Gone Mod"))
    db.update_mod_identity_fields(
        mid,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        content_status=CONTENT_HEALTHY,
    )
    db.set_official_metadata_synced(mid, True)

    provider = MagicMock()
    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", provider)

    import shutil

    shutil.rmtree(folder)

    result = refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    assert provider.call_count == 0
    assert result.local is not None
    assert result.local.folder_present is False
    assert result.local.content_status == CONTENT_FOLDER_MISSING


def test_case12_official_sync_only_once_across_refreshes(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = "3591452012"
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, mid)
    db.upsert_mod(ModMetadata(published_file_id=mid, title=f"Unknown_Mod_{mid}"))

    fresh = ModMetadata(published_file_id=mid, title="Once Only")
    provider = MagicMock(return_value=[fresh])
    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", provider)
    monkeypatch.setattr(SteamWorkshopClient, "fetch_and_save_cover", lambda *a, **k: None)

    refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    assert provider.call_count == 1
    assert db.is_official_metadata_synced(mid)


def test_modio_synced_skips_api(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "lib"
    folder = lib / "Anno 1800" / "Harbor"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "title": "Harbor",
                "url": "https://mod.io/g/anno-1800/m/harborlife",
                "source_type": "modio",
            }
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(folder / "mod.zip", "w") as zf:
        zf.writestr("a.txt", "x")

    display = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="123",
        source_url="https://mod.io/g/anno-1800/m/harborlife",
        title="Harbor",
        app_id=916440,
        game_name="Anno 1800",
    )
    mid = display.mod_id
    db.set_official_metadata_synced(mid, True)

    client = MagicMock()
    monkeypatch.setattr(
        "services.modio_metadata_refresh.ModioClient",
        lambda *a, **k: client,
    )

    result = refresh_modio_mod_metadata(
        mid, folder, library_root=lib, db=db, allow_official_sync=True
    )
    assert result.skipped
    client.resolve_mod.assert_not_called()
