"""Regression: ModCard context QMenu must stay owned and not leak top-level windows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QApplication, QMenu

from core.db_manager import DatabaseManager
from core.models import ModMetadata
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
    manager = DatabaseManager.instance(tmp_path / "card_menu.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed_mod(root: Path, *, pub_id: str, title: str) -> Path:
    folder = root / "Palworld" / title
    info = folder / ".info"
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps(
            {
                "published_file_id": pub_id,
                "title": title,
                "game_name": "Palworld",
            }
        ),
        encoding="utf-8",
    )
    return folder


def _visible_toplevel_menus() -> list[QMenu]:
    return [
        w
        for w in QApplication.topLevelWidgets()
        if isinstance(w, QMenu) and w.isVisible()
    ]


def test_context_menu_parent_is_card(qapp: QApplication, tmp_path: Path) -> None:
    mod = tmp_path / "Game" / "MenuMod"
    (mod / ".info").mkdir(parents=True)
    card = ModCardWidget(mod, parent=None)
    menu = card._build_context_menu()

    assert isinstance(menu, QMenu)
    assert menu.parent() is card
    assert menu.parentWidget() is card
    flags = int(menu.windowFlags())
    assert flags & int(Qt.WindowType.Popup)
    labels = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert labels == [
        "查看详情",
        "编辑信息",
        "部署",
        "打开目录",
        "打开 Steam 页面",
        "收藏",
        "设置分类",
    ]
    menu.deleteLater()
    qapp.processEvents()


def test_context_menu_exec_uses_global_pos_and_cleans_up(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = tmp_path / "Game" / "ExecMod"
    (mod / ".info").mkdir(parents=True)
    host = ModCardWidget(mod)
    host.show()
    card = ModCardWidget(mod, parent=host)
    card.show()

    seen: dict[str, object] = {}

    def fake_exec(self: ModCardWidget, menu: QMenu, global_pos) -> None:
        seen["menu"] = menu
        seen["pos"] = global_pos
        seen["parent"] = menu.parent()
        # Do not open a native popup in tests.

    monkeypatch.setattr(ModCardWidget, "_exec_context_menu", fake_exec)

    global_pos = QPoint(120, 340)
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse,
        QPoint(10, 10),
        global_pos,
    )
    card.contextMenuEvent(event)
    qapp.processEvents()

    assert seen["parent"] is card
    assert seen["pos"] == global_pos
    assert _visible_toplevel_menus() == []
    assert not any(c.isVisible() for c in card.findChildren(QMenu))


def test_library_refresh_does_not_leave_menu_windows(
    qapp: QApplication,
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = tmp_path / "library"
    for i in range(4):
        pub = str(95000 + i)
        path = _seed_mod(lib, pub_id=pub, title=f"Menu Mod {i}")
        db.upsert_mod(
            ModMetadata(
                published_file_id=pub,
                title=f"Menu Mod {i}",
                managed_path=str(path),
            )
        )

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()
    assert len(view._cards) == 4

    monkeypatch.setattr(
        ModCardWidget, "_exec_context_menu", lambda self, menu, pos: None
    )
    card = view._cards[0]
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse,
        QPoint(5, 5),
        card.mapToGlobal(QPoint(5, 5)),
    )
    card.contextMenuEvent(event)
    qapp.processEvents()

    assert _visible_toplevel_menus() == []

    view.refresh()
    qapp.processEvents()
    view.refresh()
    qapp.processEvents()

    assert _visible_toplevel_menus() == []
    for card in view._cards:
        assert card.parent() is view.library_host
        assert not any(
            isinstance(c, QMenu) and c.isVisible() for c in card.findChildren(QMenu)
        )
    for w in QApplication.topLevelWidgets():
        if isinstance(w, (ModCardWidget, QMenu)) and w.isVisible():
            pytest.fail(f"unexpected visible top-level {type(w).__name__}")
