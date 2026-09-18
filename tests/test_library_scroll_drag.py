"""Library scroll must incrementally update the viewport, not rebuild it.

Cross-window wheel used to takeAt every card and re-add the window, which
stormed FlowLayout.invalidate / heightChanged on the UI thread. Small wheel
must skip bind and geometry entirely. Cover scheduling must not rescan when
the bound card ids are unchanged.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from tests.test_library_scroll_jump import _open_library, _seed, _settle_at_scroll
from ui.library_view import ModLibraryView
from ui.library_viewport import compute_viewport_window
from ui.mod_card import ModCardWidget


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "scroll_drag.db")
    yield manager
    DatabaseManager.reset_instance()


def _stop_scroll_timers(view: ModLibraryView) -> None:
    timer = getattr(view, "_scroll_sync_timer", None)
    if timer is not None:
        timer.stop()
    cover = getattr(view, "_cover_sched", None)
    if cover is not None:
        cover.stop()


def _window_at(view: ModLibraryView, scroll_y: int):
    viewport_w, viewport_h = view._viewport_metrics()
    return compute_viewport_window(
        item_count=view._viewport_item_count(),
        scroll_y=scroll_y,
        viewport_width=viewport_w,
        viewport_height=viewport_h,
    )


def _cross_window_scroll(view: ModLibraryView) -> tuple[int, object]:
    """Scroll offset that moves first_index off 0 (diagnosed 0→8 at 4 columns)."""
    start = _window_at(view, 0)
    for candidate in (880, 660, 1100, 1320, 1540, 1760):
        window = _window_at(view, candidate)
        if int(window.first_index) != int(start.first_index):
            return candidate, window
    pytest.fail(
        f"could not leave window first_index={start.first_index} cols={start.columns}"
    )


def _install_layout_counters(view: ModLibraryView) -> dict:
    counts = {"inv": 0, "hc": 0, "clear": 0, "take": 0, "geo": 0, "bind": 0}
    orig_inv = view.library_layout.invalidate
    orig_take = view.library_layout.takeAt
    orig_clear = view._clear_flow_except_overlay
    orig_geo = view._recompute_viewport_geometry
    orig_bind = view._bind_viewport_cards

    def counted_inv(*args, **kwargs):
        counts["inv"] += 1
        return orig_inv(*args, **kwargs)

    def counted_take(index):
        counts["take"] += 1
        return orig_take(index)

    def counted_clear() -> None:
        counts["clear"] += 1
        orig_clear()

    def counted_geo(item_count: int):
        counts["geo"] += 1
        return orig_geo(item_count)

    def counted_bind(*, scroll_y: int) -> None:
        counts["bind"] += 1
        orig_bind(scroll_y=scroll_y)

    view.library_layout.invalidate = counted_inv  # type: ignore[method-assign]
    view.library_layout.takeAt = counted_take  # type: ignore[method-assign]
    view._clear_flow_except_overlay = counted_clear  # type: ignore[method-assign]
    view._recompute_viewport_geometry = counted_geo  # type: ignore[method-assign]
    view._bind_viewport_cards = counted_bind  # type: ignore[method-assign]
    view.library_layout.heightChanged.connect(
        lambda *_args: counts.__setitem__("hc", counts["hc"] + 1)
    )
    counts["_orig"] = {
        "inv": orig_inv,
        "take": orig_take,
        "clear": orig_clear,
        "geo": orig_geo,
        "bind": orig_bind,
    }
    return counts


def _reset_counts(counts: dict) -> None:
    for key in ("inv", "hc", "clear", "take", "geo", "bind"):
        counts[key] = 0


def test_cross_window_scroll_is_incremental_not_full_rebuild(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "mod"
    lib.mkdir()
    _seed(lib, db)
    view = _open_library(qapp, lib, db, monkeypatch)
    _settle_at_scroll(qapp, view, 0)
    _stop_scroll_timers(view)

    target, after_window = _cross_window_scroll(view)
    start_window = _window_at(view, 0)
    assert int(start_window.first_index) == 0
    live_before = max(1, view.library_layout.count())
    before_ids = {
        card._mod_id(): id(card)
        for card in view._cards
        if isinstance(card, ModCardWidget) and card._mod_id()
    }
    counts = _install_layout_counters(view)

    orig_incremental = view._try_incremental_mod_viewport
    view._try_incremental_mod_viewport = lambda **_k: False  # type: ignore[method-assign]
    t0 = time.perf_counter()
    view._sync_viewport_cards(scroll_y=target)
    qapp.processEvents()
    full_ms = (time.perf_counter() - t0) * 1000.0
    full = {key: counts[key] for key in ("inv", "hc", "clear", "take")}
    assert full["clear"] >= 1, "full rebuild must still clear when incremental is off"

    _settle_at_scroll(qapp, view, 0)
    _stop_scroll_timers(view)
    view._try_incremental_mod_viewport = orig_incremental
    _reset_counts(counts)
    before_ids = {
        card._mod_id(): id(card)
        for card in view._cards
        if isinstance(card, ModCardWidget) and card._mod_id()
    }
    live_before = max(1, view.library_layout.count())

    t0 = time.perf_counter()
    view._sync_viewport_cards(scroll_y=target)
    qapp.processEvents()
    inc_ms = (time.perf_counter() - t0) * 1000.0
    after_ids = {
        card._mod_id(): id(card)
        for card in view._cards
        if isinstance(card, ModCardWidget) and card._mod_id()
    }
    overlap = set(before_ids) & set(after_ids)

    assert int(after_window.first_index) != 0
    assert counts["clear"] == 0, "incremental bind must not takeAt the whole window"
    assert overlap, "cross-window scroll must keep overlapping cards"
    for mid in overlap:
        assert before_ids[mid] == after_ids[mid]
    assert 0 < counts["take"] < live_before
    assert counts["inv"] < full["inv"]
    assert counts["hc"] <= 8
    assert inc_ms <= full_ms or counts["take"] < full["take"]
    view.close()


def test_small_scroll_skips_bind_and_geometry(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "mod"
    lib.mkdir()
    _seed(lib, db)
    view = _open_library(qapp, lib, db, monkeypatch)
    _settle_at_scroll(qapp, view, 440)
    _stop_scroll_timers(view)
    assert _window_at(view, 440).first_index == _window_at(view, 500).first_index

    counts = _install_layout_counters(view)
    _reset_counts(counts)
    view._sync_viewport_cards(scroll_y=500)

    assert counts["bind"] == 0
    assert counts["geo"] == 0
    assert counts["inv"] == 0
    assert counts["hc"] == 0
    assert counts["clear"] == 0
    view.close()


def test_unchanged_visible_ids_skip_cover_scan(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "mod"
    lib.mkdir()
    _seed(lib, db)
    view = _open_library(qapp, lib, db, monkeypatch)
    _settle_at_scroll(qapp, view, 440)
    _stop_scroll_timers(view)

    scans = {"iter": 0, "ensure": 0, "cancel": 0}
    orig_iter = view.iter_viewport_cover_cards
    orig_ensure = ModCardWidget.ensure_cover
    orig_cancel = ModCardWidget.cancel_pending_cover

    def counted_iter(*args, **kwargs):
        scans["iter"] += 1
        return orig_iter(*args, **kwargs)

    def counted_ensure(self, *args, **kwargs):
        scans["ensure"] += 1
        return orig_ensure(self, *args, **kwargs)

    def counted_cancel(self, *args, **kwargs):
        scans["cancel"] += 1
        return orig_cancel(self, *args, **kwargs)

    view.iter_viewport_cover_cards = counted_iter  # type: ignore[method-assign]
    monkeypatch.setattr(ModCardWidget, "ensure_cover", counted_ensure)
    monkeypatch.setattr(ModCardWidget, "cancel_pending_cover", counted_cancel)

    view._last_cover_visible_ids = None
    view._load_viewport_covers()
    first = dict(scans)
    assert first["iter"] == 1
    assert first["ensure"] + first["cancel"] > 0

    view._load_viewport_covers()
    assert scans["iter"] == first["iter"]
    assert scans["ensure"] == first["ensure"]
    assert scans["cancel"] == first["cancel"]
    view.close()
