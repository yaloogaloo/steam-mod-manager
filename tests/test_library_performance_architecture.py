"""Architecture guards: Library list is DB-index + viewport, not filesystem-driven.

Lifecycle contract
------------------
Before: game switch → full ModCardWidget tree (+ cold path list_visible_mods).
After:  game switch → SQL ModListItem index → viewport window of cards.

Forbidden on Library list path:
- list_visible_mods
- load_backup
- reconcile_library
- sync_after_metadata_change
- filesystem rglob of managed mods
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.mod_library_cache import (
    build_library_snapshot,
    list_item_to_card_data,
    reset_library_cache,
)
from services.mod_list_item import (
    MOD_LIST_ITEM_FORBIDDEN_FIELDS,
    ModListItem,
    assert_mod_list_item_layer1,
)
from ui.library_viewport import compute_viewport_window, estimate_total_height


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "lib_perf_arch.db")
    yield manager
    DatabaseManager.reset_instance()
    reset_library_cache()


def test_mod_list_item_forbids_heavy_fields() -> None:
    item = ModListItem(
        internal_id="1",
        workspace_id="1",
        game_id=1,
        game_folder="G",
        name="N",
    )
    assert_mod_list_item_layer1(item)
    names = {f.name for f in item.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    assert not (names & MOD_LIST_ITEM_FORBIDDEN_FIELDS)
    assert "description" not in names
    card = list_item_to_card_data(item)
    assert card.description == ""


def test_build_library_snapshot_source_forbids_filesystem_resolve() -> None:
    src = inspect.getsource(build_library_snapshot)
    # Strip docstring — architecture notes may mention forbidden APIs.
    body = src.split('"""', 2)[-1] if '"""' in src else src
    assert "list_visible_mods" not in body
    assert "load_backup" not in body
    assert "list_managed_mods" not in body
    assert "rglob" not in body
    assert "list_mod_list_items" in body


def test_mod_library_cache_ast_forbids_list_visible_mods() -> None:
    path = ROOT / "services" / "mod_library_cache.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            assert name != "list_visible_mods"
            assert name != "load_backup"
            assert name != "reconcile_library"


def test_db_list_mod_list_items_by_game_folder(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from core.game_info import GameInfo

    lib = tmp_path / "mod"
    game = "CivGame"
    db.upsert_game(GameInfo(app_id=289070, name=game, folder_name=game))
    for i in range(5):
        mid = str(200100 + i)
        folder = lib / game / f"M{i}"
        folder.mkdir(parents=True)
        db.upsert_mod(
            ModMetadata(
                published_file_id=mid,
                title=f"M{i}",
                app_id=289070,
                game_name=game,
                managed_path=str(folder),
            )
        )
        db.update_mod_identity_fields(
            mid,
            folder_present=True,
            last_known_path=str(folder),
            app_id=289070,
        )

    rows = db.list_mod_list_items(game_folder=game)
    assert len(rows) == 5
    allowed = set(ModListItem.__dataclass_fields__)
    for row in rows:
        item = ModListItem(**{k: row[k] for k in allowed if k in row})
        assert_mod_list_item_layer1(item)


def test_snapshot_is_db_first(db: DatabaseManager, tmp_path: Path) -> None:
    from core.game_info import GameInfo

    lib = tmp_path / "mod"
    game = "SnapGame"
    db.upsert_game(GameInfo(app_id=11, name=game, folder_name=game))
    folder = lib / game / "Only"
    folder.mkdir(parents=True)
    db.upsert_mod(
        ModMetadata(
            published_file_id="300001",
            title="Only",
            app_id=11,
            description="HEAVY SHOULD NOT APPEAR",
            managed_path=str(folder),
        )
    )
    db.update_mod_identity_fields(
        "300001",
        folder_present=True,
        last_known_path=str(folder),
        app_id=11,
    )
    reset_library_cache()
    snap = build_library_snapshot(lib)
    assert snap.total_count == 1
    assert snap.cards[0].description == ""
    assert snap.list_items
    assert snap.list_items[0].name


def test_viewport_window_does_not_cover_entire_library() -> None:
    window = compute_viewport_window(
        item_count=10_000,
        scroll_y=0,
        viewport_width=900,
        viewport_height=700,
    )
    span = window.last_index - window.first_index
    assert span < 500
    assert span > 0
    assert estimate_total_height(10_000, 900) > 50_000


def test_backup_default_path_enqueues_not_inline() -> None:
    src = inspect.getsource(
        __import__(
            "services.metadata_backup_sync", fromlist=["sync_after_metadata_change"]
        ).sync_after_metadata_change
    )
    assert "mark_backup_dirty" in src
    assert "wait" in src
