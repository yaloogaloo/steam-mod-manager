"""Visible child detach must not map a top-level HWND.

Cover / refresh / offline success rebuild Detail → DependencyListHost.set_items.
Detaching a still-visible DependencyItem via setParent(None) was the flash window.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMainWindow

from ui.dependency_item_widget import DependencyDisplayRow, DependencyItem, DependencyListHost
from ui.window_lifecycle import (
    detach_owned_widget,
    install_window_ownership_guard,
    visible_illegal_toplevels,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    install_window_ownership_guard(app)
    return app


def _visible_dependency_toplevels() -> list:
    app = QApplication.instance()
    if app is None:
        return []
    return [
        w
        for w in app.topLevelWidgets()
        if isinstance(w, DependencyItem) and w.isVisible()
    ]


def test_detach_owned_widget_hides_before_unparent(qapp: QApplication) -> None:
    main = QMainWindow()
    main.resize(400, 200)
    main.show()
    qapp.processEvents()
    child = DependencyItem(DependencyDisplayRow(name="Dep A", workspace_id="123"), parent=main)
    child.show()
    qapp.processEvents()
    assert child.isVisible()
    assert not child.isWindow()
    detach_owned_widget(child)
    qapp.processEvents()
    assert not child.isVisible()
    assert child.parent() is None
    assert not child.isWindow() or not child.isVisible()
    main.close()


def test_dependency_set_items_rebuild_maps_no_toplevel(
    qapp: QApplication,
) -> None:
    """Cover/refresh/offline success path: rebuild a visible dependency row."""
    main = QMainWindow()
    main.setWindowTitle("SMM-lifecycle-host")
    host = DependencyListHost(main)
    main.setCentralWidget(host)
    main.resize(480, 200)
    main.show()
    qapp.processEvents()
    main.activateWindow()
    qapp.processEvents()

    row_a = DependencyDisplayRow(name="First Dep", workspace_id="111", relation_id=1)
    row_b = DependencyDisplayRow(name="Second Dep", workspace_id="222", relation_id=2)
    host.set_items([row_a])
    host.show()
    qapp.processEvents()
    first = host.findChild(DependencyItem)
    assert first is not None
    assert first.isVisible()
    assert first.parent() is host
    assert not first.isWindow()

    host.set_items([row_b])
    qapp.processEvents()
    qapp.processEvents()

    assert _visible_dependency_toplevels() == []
    remaining = host.findChildren(DependencyItem)
    assert len(remaining) == 1
    assert remaining[0].isVisible()
    assert remaining[0].parent() is host
    assert remaining[0].isWindow() is False
    assert remaining[0] not in QApplication.topLevelWidgets()
    if first is not remaining[0]:
        assert first.isWindow() is False or first.isVisible() is False
        assert not (first.isVisible() and first in QApplication.topLevelWidgets())
    assert visible_illegal_toplevels() == []
    active = QApplication.activeWindow()
    assert active is None or active is main
    assert main.isActiveWindow() or active is main or active is None
    main.close()
