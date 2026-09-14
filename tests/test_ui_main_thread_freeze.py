"""Main-thread UI freeze guards: cover fan-out + card cache budget."""

from __future__ import annotations

import inspect
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from services.cover_loader import CoverLoaderManager, reset_cover_loader_stats
from ui.library_view import LIBRARY_CARD_CACHE_BUDGET, ModLibraryView
from ui.mod_card import COVER_HEIGHT, COVER_WIDTH, ModCardWidget


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _reset_covers() -> None:
    CoverLoaderManager.reset_instance()
    reset_cover_loader_stats()
    yield
    CoverLoaderManager.reset_instance()
    reset_cover_loader_stats()


def test_mod_card_does_not_broadcast_connect_image_ready() -> None:
    src = inspect.getsource(ModCardWidget.__init__)
    assert "image_ready.connect" not in src
    assert "on_ready" in inspect.getsource(ModCardWidget._request_cover)


def test_token_ready_delivers_only_to_owner(qapp: QApplication, tmp_path: Path) -> None:
    folder = tmp_path / "Game" / "Mod"
    folder.mkdir(parents=True)
    cover = folder / "cover.png"
    img = QImage(COVER_WIDTH, COVER_HEIGHT, QImage.Format.Format_RGB32)
    img.fill(2)
    img.save(str(cover))

    hits: list[str] = []

    class _Probe(ModCardWidget):
        def _on_cover_image_ready(self, token: str, image: object) -> None:
            hits.append(str(token))
            super()._on_cover_image_ready(token, image)

    card_a = _Probe(folder)
    card_b = _Probe(folder)
    # Simulate stale broadcast: if cards still listened, both would fire.
    mgr = CoverLoaderManager.instance()
    mgr.request(
        "tok-a",
        folder,
        cover_ref=str(cover),
        width=COVER_WIDTH,
        height=COVER_HEIGHT,
        on_ready=card_a._on_cover_image_ready,
    )
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not hits:
        qapp.processEvents()
        time.sleep(0.02)
    assert hits == ["tok-a"]
    # card_b must not have been invoked
    assert card_b._cover_applied_token != "tok-a"
    card_a.deleteLater()
    card_b.deleteLater()


def test_card_cache_budget_trims_off_viewport(qapp: QApplication) -> None:
    view = ModLibraryView()
    # Fake a bloated cache of plain QWidget stand-ins via ModCardWidget stubs.
    for i in range(LIBRARY_CARD_CACHE_BUDGET + 40):
        key = f"mid-{i}"
        card = ModCardWidget(Path(f"D:/fake/{i}"))
        view._card_cache[key] = card
    live = {f"mid-{i}" for i in range(12)}
    view._trim_card_cache_budget(live_keys=live)
    assert len(view._card_cache) <= max(LIBRARY_CARD_CACHE_BUDGET, 24)
    for key in live:
        assert key in view._card_cache
    view.close()


def test_bind_hides_only_previous_live_not_full_cache() -> None:
    src = inspect.getsource(ModLibraryView._bind_viewport_cards)
    assert "_viewport_live_cards" in src
    assert "list(self._card_cache.values())" not in src
    assert "_trim_card_cache_budget" in src


def test_scroll_is_debounced() -> None:
    src = inspect.getsource(ModLibraryView._on_library_scroll_value)
    assert "_scroll_sync_timer" in src
