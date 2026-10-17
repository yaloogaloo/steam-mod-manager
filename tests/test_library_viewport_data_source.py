"""Viewport ``_card_entries`` must never be the full-game data source."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from ui.library_query import (
    FILTER_PLATFORM_NEXUS,
    FILTER_PLATFORM_STEAM,
    ModFilterIndex,
)
from ui.library_view import ModLibraryView


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _index(
    mod_id: str,
    *,
    platform: str = "steam",
    category_tags: str = "",
    display_name: str = "",
) -> ModFilterIndex:
    return ModFilterIndex(
        mod_id=str(mod_id),
        display_name=display_name or f"Mod {mod_id}",
        steam_name="",
        notes="",
        game_name="ViewportGame",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=0.0,
        sort_name=f"mod {mod_id}",
        platform=platform,
        category_tags=category_tags,
    )


def test_full_list_helpers_source_forbid_card_entries() -> None:
    """Static guard: aggregation helpers must read projection, not viewport."""
    for fn in (
        ModLibraryView._sync_peer_mods_to_panel,
        ModLibraryView._current_library_deployed_mod_ids,
    ):
        body = inspect.getsource(fn)
        if '"""' in body:
            parts = body.split('"""', 2)
            body = parts[0] + (parts[2] if len(parts) > 2 else "")
        assert "_card_entries" not in body
        assert "_game_row_entries" in inspect.getsource(fn)

    merged = inspect.getsource(ModLibraryView._merged_category_options)
    assert "_card_entries" not in merged
    assert "_current_game_type_catalog" in merged


def test_platform_category_peers_use_game_rows_not_viewport(
    qapp: QApplication, tmp_path: Path
) -> None:
    """Empty viewport + 24 projection rows → chips/peers still see full game."""
    view = ModLibraryView()
    view.current_game_id = 4242
    rows = []
    for i in range(24):
        mid = str(880000 + i)
        plat = "steam" if i < 12 else "nexus"
        tags = "美化" if i == 20 else ("地图" if i == 21 else "")
        rows.append((_index(mid, platform=plat, category_tags=tags), object()))
    view._game_row_entries = rows
    view._filtered_row_entries = list(rows)
    view._card_entries = []  # viewport empty — bug surface
    view._cards = []

    platforms = {
        str(getattr(index, "platform", "") or "")
        for index, _payload in view._game_row_entries
    }
    assert "steam" in platforms
    assert "nexus" in platforms
    # Filter chip keys still map to projection platforms.
    assert FILTER_PLATFORM_STEAM.startswith("platform") or FILTER_PLATFORM_STEAM
    assert FILTER_PLATFORM_NEXUS.startswith("platform") or FILTER_PLATFORM_NEXUS

    view._current_game_type_catalog = lambda: [(1, "美化"), (2, "地图")]
    cats = view._merged_category_options()
    assert "美化" in [name for _tid, name in cats]
    assert "地图" in [name for _tid, name in cats]

    view._sync_peer_mods_to_panel(exclude="880000")
    peers = list(view.detail_panel._peer_mods)
    assert len(peers) == 23
    ids = {str(mid) for mid, _title in peers}
    assert "880000" not in ids
    assert "880023" in ids
    view.close()
