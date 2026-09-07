"""Final Identity Authority closure — workspace_id is display-only."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import PLATFORM_NEXUS, PLATFORM_STEAM, DatabaseManager
from core.game_info import GameInfo
from services.identity_pollution import (
    apply_identity_pollution_repair,
    scan_identity_pollution,
)
from services.identity_service import create_mod_identity, identity_create_scope
from services.mod_identity import resolve_existing_mod_id
from services.mod_identity_authority import resolve_mod_identity

ROOT = Path(__file__).resolve().parents[1]
STARDEW = 413150
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "identity_final.db")
    manager.upsert_game(
        GameInfo(app_id=STARDEW, name="Stardew Valley", folder_name="Stardew Valley")
    )
    manager.upsert_game(
        GameInfo(app_id=BG3, name="Baldurs Gate 3", folder_name="Baldurs Gate 3")
    )
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_steam_and_nexus_same_digits_are_distinct_entities(db: DatabaseManager) -> None:
    with identity_create_scope():
        steam = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id="1333",
            workshop_id="1333",
            title="Steam Workshop 1333",
            app_id=BG3,
        )
        nexus = create_mod_identity(
            db,
            platform=PLATFORM_NEXUS,
            external_id="1333",
            source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
            title="Carry Chest",
            app_id=STARDEW,
            game_name="Stardew Valley",
        )
    assert steam.mod_id
    assert nexus.mod_id
    assert str(steam.mod_id) != str(nexus.mod_id)
    s = db.get_mod_display_info(steam.mod_id)
    n = db.get_mod_display_info(nexus.mod_id)
    assert s is not None and n is not None
    assert str(s.platform).lower() == PLATFORM_STEAM
    assert str(n.platform).lower() == PLATFORM_NEXUS


def test_workspace_lookup_ambiguous_returns_empty(db: DatabaseManager) -> None:
    a = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/baldursgate3/mods/1333",
        title="Community Library",
        app_id=BG3,
        game_name="Baldurs Gate 3",
    )
    b = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
        title="Carry Chest",
        app_id=STARDEW,
        game_name="Stardew Valley",
    )
    # Force historical collision shape (same display id, different games).
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET workspace_id = ? WHERE mod_id IN (?, ?)",
            ("1333", int(a.mod_id), int(b.mod_id)),
        )
        db._conn.commit()
    assert db.find_mod_by_workspace_id("1333", platform=PLATFORM_NEXUS, app_id=0) is None
    assert str(
        db.find_mod_by_external(PLATFORM_NEXUS, "1333", app_id=BG3).mod_id
    ) != str(
        db.find_mod_by_external(PLATFORM_NEXUS, "1333", app_id=STARDEW).mod_id
    )


def test_app_id_zero_excluded_from_identity_bind(db: DatabaseManager) -> None:
    with identity_create_scope(), db._lock:
        dirty = int(db.allocate_mod_id())
        db._conn.execute(
            """
            UPDATE mods SET platform=?, app_id=0, title=?, external_id=?,
                   source_url=?, workspace_id=?, display_name=?
            WHERE mod_id=?
            """,
            (
                PLATFORM_NEXUS,
                "Community Library",
                "1333",
                "https://www.nexusmods.com/baldursgate3/mods/1333",
                "1333",
                "Community Library",
                dirty,
            ),
        )
        db._conn.commit()

    hit = resolve_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
        app_id=STARDEW,
    )
    assert hit.mod_id == ""

    existing = resolve_existing_mod_id(
        {
            "platform": PLATFORM_NEXUS,
            "external_id": "1333",
            "workspace_id": "1333",
            "app_id": STARDEW,
            "url": "https://www.nexusmods.com/stardewvalley/mods/1333",
        },
        db=db,
    )
    assert existing == ""


def test_resolve_existing_ignores_workspace_id(db: DatabaseManager) -> None:
    db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="9991",
        source_url="https://www.nexusmods.com/baldursgate3/mods/9991",
        title="BG3 Only",
        app_id=BG3,
        game_name="Baldurs Gate 3",
    )
    found = resolve_existing_mod_id(
        {
            "platform": PLATFORM_NEXUS,
            "external_id": "9991",
            "workspace_id": "9991",
            "app_id": STARDEW,
            "url": "https://www.nexusmods.com/stardewvalley/mods/9991",
        },
        db=db,
    )
    assert found == ""


def test_authority_modules_forbid_workspace_identity_lookup() -> None:
    auth = (ROOT / "services/mod_identity_authority.py").read_text(encoding="utf-8")
    resolve_fn = auth.split("def resolve_mod_identity")[1].split(
        "def create_mod_identity"
    )[0]
    assert "find_mod_by_workspace_id(" not in resolve_fn
    ident = (ROOT / "services/mod_identity.py").read_text(encoding="utf-8")
    body = ident.split("def resolve_existing_mod_id")[1].split(
        "def ensure_mod_identity"
    )[0]
    assert "find_mod_by_workspace_id(" not in body
    assert "_lookup_workspace(" not in body
    rec = (ROOT / "services/library_reconcile.py").read_text(encoding="utf-8")
    assert "find_mod_by_workspace_id(" not in rec


def test_pollution_repair_clears_cross_game_workspace(db: DatabaseManager) -> None:
    a = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="6183",
        source_url="https://www.nexusmods.com/baldursgate3/mods/6183",
        title="Sit This One Out 2",
        app_id=BG3,
        game_name="Baldurs Gate 3",
    )
    with identity_create_scope(), db._lock:
        dirty = int(db.allocate_mod_id())
        db._conn.execute(
            """
            UPDATE mods SET platform=?, app_id=0, title=?, external_id=?,
                   source_url=?, workspace_id=?, display_name=?
            WHERE mod_id=?
            """,
            (
                PLATFORM_NEXUS,
                "Train Station",
                "6183",
                "https://www.nexusmods.com/stardewvalley/mods/6183",
                str(a.workspace_id or "6183"),
                "Train Station",
                dirty,
            ),
        )
        db._conn.commit()

    report = scan_identity_pollution(db)
    assert report.cross_game_workspace or report.app_id_zero
    apply_identity_pollution_repair(db, report, apply=True)
    again = scan_identity_pollution(db)
    assert len(again.cross_game_workspace) == 0


def test_import_create_goes_through_authority_only() -> None:
    nexus = (ROOT / "services/importers/nexus.py").read_text(encoding="utf-8")
    assert "create_mod_identity(" in nexus
