"""Category tags (mod_tags tag_type=category)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import TAG_TYPE_CATEGORY, DatabaseManager
from core.models import ModMetadata
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
    db.upsert_mod(ModMetadata(published_file_id="901", title="T"))
    db.add_category_tag(901, "Gameplay")
    db.add_category_tag(901, "Fix")
    db.add_category_tag(901, "Gameplay")  # duplicate ignored
    tags = db.get_category_tags(901)
    assert tags == ["Gameplay", "Fix"]
    assert "Gameplay" in db.list_all_category_tags()
    assert db.remove_category_tag(901, "Fix") == 1
    assert db.get_category_tags(901) == ["Gameplay"]
    raw = db.get_mod_tags(901)
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
        category_tags="Gameplay Fix",
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
        category_tags="Graphics",
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
        category_tags="Gameplay",
    )
    assert matches_category_filter(a, "Gameplay")
    assert not matches_category_filter(b, "Gameplay")
    out = filter_and_sort(
        [(a, "a"), (b, "b"), (c, "c")],
        platform_key=FILTER_PLATFORM_NEXUS,
        category_key="Gameplay",
    )
    assert out == ["a"]
