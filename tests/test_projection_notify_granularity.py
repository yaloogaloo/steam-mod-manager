"""Projection notify must not full-rebuild the Library on every id."""

from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.mod_platform import PLATFORM_STEAM
from services.mod_library_cache import ModCardData, get_library_cache, reset_library_cache
from services.mod_projection_events import reset_mod_changed_listeners
from ui.library_query import SORT_NAME, SORT_SIZE, ModFilterIndex
from ui.library_view import ModLibraryView


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def view(qapp: QApplication):
    reset_library_cache()
    reset_mod_changed_listeners()
    widget = ModLibraryView()
    yield widget
    widget.deleteLater()
    qapp.processEvents()
    reset_mod_changed_listeners()
    reset_library_cache()


def _payload(mid: str, name: str, *, size: int | None = None, status: str = "unknown") -> ModCardData:
    return ModCardData(
        id=mid,
        title=name,
        platform=PLATFORM_STEAM,
        cover="",
        description="",
        tags="",
        size=size if status == "ok" else None,
        updated_time=1.0,
        managed_path=f"GameX/{name}",
        game_folder="GameX",
        steam_name=name,
        game_name="GameX",
        size_status=status,
    )


def _index(data: ModCardData) -> ModFilterIndex:
    return ModFilterIndex(
        mod_id=data.id,
        display_name=data.title,
        steam_name=data.steam_name,
        notes="",
        game_name=data.game_name,
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=float(data.updated_time or 0.0),
        sort_name=data.title,
        local_size_bytes=data.size if data.size_status == "ok" else None,
        local_size_status=data.size_status,
    )


def _seed_view(view: ModLibraryView, cards: list[ModCardData]) -> None:
    cache = get_library_cache()
    for card in cards:
        cache.put_card_data(card)
    entries = [(_index(card), card) for card in cards]
    view._game_row_entries = list(entries)
    view._filtered_row_entries = list(entries)
    view._last_filter_sig = ("seed",)


def _flush(view: ModLibraryView, qapp: QApplication) -> None:
    for _ in range(30):
        qapp.processEvents()
        pending = getattr(view, "_pending_projection", None) or {}
        timer = getattr(view, "_projection_coalesce", None)
        if not pending and (timer is None or not timer.isActive()):
            break


def test_single_notify_skips_full_filter_when_sort_fields_unchanged(
    qapp: QApplication, view: ModLibraryView
) -> None:
    a = _payload("1", "Alpha", size=10, status="ok")
    b = _payload("2", "Beta", size=20, status="ok")
    _seed_view(view, [a, b])
    calls: list[str] = []
    real = view._apply_view_filter

    def _tracked() -> None:
        calls.append("apply")
        real()

    view._apply_view_filter = _tracked  # type: ignore[method-assign]
    get_library_cache().put_card_data(replace(a, cover="cover.png"))
    view.on_mod_changed("1")
    _flush(view, qapp)
    assert calls == []


def test_name_change_under_name_sort_recomputes_once(
    qapp: QApplication, view: ModLibraryView
) -> None:
    a = _payload("1", "Alpha")
    b = _payload("2", "Beta")
    _seed_view(view, [a, b])
    view._sort_mode = SORT_NAME
    calls: list[str] = []
    real = view._apply_view_filter

    def _tracked() -> None:
        calls.append("apply")
        real()

    view._apply_view_filter = _tracked  # type: ignore[method-assign]
    get_library_cache().put_card_data(replace(a, title="Zulu"))
    view.on_mod_changed("1")
    _flush(view, qapp)
    assert calls == ["apply"]


def test_batch_notifications_one_ui_pass(
    qapp: QApplication, view: ModLibraryView
) -> None:
    cards = [
        _payload("1", "A", size=10, status="ok"),
        _payload("2", "B", size=20, status="ok"),
        _payload("3", "C", size=30, status="ok"),
    ]
    _seed_view(view, cards)
    view._sort_mode = SORT_SIZE
    calls: list[str] = []
    real = view._apply_view_filter

    def _tracked() -> None:
        calls.append("apply")
        real()

    view._apply_view_filter = _tracked  # type: ignore[method-assign]
    get_library_cache().put_card_data(replace(cards[0], size=1_000_000, size_status="ok"))
    get_library_cache().put_card_data(replace(cards[1], size=2_000_000, size_status="ok"))
    get_library_cache().put_card_data(replace(cards[2], size=3_000_000, size_status="ok"))
    view.on_mod_changed("1")
    view.on_mod_changed("2")
    view.on_mod_changed("3")
    _flush(view, qapp)
    assert calls == ["apply"]


def test_size_projection_updates_sort_without_notify_mod_changed(
    qapp: QApplication, view: ModLibraryView, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.size_observation import SizeObservation, SIZE_STATUS_OK

    small = _payload("11", "Small", size=100, status="ok")
    large = _payload("12", "Large", size=200, status="ok")
    _seed_view(view, [small, large])
    view._sort_mode = SORT_SIZE
    called: list[object] = []
    monkeypatch.setattr(
        "services.mod_projection_events.notify_mod_changed",
        lambda *a, **k: called.append(1),
    )
    get_library_cache().patch_local_size("11", size_bytes=1_000_000, status="ok")
    view.on_size_projection(
        SizeObservation(
            internal_id="11",
            status=SIZE_STATUS_OK,
            size_bytes=1_000_000,
            observed_at="",
            root_mtime=None,
            managed_path="",
        )
    )
    _flush(view, qapp)
    names = [index.display_name for index, _p in view._filtered_row_entries]
    assert names[0] == "Large"
    assert names[1] == "Small"
    assert called == []
