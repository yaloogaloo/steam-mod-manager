"""Library viewport layout lifecycle — first row must pack full columns.

Lifecycle bug this locks:
  Vertical scroll pads were FlowLayout peers → first row lost one card slot.
Correct lifecycle:
  VBox[top_pad | cards FlowLayout | bottom_pad] — FlowLayout peers are cards only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager
from services.mod_library_cache import reset_library_cache
from ui.library_query import (
    FILTER_CATEGORY_ALL,
    FILTER_DEPLOYMENT_RECORD,
    FILTER_FAVORITE,
    SORT_NAME,
)
from ui.library_view import LIBRARY_CARDS_PER_ROW, ModLibraryView
from ui.library_viewport import (
    CARD_SLOT_HEIGHT,
    VIEWPORT_ROW_BUFFER,
    clamp_scroll_y,
    compute_viewport_window,
    estimate_columns,
    estimate_total_height,
)
from ui.mod_card import CARD_WIDTH, ModCardWidget


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "viewport_layout.db")
    yield manager
    DatabaseManager.reset_instance()
    reset_library_cache()


def _seed(
    lib: Path,
    db: DatabaseManager,
    game: str,
    n: int,
    *,
    app_id: int,
    title_prefix: str | None = None,
    hit_count: int = 0,
) -> None:
    db.upsert_game(GameInfo(app_id=app_id, name=game, folder_name=game))
    for i in range(n):
        mid = str(app_id * 1000 + i)
        title = (
            f"{title_prefix}{i:03d}"
            if title_prefix and i < hit_count
            else f"Mod{i:03d}"
        )
        folder = lib / game / f"Mod{i:03d}"
        info = folder / INFO_DIR_NAME
        info.mkdir(parents=True)
        (info / METADATA_FILENAME).write_text(
            json.dumps(
                {
                    "published_file_id": mid,
                    "title": title,
                    "game_name": game,
                    "app_id": app_id,
                }
            ),
            encoding="utf-8",
        )
        db.upsert_mod(
            ModMetadata(
                published_file_id=mid,
                title=title,
                app_id=app_id,
                game_name=game,
                managed_path=str(folder),
            )
        )
        db.update_mod_identity_fields(
            mid,
            folder_present=True,
            last_known_path=str(folder),
            app_id=app_id,
        )


def _cards_on_first_row(view: ModLibraryView) -> list[ModCardWidget]:
    cards = [c for c in view._cards if isinstance(c, ModCardWidget) and c.isVisible()]
    if not cards:
        return []
    # Force a layout pass so geometries are current.
    view.library_layout.invalidate()
    host = view._cards_host or view.library_host
    host.updateGeometry()
    view.library_layout.setGeometry(host.rect() if host.width() > 0 else QRect(0, 0, 900, 600))
    tops = sorted({int(c.geometry().y()) for c in cards})
    first_y = tops[0]
    return [c for c in cards if int(c.geometry().y()) == first_y]


def _flow_card_count(view: ModLibraryView) -> int:
    n = 0
    for i in range(view.library_layout.count()):
        item = view.library_layout.itemAt(i)
        w = item.widget() if item is not None else None
        if isinstance(w, ModCardWidget):
            n += 1
    return n


def test_estimate_columns_matches_four_card_center_width() -> None:
    # Center pane sized for 4 cards must report 4 columns.
    width = (
        LIBRARY_CARDS_PER_ROW * CARD_WIDTH
        + (LIBRARY_CARDS_PER_ROW - 1) * 8
        + 2 * 2
    )
    assert estimate_columns(width) == 4
    window = compute_viewport_window(
        item_count=12,
        scroll_y=0,
        viewport_width=width,
        viewport_height=700,
    )
    assert window.columns == 4
    assert window.first_index == 0


def test_flow_layout_peers_are_cards_only_not_spacers(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "mod"
    _seed(lib, db, "LayoutGame", 12, app_id=501)
    view = ModLibraryView()
    view.resize(1100, 800)
    view.show()
    qapp.processEvents()
    view.set_target_root(str(lib))
    view.refresh(force=True, reconcile=False)
    qapp.processEvents()
    view._current_game_filter = "LayoutGame"
    view._render_mod_cards(ModFileManager(lib), force_reload=False)
    qapp.processEvents()
    view._sync_viewport_cards()
    qapp.processEvents()

    for i in range(view.library_layout.count()):
        item = view.library_layout.itemAt(i)
        w = item.widget() if item is not None else None
        assert w is not None
        assert isinstance(w, ModCardWidget), (
            f"FlowLayout peer must be ModCardWidget, got {type(w).__name__}"
        )
    assert view._viewport_top_spacer is not None
    assert view._viewport_top_spacer.parent() is view.library_host
    assert view.library_layout.indexOf(view._viewport_top_spacer) == -1


def test_initial_open_first_row_has_four_cards(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "mod"
    _seed(lib, db, "LayoutGame", 16, app_id=502)
    view = ModLibraryView()
    # Width large enough for 4 cards in the scroll viewport.
    view.resize(1200, 900)
    view.show()
    qapp.processEvents()
    view.set_target_root(str(lib))
    view.refresh(force=True, reconcile=False)
    qapp.processEvents()
    view._current_game_filter = "LayoutGame"
    view._render_mod_cards(ModFileManager(lib), force_reload=False)
    qapp.processEvents()
    view._sync_viewport_cards()
    qapp.processEvents()

    first = _cards_on_first_row(view)
    assert len(first) == LIBRARY_CARDS_PER_ROW, (
        f"first row expected {LIBRARY_CARDS_PER_ROW} cards, got {len(first)}"
    )
    assert _flow_card_count(view) == view.library_layout.count()


def test_resize_keeps_four_columns(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "mod"
    _seed(lib, db, "LayoutGame", 16, app_id=503)
    view = ModLibraryView()
    view.resize(1200, 900)
    view.show()
    qapp.processEvents()
    view.set_target_root(str(lib))
    view.refresh(force=True, reconcile=False)
    qapp.processEvents()
    view._current_game_filter = "LayoutGame"
    view._render_mod_cards(ModFileManager(lib), force_reload=False)
    qapp.processEvents()
    view._sync_viewport_cards()
    qapp.processEvents()

    view.resize(1300, 900)
    qapp.processEvents()
    view._sync_viewport_cards()
    qapp.processEvents()

    first = _cards_on_first_row(view)
    assert len(first) == LIBRARY_CARDS_PER_ROW


def test_game_switch_does_not_shorten_first_row(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "mod"
    _seed(lib, db, "GameA", 16, app_id=504)
    _seed(lib, db, "GameB", 16, app_id=505)
    view = ModLibraryView()
    view.resize(1200, 900)
    view.show()
    qapp.processEvents()
    view.set_target_root(str(lib))
    view.refresh(force=True, reconcile=False)
    qapp.processEvents()

    for game in ("GameA", "GameB", "GameA"):
        view._current_game_filter = game
        view._render_mod_cards(ModFileManager(lib), force_reload=False)
        qapp.processEvents()
        view._set_scroll_value(0)
        view._sync_viewport_cards()
        qapp.processEvents()
        first = _cards_on_first_row(view)
        assert len(first) == LIBRARY_CARDS_PER_ROW, f"{game}: first row={len(first)}"


def test_scroll_reuse_does_not_insert_pads_into_flow(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "mod"
    _seed(lib, db, "LayoutGame", 40, app_id=506)
    view = ModLibraryView()
    view.resize(1200, 900)
    view.show()
    qapp.processEvents()
    view.set_target_root(str(lib))
    view.refresh(force=True, reconcile=False)
    qapp.processEvents()
    view._current_game_filter = "LayoutGame"
    view._render_mod_cards(ModFileManager(lib), force_reload=False)
    qapp.processEvents()
    view._sync_viewport_cards()
    qapp.processEvents()
    before = _flow_card_count(view)
    assert before > 0

    view._set_scroll_value(400)
    view._sync_viewport_cards()
    qapp.processEvents()
    after = _flow_card_count(view)
    assert after == view.library_layout.count()
    # Still only cards in the flow; count stays in the viewport window band.
    assert after < 40
    first = _cards_on_first_row(view)
    assert len(first) == LIBRARY_CARDS_PER_ROW


def test_clamp_scroll_y_rejects_stale_offset_on_short_list() -> None:
    """Leftover scroll from a long list must not produce a tail window of 2/7."""
    stale = 660
    y = clamp_scroll_y(
        scroll_y=stale,
        item_count=7,
        viewport_width=1100,
        viewport_height=700,
    )
    assert y == 0
    window = compute_viewport_window(
        item_count=7,
        scroll_y=y,
        viewport_width=1100,
        viewport_height=700,
    )
    assert window.first_index == 0
    assert window.last_index == 7
    # Unclamped math is the regression: last two of seven.
    leaked = compute_viewport_window(
        item_count=7,
        scroll_y=stale,
        viewport_width=1100,
        viewport_height=700,
    )
    assert leaked.last_index - leaked.first_index == 2
    assert leaked.first_index == 5


def test_filter_shrink_100_to_7_binds_first_screen_not_tail(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    """100-item list scrolled to bottom → category shrinks to 7 → first screen."""
    from core.db_manager import TAG_TYPE_CATEGORY

    lib = tmp_path / "mod"
    game = "ShrinkGame"
    app_id = 601
    _seed(lib, db, game, 100, app_id=app_id)
    db.add_game_category(app_id, "综合")
    tagged_ids = [str(app_id * 1000 + i) for i in range(7)]
    for mid in tagged_ids:
        db.add_mod_tag(mid, TAG_TYPE_CATEGORY, "综合")
    reset_library_cache()

    view = ModLibraryView()
    view.resize(1100, 800)
    view.show()
    qapp.processEvents()
    view.set_target_root(str(lib))
    view.refresh(force=True, reconcile=False)
    qapp.processEvents()
    view._current_game_filter = game
    view.current_game_id = app_id
    view._render_mod_cards(ModFileManager(lib), force_reload=True)
    qapp.processEvents()
    view._sync_viewport_cards()
    qapp.processEvents()

    assert len(view._filtered_row_entries) == 100
    assert "100 Mods" in view.count_label.text()
    assert len(view._cards) < 100

    vp_w = int(view.scroll.viewport().width() or 1100)
    tall = estimate_total_height(100, vp_w)
    view.library_host.setMinimumHeight(tall)
    qapp.processEvents()
    view._set_scroll_value(view.scroll.verticalScrollBar().maximum())
    qapp.processEvents()
    view._sync_viewport_cards()
    qapp.processEvents()
    assert view._capture_scroll() > 0

    view._category_filter = "综合"
    view._last_filter_sig = None
    view._apply_view_filter()
    qapp.processEvents()

    assert len(view._filtered_row_entries) == 7
    assert view.count_label.text().startswith("7 Mods")
    bound_ids = [
        str(getattr(index, "mod_id", "") or "") for index, _ in view._card_entries
    ]
    filtered_ids = [
        str(getattr(index, "mod_id", "") or "")
        for index, _ in view._filtered_row_entries
    ]
    assert bound_ids
    assert bound_ids[0] == filtered_ids[0]
    assert bound_ids != filtered_ids[-2:]
    assert filtered_ids[0] in bound_ids
    assert len(bound_ids) >= min(7, LIBRARY_CARDS_PER_ROW)
    assert view._capture_scroll() == 0

    # Scrolling the restored 100-item list still windows (virtualization intact).
    view._category_filter = FILTER_CATEGORY_ALL
    view._last_filter_sig = None
    view._apply_view_filter()
    qapp.processEvents()
    assert len(view._filtered_row_entries) == 100
    assert "100 Mods" in view.count_label.text()
    view.library_host.setMinimumHeight(estimate_total_height(100, vp_w))
    qapp.processEvents()
    view._set_scroll_value(800)
    view._sync_viewport_cards()
    qapp.processEvents()
    assert len(view._cards) < 100
    assert len(view._cards) > 0
    scrolled_ids = [
        str(getattr(index, "mod_id", "") or "") for index, _ in view._card_entries
    ]
    assert scrolled_ids
    assert view._capture_scroll() > 0 or scrolled_ids[0] != filtered_ids[0]
