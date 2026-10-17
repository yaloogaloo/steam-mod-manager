"""Category tags (mod_tags tag_type=category)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import TAG_TYPE_CATEGORY, DatabaseManager
from core.models import ModMetadata
from tests.helpers.identity import create_steam_test_mod
from ui.library_query import (
    FILTER_PLATFORM_NEXUS,
    ModFilterIndex,
    filter_and_sort,
    matches_category_filter,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "tags.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_add_remove_category_tags(db: DatabaseManager) -> None:
    pk = create_steam_test_mod(db, external_id="901", title="T").mod_id

    db.add_category_tag(pk, "Gameplay")
    db.add_category_tag(pk, "Fix")
    db.add_category_tag(pk, "Gameplay")  # duplicate ignored
    tags = db.get_category_tags(pk)
    assert tags == ["Gameplay", "Fix"]
    assert "Gameplay" in db.list_all_category_tags()
    assert db.remove_category_tag(pk, "Fix") == 1
    assert db.get_category_tags(pk) == ["Gameplay"]
    raw = db.get_mod_tags(pk)
    assert all(t.tag_type == TAG_TYPE_CATEGORY for t in raw)


def test_category_filter_combined() -> None:
    a = ModFilterIndex(
        mod_id="1",
        display_name="A",
        steam_name="",
        notes="",
        game_name="",
        favorite=False,
        deployed=False,
        has_offline=True,
        mtime=1,
        sort_name="A",
        platform="nexus",
        type_id=12,
        category_tags="装备",
    )
    b = ModFilterIndex(
        mod_id="2",
        display_name="B",
        steam_name="",
        notes="",
        game_name="",
        favorite=False,
        deployed=False,
        has_offline=True,
        mtime=1,
        sort_name="B",
        platform="nexus",
        type_id=13,
        category_tags="法术",
    )
    c = ModFilterIndex(
        mod_id="3",
        display_name="C",
        steam_name="",
        notes="",
        game_name="",
        favorite=False,
        deployed=False,
        has_offline=True,
        mtime=1,
        sort_name="C",
        platform="steam",
        type_id=12,
        category_tags="装备",
    )
    assert matches_category_filter(a, "12")
    assert not matches_category_filter(b, "12")
    assert not matches_category_filter(a, "装备")
    out = filter_and_sort(
        [(a, "a"), (b, "b"), (c, "c")],
        platform_key=FILTER_PLATFORM_NEXUS,
        category_key="12",
    )
    assert out == ["a"]
