"""First-wheel Library scroll must not yank the selected card to the top.

Root cause locked: viewport rebind hide() of a focused ModCard → Qt
QScrollArea::focusNextPrevChild → ensureWidgetVisible. These tests cover the
user gesture, not a scrollbar restore patch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from services.file_ops import ModFileManager
from services.mod_library_cache import reset_library_cache
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
from ui.library_view import ModLibraryView
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
    manager = DatabaseManager.instance(tmp_path / "scroll_jump.db")
    yield manager
    DatabaseManager.reset_instance()
    reset_library_cache()


def _seed(lib: Path, db: DatabaseManager, n: int = 48) -> None:
    game = "JumpGame"
    app_id = 4242
    db.upsert_game(GameInfo(app_id=app_id, name=game, folder_name=game))
    for i in range(n):
        created = create_steam_test_mod(
            db,
            external_id=str(app_id * 1000 + i),
            title=f"Mod{i:03d}",
            app_id=app_id,
            game_name=game,
        )
        folder = lib / game / f"Mod{i:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        prove_managed_folder(
            db,
            folder,
            handle=str(created.mod_id),
            title=f"Mod{i:03d}",
            app_id=app_id,
            game_name=game,
        )


def _open_library(
    qapp: QApplication, lib: Path, db: DatabaseManager, monkeypatch
) -> ModLibraryView:
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    view = ModLibraryView()
    view.resize(1200, 800)
    view.show()
    qapp.processEvents()
    view.set_target_root(str(lib))
    view.refresh(force=True, reconcile=False)
    qapp.processEvents()
    view._current_game_filter = "JumpGame"
    view.current_game_id = 4242
    view.current_game_name = "JumpGame"
    view._render_mod_cards(ModFileManager(lib), force_reload=True)
    qapp.processEvents()
    view._sync_viewport_cards()
    qapp.processEvents()
    QTest.qWait(60)
    return view


def _card_viewport_y(view: ModLibraryView, card: ModCardWidget) -> int:
    vp = view.scroll.viewport()
    assert vp is not None
    return int(card.mapTo(vp, QPoint(0, 0)).y())


def _mid_viewport_card(view: ModLibraryView) -> ModCardWidget:
    cards = [c for c in view._cards if isinstance(c, ModCardWidget) and not c.isHidden()]
    assert cards
    vp_h = int(view.scroll.viewport().height() or 600)
    return min(cards, key=lambda c: abs(_card_viewport_y(view, c) - vp_h // 2))


def _send_wheel(view: ModLibraryView, delta_y: int) -> None:
    vp = view.scroll.viewport()
    pos = QPoint(max(10, vp.width() // 2), max(10, vp.height() // 2))
    event = QWheelEvent(
        QPointF(pos),
        QPointF(vp.mapToGlobal(pos)),
        QPoint(0, 0),
        QPoint(0, delta_y),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(vp, event)
    QTest.qWait(50)


def _settle_at_scroll(qapp: QApplication, view: ModLibraryView, value: int) -> None:
    view._set_scroll_value(value)
    timer = getattr(view, "_scroll_sync_timer", None)
    if timer is not None:
        timer.stop()
    view._sync_viewport_cards(scroll_y=value)
    qapp.processEvents()
    view._on_library_scroll_covers()
    qapp.processEvents()


def _select_then_scroll(
    qapp: QApplication, view: ModLibraryView, scroll_y: int
) -> ModCardWidget:
    """Open the detail panel first so later scroll is not clamped by a splitter resize."""
    _settle_at_scroll(qapp, view, 0)
    opener = _mid_viewport_card(view)
    view._select_card(opener, show_panel=True)
    qapp.processEvents()
    _settle_at_scroll(qapp, view, scroll_y)
    card = _mid_viewport_card(view)
    view._select_card(card, show_panel=False)
    qapp.processEvents()
    return card


def test_mod_card_is_not_in_qscrollarea_focus_chain(qapp: QApplication) -> None:
    del qapp
    card = ModCardWidget(Path("."))
    assert card.focusPolicy() == Qt.FocusPolicy.NoFocus
    card.deleteLater()


def test_wheel_down_does_not_yank_selected_card(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "mod"
    lib.mkdir()
    _seed(lib, db)
    view = _open_library(qapp, lib, db, monkeypatch)
    card = _select_then_scroll(qapp, view, 440)

    before_y = _card_viewport_y(view, card)
    before = view._capture_scroll()
    assert before == 440
    assert before_y > 0

    _send_wheel(view, -120)
    after = view._capture_scroll()
    after_y = _card_viewport_y(view, card)

    assert 440 < after <= 560, f"wheel down jumped scroll {before} -> {after}"
    assert after_y > 0, f"selected card left the top of the viewport (vp_y={after_y})"
    assert abs(after_y - before_y) <= 160
    view.close()


def test_wheel_up_does_not_reverse_jump(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "mod"
    lib.mkdir()
    _seed(lib, db)
    view = _open_library(qapp, lib, db, monkeypatch)
    card = _select_then_scroll(qapp, view, 440)

    _send_wheel(view, 120)
    after = view._capture_scroll()
    after_y = _card_viewport_y(view, card)

    assert 320 <= after < 440, f"wheel up reverse-jumped to {after}"
    assert after_y > 0
    view.close()


def test_wheel_without_selection_stays_linear(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "mod"
    lib.mkdir()
    _seed(lib, db)
    view = _open_library(qapp, lib, db, monkeypatch)
    view._clear_selection()
    _settle_at_scroll(qapp, view, 440)
    view.scroll.setFocus(Qt.FocusReason.OtherFocusReason)
    qapp.processEvents()

    _send_wheel(view, -120)
    after = view._capture_scroll()
    assert 440 < after <= 560
    view.close()


def test_wheel_selected_but_focus_on_scroll_area(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "mod"
    lib.mkdir()
    _seed(lib, db)
    view = _open_library(qapp, lib, db, monkeypatch)
    card = _select_then_scroll(qapp, view, 440)
    view.scroll.setFocus(Qt.FocusReason.OtherFocusReason)
    qapp.processEvents()
    assert view._selected_mod_id == card._mod_id()
    assert not isinstance(QApplication.focusWidget(), ModCardWidget)

    before_y = _card_viewport_y(view, card)
    _send_wheel(view, -120)
    after = view._capture_scroll()
    after_y = _card_viewport_y(view, card)
    assert 440 < after <= 560
    assert after_y > 0
    assert abs(after_y - before_y) <= 160
    view.close()


def test_click_selection_and_detail_still_work(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "mod"
    lib.mkdir()
    _seed(lib, db)
    view = _open_library(qapp, lib, db, monkeypatch)
    _settle_at_scroll(qapp, view, 0)
    cards = [c for c in view._cards if isinstance(c, ModCardWidget) and not c.isHidden()]
    assert len(cards) >= 2
    first, second = cards[0], cards[1]

    QTest.mouseClick(first, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    mid = first._mod_id()
    assert view._selected_mod_id == mid
    assert view._selected_mod_ids == [mid]
    assert str(view.detail_panel.current_mod_id() or "") == mid
    assert not isinstance(QApplication.focusWidget(), ModCardWidget)

    monkeypatch.setattr(
        QApplication,
        "keyboardModifiers",
        staticmethod(lambda: Qt.KeyboardModifier.ControlModifier),
    )
    QTest.mouseClick(second, Qt.MouseButton.LeftButton)
    qapp.processEvents()
    assert first._mod_id() in view._selected_mod_ids
    assert second._mod_id() in view._selected_mod_ids
    assert len(view._selected_mod_ids) == 2
    view.close()


def test_small_wheel_does_not_teardown_flow(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "mod"
    lib.mkdir()
    _seed(lib, db)
    view = _open_library(qapp, lib, db, monkeypatch)
    _settle_at_scroll(qapp, view, 440)
    timer = getattr(view, "_scroll_sync_timer", None)
    if timer is not None:
        timer.stop()
    calls = {"n": 0}
    orig = view._clear_flow_except_overlay

    def counted() -> None:
        calls["n"] += 1
        orig()

    view._clear_flow_except_overlay = counted  # type: ignore[method-assign]
    _send_wheel(view, -120)
    assert calls["n"] == 0
    view.close()


def test_teardown_releases_modcard_focus_before_hide(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    """Window-changing rebind must not let Qt ensureWidgetVisible chase cards."""
    lib = tmp_path / "mod"
    lib.mkdir()
    _seed(lib, db)
    view = _open_library(qapp, lib, db, monkeypatch)
    _settle_at_scroll(qapp, view, 440)
    card = _mid_viewport_card(view)
    view._select_card(card, show_panel=True)
    card.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    card.setFocus(Qt.FocusReason.OtherFocusReason)
    qapp.processEvents()
    assert QApplication.focusWidget() is card

    view._sync_viewport_cards(scroll_y=880)
    qapp.processEvents()
    assert view._capture_scroll() == 880
    assert not isinstance(QApplication.focusWidget(), ModCardWidget)
    view.close()
