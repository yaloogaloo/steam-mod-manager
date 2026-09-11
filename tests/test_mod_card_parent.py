"""Regression: ModCardWidget must never become a top-level window."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    patch_library_get_db,
    write_info_sidecar,
)

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
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
    manager = DatabaseManager.instance(tmp_path / "card_parent.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed_mod(
    db: DatabaseManager, root: Path, *, pub_id: str, title: str
) -> Path:
    created = create_steam_test_mod(db, external_id=pub_id, title=title)
    folder = root / "Palworld" / title
    folder.mkdir(parents=True, exist_ok=True)
    write_info_sidecar(
        folder,
        internal_id=str(created.mod_id),
        title=title,
        external_id=pub_id,
        workspace_id=str(created.workspace_id or pub_id),
        game_name="Palworld",
    )
    bind_managed_path(db, created.mod_id, folder, title=title)
    return folder


def _assert_card_parented(card: ModCardWidget, view: ModLibraryView) -> None:
    parent = card.parent()
    assert parent is not None
    expected = view._cards_host if view._cards_host is not None else view.library_host
    assert parent is expected
    assert not card.isWindow()


def test_render_cards_always_have_parent(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_library_get_db(monkeypatch, db)
    lib = tmp_path / "library"
    for i in range(5):
        pub = str(93000 + i)
        _seed_mod(db, lib, pub_id=pub, title=f"Mod {i}")

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()

    assert len(view._cards) == 5
    for card in view._cards:
        assert isinstance(card, ModCardWidget)
        _assert_card_parented(card, view)


def _visible_toplevel_mod_cards() -> list[ModCardWidget]:
    return [
        w
        for w in QApplication.topLevelWidgets()
        if isinstance(w, ModCardWidget) and w.isVisible()
    ]


def test_refresh_does_not_spawn_toplevel_mod_cards(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_library_get_db(monkeypatch, db)
    lib = tmp_path / "library"
    for i in range(8):
        pub = str(94000 + i)
        _seed_mod(db, lib, pub_id=pub, title=f"Refresh Mod {i}")

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()

    before_toplevel = set(QApplication.topLevelWidgets())

    def _assert_cards_safe() -> None:
        assert _visible_toplevel_mod_cards() == []
        for w in QApplication.topLevelWidgets():
            if isinstance(w, ModCardWidget):
                # Cleared cards may briefly be top-level until deleteLater runs,
                # but must never be visible (the blank-window bug).
                assert not w.isVisible()
        for card in view._cards:
            _assert_card_parented(card, view)

    _assert_cards_safe()

    view.refresh()
    qapp.processEvents()
    _assert_cards_safe()
    # No new top-level widgets introduced by refresh (except transient deleteLater
    # orphans that must stay invisible — already asserted above).
    after = set(QApplication.topLevelWidgets())
    new_visible = [
        w
        for w in (after - before_toplevel)
        if w.isVisible() and isinstance(w, ModCardWidget)
    ]
    assert new_visible == []

    view.refresh()
    qapp.processEvents()
    _assert_cards_safe()
    assert len(view._cards) == 8
    assert _visible_toplevel_mod_cards() == []


def test_reveal_card_skips_show_when_parentless(
    qapp: QApplication,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard: if parent is still None after addWidget, do not show()."""
    import logging

    mod = tmp_path / "Game" / "Orphan"
    mod.mkdir(parents=True)
    (mod / ".info").mkdir()

    view = ModLibraryView()
    orphan = ModCardWidget(mod, parent=None)
    assert orphan.parent() is None

    # Simulate layout failing to reparent
    monkeypatch.setattr(view.library_layout, "addWidget", lambda *_a, **_k: None)

    with caplog.at_level(logging.WARNING, logger="ui.library_view"):
        view._reveal_card(orphan)

    assert orphan.parent() is None
    assert not orphan.isVisible()
    assert not (orphan.isWindow() and orphan.isVisible())
    assert any(
        "[UI BUG] ModCardWidget has no parent before show" in r.message
        for r in caplog.records
    )