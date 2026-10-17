"""Library Projection P1 — identity migration to internal_id only."""

from __future__ import annotations

import inspect
import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.mod_library_cache import (
    build_library_snapshot,
    get_library_cache,
    reset_library_cache,
)
from services.mod_projection_events import notify_mod_changed, reset_mod_changed_listeners

STARDEW = 413150
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_mod_changed_listeners()
    manager = DatabaseManager.instance(tmp_path / "p1_proj.db")
    manager.upsert_game(GameInfo(app_id=STARDEW, name="Stardew Valley"))
    manager.upsert_game(GameInfo(app_id=BG3, name="Baldurs Gate 3"))
    yield manager
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_mod_changed_listeners()


def _meta(folder: Path, payload: dict) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _seed_nexus(
    db: DatabaseManager,
    library: Path,
    *,
    external_id: str,
    app_id: int,
    game: str,
    title: str,
    folder_name: str,
    url: str,
) -> tuple[str, Path]:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=external_id,
        source_url=url,
        title=title,
        app_id=app_id,
        game_name=game,
        operation="import",
    )
    mid = str(created.mod_id)
    folder = library / game / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pak.bin").write_bytes(b"x")
    _meta(
        folder,
        {
            "internal_id": mid,
            "workspace_id": external_id,
            "platform": PLATFORM_NEXUS,
            "app_id": app_id,
            "title": title,
            "url": url,
        },
    )
    db.update_mod_identity_fields(
        mid,
        internal_id=mid,
        workspace_id=external_id,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        app_id=app_id,
    )
    return mid, folder


def test_same_workspace_id_two_internal_entities(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    bg3_mid, _ = _seed_nexus(
        db,
        library,
        external_id="1333",
        app_id=BG3,
        game="Baldurs Gate 3",
        title="Community Library",
        folder_name="Community Library",
        url="https://www.nexusmods.com/baldursgate3/mods/1333",
    )
    sd_mid, _ = _seed_nexus(
        db,
        library,
        external_id="1333",
        app_id=STARDEW,
        game="Stardew Valley",
        title="Carry Chest",
        folder_name="Carry Chest",
        url="https://www.nexusmods.com/stardewvalley/mods/1333",
    )
    snap = build_library_snapshot(library)
    ids = {c.id for c in snap.cards}
    assert bg3_mid in ids
    assert sd_mid in ids
    assert bg3_mid != sd_mid
    assert len([c for c in snap.cards if c.workspace_id == "1333"]) == 2


def test_delete_folder_does_not_create_new_entity(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        external_id="9001",
        app_id=STARDEW,
        game="Stardew Valley",
        title="Carry",
        folder_name="Carry",
        url="https://www.nexusmods.com/stardewvalley/mods/9001",
    )
    before = len(build_library_snapshot(library).cards)
    shutil.rmtree(folder)
    db.update_mod_identity_fields(mid, folder_present=False)
    snap = build_library_snapshot(library)
    assert len(snap.cards) == before
    match = [c for c in snap.cards if c.id == mid]
    assert len(match) == 1
    assert match[0].folder_absent is True
    assert db.get_mod(mid) is not None


def test_rename_folder_keeps_same_internal_id(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mid, folder = _seed_nexus(
        db,
        library,
        external_id="9002",
        app_id=STARDEW,
        game="Stardew Valley",
        title="OldTitle",
        folder_name="OldTitle",
        url="https://www.nexusmods.com/stardewvalley/mods/9002",
    )
    new = folder.parent / "NewTitle"
    folder.rename(new)
    db.update_mod_identity_fields(
        mid, last_known_path=str(new.resolve()), folder_present=True
    )
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)
    notify_mod_changed(mid)
    card = cache.get_card_data(mid)
    assert card is not None
    assert card.id == mid
    assert Path(card.managed_path).resolve() == new.resolve()
    assert len([c for c in cache._all if c.id == mid]) == 1


def test_metadata_change_refreshes_single_entity(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mid, _folder = _seed_nexus(
        db,
        library,
        external_id="9003",
        app_id=STARDEW,
        game="Stardew Valley",
        title="Before",
        folder_name="Before",
        url="https://www.nexusmods.com/stardewvalley/mods/9003",
    )
    other, _ = _seed_nexus(
        db,
        library,
        external_id="9004",
        app_id=STARDEW,
        game="Stardew Valley",
        title="Other",
        folder_name="Other",
        url="https://www.nexusmods.com/stardewvalley/mods/9004",
    )
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)
    other_before = cache.get_card_data(other)
    assert other_before is not None
    other_title = other_before.title

    db.update_mod_user_metadata(mid, {"display_name": "AfterName"})
    notify_mod_changed(mid)
    patched = cache.get_card_data(mid)
    assert patched is not None
    assert patched.title == "AfterName"
    untouched = cache.get_card_data(other)
    assert untouched is not None
    assert untouched.title == other_title


def test_card_consumes_mod_card_data_only() -> None:
    """ModCardWidget must not call DB / registration / filesystem identity APIs."""
    src = inspect.getsource(
        __import__("ui.mod_card", fromlist=["ModCardWidget"]).ModCardWidget
    )
    forbidden = (
        "get_db(",
        "find_mod_for_registration(",
        "find_mod_by_external(",
        "find_mod_by_workspace_id(",
        "list_visible_mods(",
        "get_mod_backup_row_by_path(",
        "ensure_mod_identity(",
    )
    for call in forbidden:
        assert call not in src, f"ModCard must not call {call}"
    assert "_card_data" in src
    mod_src = Path("ui/mod_card.py").read_text(encoding="utf-8")
    assert "selection_requested = Signal(str)" in mod_src
    assert "Signal(object)  # Path managed_path" not in mod_src


def test_viewport_cache_key_is_internal_id() -> None:
    from ui.library_view import ModLibraryView

    key = ModLibraryView._card_cache_key(Path("D:/anywhere/renamed"), mod_id="9001")
    assert key == "entity:9001"
    assert ModLibraryView._card_cache_key(Path("D:/x"), mod_id="") == ""


def test_library_view_forbids_registration_lookups() -> None:
    src = Path("ui/library_view.py").read_text(encoding="utf-8")
    for call in (
        "find_mod_for_registration(",
        "find_mod_by_external(",
        "find_mod_by_workspace_id(",
        "list_visible_mods(",
        "get_mod_backup_row_by_path(",
        "resolve_mod_metadata(None,",
    ):
        assert call not in src, f"LibraryView must not call {call}"
