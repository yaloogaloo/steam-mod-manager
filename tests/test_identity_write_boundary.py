"""Identity write boundary: Steam Workshop ID must never mint mods.mod_id."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import PLATFORM_STEAM, steam_workshop_url
from core.steam_api import SteamWorkshopClient
from services.identity_service import identity_create_scope


APP_ID = 289070
INTERNAL_ID = 465
WORKSHOP_ID = "872296228"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "identity_write_boundary.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name="Civ6", folder_name="Civ6"))
    yield manager
    DatabaseManager.reset_instance()


def _seed_split_entity(db: DatabaseManager) -> None:
    """mod_id=465, published/external/workspace=872296228 (post-rebuild shape)."""
    assert INTERNAL_ID != int(WORKSHOP_ID)
    with identity_create_scope(), db._lock:
        db._conn.execute(
            """
            INSERT INTO mods (
                mod_id, app_id, title, preview_url, description,
                display_name, custom_description, user_notes, favorite,
                platform, source_url, external_id, workspace_id, mod_files,
                updated_at
            )
            VALUES (?, ?, ?, '', '', '', '', '', 0, ?, ?, ?, ?, '{}', datetime('now'))
            """,
            (
                INTERNAL_ID,
                APP_ID,
                "",  # empty title → catalog refresh treats as needing fetch
                PLATFORM_STEAM,
                steam_workshop_url(WORKSHOP_ID),
                WORKSHOP_ID,
                WORKSHOP_ID,
            ),
        )
        db._conn.commit()


def test_upsert_mod_updates_existing_pk_not_workshop(db: DatabaseManager) -> None:
    """mod_id=465 + published_file_id=872296228 → refresh keeps PK 465."""
    _seed_split_entity(db)

    db.upsert_mod(
        ModMetadata(
            published_file_id=WORKSHOP_ID,
            title="Official Workshop Title",
            description="Official desc",
            app_id=APP_ID,
        )
    )

    info = db.get_mod_display_info(str(INTERNAL_ID))
    assert info is not None
    assert info.steam_name == "Official Workshop Title"
    assert str(info.external_id) == WORKSHOP_ID
    assert str(info.workspace_id) == WORKSHOP_ID
    assert db.get_mod(WORKSHOP_ID) is None
    assert (
        db._conn.execute(
            "SELECT COUNT(*) AS n FROM mods WHERE mod_id = ?",
            (int(WORKSHOP_ID),),
        ).fetchone()["n"]
        == 0
    )
    assert (
        db._conn.execute(
            "SELECT COUNT(*) AS n FROM mods WHERE mod_id = ?",
            (INTERNAL_ID,),
        ).fetchone()["n"]
        == 1
    )


def test_upsert_mod_refuses_auto_create_without_entity(db: DatabaseManager) -> None:
    """No existing entity → upsert_mod must not mint Internal PK from Workshop."""
    workshop = "999888777"
    db.upsert_mod(
        ModMetadata(
            published_file_id=workshop,
            title="Should Not Exist",
            app_id=APP_ID,
        )
    )
    assert db.get_mod(workshop) is None
    assert db.get_mod_display_info(workshop) is None
    assert (
        db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"] == 0
    )


def test_steam_api_batch_updates_only_never_inserts(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steam API payload may UPDATE existing rows; must not INSERT new PK."""
    _seed_split_entity(db)
    payload = ModMetadata(
        published_file_id=WORKSHOP_ID,
        title="From Steam API",
        description="api",
        app_id=APP_ID,
    )
    orphan = ModMetadata(
        published_file_id="111222333",
        title="Orphan Workshop",
        description="must not insert",
        app_id=APP_ID,
    )

    def _fake_request(self, ids):  # noqa: ANN001
        out = []
        for raw in ids:
            wid = str(raw)
            if wid == WORKSHOP_ID:
                out.append(payload)
            elif wid == "111222333":
                out.append(orphan)
            else:
                out.append(
                    ModMetadata(published_file_id=wid, fetch_error="missing")
                )
        return out

    monkeypatch.setattr(
        SteamWorkshopClient,
        "_request_published_file_details",
        _fake_request,
    )
    client = SteamWorkshopClient(
        db=db, request_interval=0, enable_scrape_fallback=False
    )
    try:
        out = client.get_details_batch([WORKSHOP_ID, "111222333"])
    finally:
        client.close()

    assert any(m.title == "From Steam API" for m in out)
    info = db.get_mod_display_info(str(INTERNAL_ID))
    assert info is not None
    assert info.steam_name == "From Steam API"
    assert db.get_mod("111222333") is None
    assert (
        db._conn.execute(
            "SELECT COUNT(*) AS n FROM mods WHERE mod_id = ?",
            (111222333,),
        ).fetchone()["n"]
        == 0
    )
    assert (
        db._conn.execute(
            "SELECT COUNT(*) AS n FROM mods WHERE mod_id = ?",
            (int(WORKSHOP_ID),),
        ).fetchone()["n"]
        == 0
    )


def test_upsert_mods_resolves_workshop_to_internal_pk(db: DatabaseManager) -> None:
    _seed_split_entity(db)
    n = db.upsert_mods(
        [
            ModMetadata(
                published_file_id=WORKSHOP_ID,
                title="Batch Title",
                app_id=APP_ID,
            )
        ]
    )
    assert n >= 1
    info = db.get_mod_display_info(str(INTERNAL_ID))
    assert info is not None
    assert info.steam_name == "Batch Title"
    assert db.get_mod(WORKSHOP_ID) is None


def test_resolve_steam_entity_mod_id_prefers_internal(db: DatabaseManager) -> None:
    _seed_split_entity(db)
    assert (
        db.resolve_steam_entity_mod_id(WORKSHOP_ID, app_id=APP_ID) == str(INTERNAL_ID)
    )
    assert db.resolve_steam_entity_mod_id("404404404", app_id=APP_ID) is None
