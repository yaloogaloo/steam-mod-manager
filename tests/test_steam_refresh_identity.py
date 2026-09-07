"""Steam refresh must UPDATE by mods.mod_id after Identity split (PK ≠ Workshop)."""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import PLATFORM_STEAM
from core.steam_api import SteamWorkshopClient
from services.file_ops import INFO_DIR_NAME
from services.metadata_refresh import (
    refresh_steam_mod_metadata,
    resolve_steam_workshop_external_id,
)
from services.mod_refresh import refresh_mod
from ui.metadata_refresh_thread import ModRefreshWorker


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "steam_refresh_identity.db")
    manager.upsert_game(GameInfo(app_id=289070, name="Civ6", folder_name="Civ6"))
    yield manager
    DatabaseManager.reset_instance()


def _steam_folder(lib: Path, *, name: str) -> Path:
    folder = lib / "Civ6" / name
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {"title": name, "display_name": name},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(folder / "payload.zip", "w") as zf:
        zf.writestr("mod.xml", "<Mod/>")
    return folder


def _seed_split_identity(
    db: DatabaseManager,
    folder: Path,
    *,
    workshop_id: str,
    title: str,
    app_id: int = 289070,
    mod_id: int = 465,
):
    """Steam entity with mods.mod_id ≠ workspace_id (post-rebuild shape)."""
    from services.identity_service import identity_create_scope
    from core.mod_platform import steam_workshop_url
    from core.db_manager import _utc_now

    mid = int(mod_id)
    assert mid != int(workshop_id)
    with identity_create_scope(), db._lock:
        db._conn.execute(
            """
            INSERT INTO mods (
                mod_id, app_id, title, preview_url, description,
                display_name, custom_description, user_notes, favorite,
                platform, source_url, external_id, workspace_id, mod_files,
                last_known_path, folder_present, updated_at
            )
            VALUES (?, ?, ?, '', '', '', '', '', 0, ?, ?, ?, ?, '{}', ?, 1, ?)
            """,
            (
                mid,
                app_id,
                title,
                PLATFORM_STEAM,
                steam_workshop_url(workshop_id),
                workshop_id,
                workshop_id,
                str(folder),
                _utc_now(),
            ),
        )
        db._conn.commit()
    return type(
        "Ent",
        (),
        {
            "mod_id": str(mid),
            "workspace_id": workshop_id,
            "external_id": workshop_id,
        },
    )()


def test_resolve_workshop_id_from_row_not_pk_digits(db: DatabaseManager, tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, name="Alice")
    created = _seed_split_identity(
        db, folder, workshop_id="3681020076", title="Alice"
    )
    assert str(created.mod_id) != "3681020076"
    assert resolve_steam_workshop_external_id(db, str(created.mod_id)) == "3681020076"
    # Workshop / workspace as lookup key must not invent Workshop from missing PK.
    assert resolve_steam_workshop_external_id(db, "3681020076") == ""
    assert resolve_steam_workshop_external_id(db, "999999999") == ""


def test_refresh_updates_pk_row_not_workshop_row(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, name="Unknown_Mod_465")
    created = _seed_split_identity(
        db, folder, workshop_id="3681020076", title="Unknown_Mod_465"
    )
    mid = str(created.mod_id)
    workshop = "3681020076"
    assert db.get_mod_display_info(workshop) is None

    fresh = ModMetadata(
        published_file_id=workshop,
        title="Official Workshop Title",
        description="Official desc",
        app_id=289070,
    )
    monkeypatch.setattr(
        SteamWorkshopClient, "refresh_details", MagicMock(return_value=[fresh])
    )
    monkeypatch.setattr(
        SteamWorkshopClient, "fetch_and_save_cover", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "services.metadata_refresh.rename_managed_folder_for_title",
        lambda folder, meta, **k: (Path(folder), False),
    )

    with caplog.at_level(logging.ERROR):
        result = refresh_mod(
            mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db
        )
    assert result.success
    assert result.official_success

    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.steam_name == "Official Workshop Title"
    assert db.get_mod_display_info(workshop) is None
    assert db._conn.execute(
        "SELECT COUNT(*) AS n FROM mods WHERE mod_id = ?", (int(workshop),)
    ).fetchone()["n"] == 0


def test_workspace_id_as_mod_id_fails_with_structured_log(
    db: DatabaseManager, tmp_path: Path, caplog
) -> None:
    lib = tmp_path / "lib"
    folder = _steam_folder(lib, name="ModFolder")
    created = _seed_split_identity(
        db, folder, workshop_id="3793097715", title="Bottom Mod", mod_id=466
    )
    workshop = str(created.workspace_id)
    assert workshop == "3793097715"
    assert str(created.mod_id) != workshop

    with caplog.at_level(logging.ERROR):
        result = refresh_steam_mod_metadata(
            workshop,
            folder,
            library_root=lib,
            force=True,
            allow_official_sync=True,
            db=db,
            download_cover=False,
        )
    assert result.success is False
    assert "not found" in (result.error or "").lower() or "mod_id" in (
        result.error or ""
    ).lower()
    assert any("[MOD_REFRESH_FAILED]" in r.message for r in caplog.records)
    assert any("identity_validate" in r.message for r in caplog.records)


def test_folder_name_fallback_forbidden_in_worker(
    tmp_path: Path, caplog
) -> None:
    folder = tmp_path / "Some Chinese Folder Name"
    folder.mkdir()
    worker = ModRefreshWorker(folder, mod_id="", platform=PLATFORM_STEAM)
    assert worker.mod_id == ""
    failed: list[str] = []
    worker.refresh_failed.connect(failed.append)
    with caplog.at_level(logging.ERROR):
        worker.run()
    assert failed
    assert any("[MOD_REFRESH_FAILED]" in r.message for r in caplog.records)
    assert any("worker_identity" in r.message for r in caplog.records)


def test_refresh_details_does_not_upsert_db(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    workshop = "872296228"
    payload = ModMetadata(
        published_file_id=workshop,
        title="Should Not Insert",
        description="x",
        app_id=289070,
    )

    def _fake_request(self, ids):  # noqa: ANN001
        return [payload]

    monkeypatch.setattr(
        SteamWorkshopClient,
        "_request_published_file_details_refresh",
        _fake_request,
    )
    client = SteamWorkshopClient(db=db, request_interval=0, enable_scrape_fallback=False)
    try:
        out = client.refresh_details([workshop])
    finally:
        client.close()
    assert out[0].title == "Should Not Insert"
    assert db.get_mod(workshop) is None
    assert db.get_mod_display_info(workshop) is None
