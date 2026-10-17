"""Identity consumer audit — Nexus cross-game import + metadata refresh."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from services.offline.manager import attach_nexus_offline_page


STARDEW = 413150
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "consumer_audit.db")
    manager.upsert_game(
        GameInfo(app_id=STARDEW, name="Stardew Valley", folder_name="Stardew Valley")
    )
    manager.upsert_game(
        GameInfo(app_id=BG3, name="Baldurs Gate 3", folder_name="Baldurs Gate 3")
    )
    yield manager
    DatabaseManager.reset_instance()


def _folder(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    (folder / "mod.dll").write_bytes(b"x")
    return folder


def _offline_html(tmp_path: Path, *, game: str, mod_id: str, title: str) -> Path:
    path = tmp_path / f"{game}_{mod_id}.html"
    path.write_text(
        f"""<!DOCTYPE html>
<html><head>
<meta property="og:url" content="https://www.nexusmods.com/{game}/mods/{mod_id}"/>
<meta property="og:title" content="{title}"/>
</head><body><section data-mod-id="{mod_id}"></section>
<p class="mod_description">Offline body for {title}</p>
</body></html>
""",
        encoding="utf-8",
    )
    return path


def test_nexus_same_external_id_different_game_creates_distinct_entity(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Stardew 1333 must not reuse BG3 Community Library 1333."""
    lib = tmp_path / "lib"
    lib.mkdir()

    bg3 = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/baldursgate3/mods/1333",
        title="Community Library",
        app_id=BG3,
        game_name="Baldurs Gate 3",
        operation="import",
    )
    assert bg3.created or bg3.mod_id

    result = NexusImporter(db=db).import_mod(
        source_folder=_folder(tmp_path, "CarryChest"),
        title="Carry Chest",
        nexus_url="https://www.nexusmods.com/stardewvalley/mods/1333",
        nexus_id="1333",
        library_root=lib,
        context=ImportContext(game_id=STARDEW, game_name="Stardew Valley"),
    )
    assert result.success, result.error
    assert str(result.mod_id) != str(bg3.mod_id)

    bg3_info = db.get_mod_display_info(bg3.mod_id)
    sd_info = db.get_mod_display_info(result.mod_id)
    assert bg3_info is not None and sd_info is not None
    assert int(bg3_info.app_id) == BG3
    assert int(sd_info.app_id) == STARDEW
    assert "Community Library" in (bg3_info.display_name or bg3_info.title or "")
    assert "Carry Chest" in (sd_info.display_name or sd_info.title or "")
    assert db.find_mod_by_external(PLATFORM_NEXUS, "1333", app_id=BG3) is not None
    assert db.find_mod_by_external(PLATFORM_NEXUS, "1333", app_id=STARDEW) is not None


def test_import_existing_entity_refreshes_metadata(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Same-scope re-import must rewrite title/source_url onto the existing entity."""
    first = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="7777",
        source_url="https://www.nexusmods.com/stardewvalley/mods/7777",
        title="Old Title",
        app_id=STARDEW,
        game_name="Stardew Valley",
        operation="import",
    )
    mid = str(first.mod_id)
    again = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="7777",
        source_url="https://www.nexusmods.com/stardewvalley/mods/7777",
        title="Fresh Title",
        app_id=STARDEW,
        game_name="Stardew Valley",
        operation="import",
    )
    assert str(again.mod_id) == mid
    assert again.reused or not again.created
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert "Fresh Title" in (info.display_name or info.title or "")
    assert "stardewvalley/mods/7777" in str(info.source_url or "")


def test_offline_archive_does_not_reuse_cross_game_identity(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Offline page for Stardew 1333 must not rewrite a BG3 1333 entity."""
    bg3 = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/baldursgate3/mods/1333",
        title="Community Library",
        app_id=BG3,
        game_name="Baldurs Gate 3",
        operation="import",
    )
    mid = str(bg3.mod_id)
    folder = tmp_path / "BG3" / "Community Library"
    info_dir = folder / INFO_DIR_NAME
    info_dir.mkdir(parents=True)
    (info_dir / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "internal_id": mid,
                "workspace_id": "1333",
                "external_id": "1333",
                "platform": PLATFORM_NEXUS,
                "app_id": BG3,
                "title": "Community Library",
                "url": "https://www.nexusmods.com/baldursgate3/mods/1333",
            }
        ),
        encoding="utf-8",
    )
    db.update_mod_identity_fields(
        mid, last_known_path=str(folder.resolve()), folder_present=True
    )

    html = _offline_html(
        tmp_path, game="stardewvalley", mod_id="1333", title="Carry Chest"
    )
    attach_nexus_offline_page(
        mid,
        html,
        managed_path=folder,
        merge_mode="import_overwrite",
    )
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert int(info.app_id) == BG3
    assert "Community Library" in (info.display_name or info.title or "")
    assert "baldursgate3" in str(info.source_url or "")
    assert "Carry Chest" not in (info.display_name or info.title or "")


def test_polluted_hybrid_row_does_not_absorb_cross_game_import(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Historical hybrid (Stardew app_id + BG3 URL) must not absorb Stardew 1333.

    Unique (platform, app_id, external_id) may be occupied by pollution; import
    must refuse rewrite rather than overwrite Community Library with Carry Chest.
    """
    lib = tmp_path / "lib"
    lib.mkdir()
    hybrid = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/baldursgate3/mods/1333",
        title="Community Library",
        app_id=STARDEW,
        game_name="Stardew Valley",
        operation="import",
    )
    result = NexusImporter(db=db).import_mod(
        source_folder=_folder(tmp_path, "CarryChest"),
        title="Carry Chest",
        nexus_url="https://www.nexusmods.com/stardewvalley/mods/1333",
        nexus_id="1333",
        library_root=lib,
        context=ImportContext(game_id=STARDEW, game_name="Stardew Valley"),
    )
    assert not result.success
    assert "conflict" in (result.error or "").lower()
    hybrid_info = db.get_mod_display_info(hybrid.mod_id)
    assert hybrid_info is not None
    assert "Community Library" in (hybrid_info.display_name or hybrid_info.title or "")
    assert "baldursgate3" in str(hybrid_info.source_url or "")
    assert "Carry Chest" not in (hybrid_info.display_name or hybrid_info.title or "")
