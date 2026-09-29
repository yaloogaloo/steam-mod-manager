"""Success paths must not create an unsolicited top-level window.

Cover / refresh / offline save rebuild Detail. The visible-child detach and
the one-show_mod contract are what keep Explorer, dialogs, and orphan
DependencyItem windows out of those paths.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMainWindow

from tests.diagnostics.transient_window_capture import classify_record
from ui.dependency_item_widget import DependencyDisplayRow, DependencyItem, DependencyListHost
from ui.library_view import ModLibraryView
from ui.mod_detail_panel import ModDetailPanel
from ui.window_lifecycle import install_window_ownership_guard


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    install_window_ownership_guard(app)
    return app


def test_save_webpage_explorer_is_not_a_product_window() -> None:
    rec = classify_record(
        {
            "hwnd_new": True,
            "class": "CabinetWClass",
            "title": ".info - 文件资源管理器",
        }
    )
    assert rec == "EXTERNAL_SHELL"


def test_success_rebuild_adds_no_toplevel_and_keeps_host(qapp: QApplication) -> None:
    main = QMainWindow()
    main.setWindowTitle("SMM-success-host")
    host = DependencyListHost(main)
    main.setCentralWidget(host)
    main.resize(480, 220)
    main.show()
    qapp.processEvents()
    main.activateWindow()
    qapp.processEvents()
    before = {id(w) for w in QApplication.topLevelWidgets() if w.isVisible()}

    host.set_items(
        [DependencyDisplayRow(name="Dep", workspace_id="42", relation_id=1)]
    )
    qapp.processEvents()
    host.set_items(
        [DependencyDisplayRow(name="Dep rebuilt", workspace_id="42", relation_id=1)]
    )
    qapp.processEvents()

    after = [w for w in QApplication.topLevelWidgets() if w.isVisible()]
    extra = [w for w in after if id(w) not in before and not isinstance(w, QMainWindow)]
    assert extra == []
    rows = host.findChildren(DependencyItem)
    assert len(rows) == 1
    assert rows[0].isWindow() is False
    assert rows[0] not in QApplication.topLevelWidgets()
    active = QApplication.activeWindow()
    assert active is None or active is main
    main.close()


class _ActiveTimer:
    def isActive(self) -> bool:
        return True

    def start(self) -> None:
        return None


def test_one_mutation_records_one_detail_sync_and_skips_projection_rebuild() -> None:
    """Cover, refresh, and offline each notify and reload. Detail show_mod is once."""
    shows: list[str] = []

    class Panel:
        _suppress_projection_detail_rebuild = False
        _deploy_busy = False

        def show_mod(self, *_a, **_k) -> None:
            shows.append("show_mod")

    panel = Panel()
    view = ModLibraryView.__new__(ModLibraryView)
    view.detail_panel = panel  # type: ignore[assignment]
    view._pending_projection = {}
    view._projection_coalesce = _ActiveTimer()
    view._projection_skip_detail = set()

    def _one_operation(mod_id: str) -> None:
        with ModDetailPanel._canonical_detail_sync(panel):  # type: ignore[arg-type]
            ModLibraryView._queue_projection_patch(view, mod_id, kind="full")
            ModLibraryView._queue_projection_patch(view, mod_id, kind="full")
            panel.show_mod()

    for mid in ("cover-id", "refresh-id", "offline-id"):
        shows.clear()
        view._projection_skip_detail = set()
        _one_operation(mid)
        assert shows == ["show_mod"], mid
        assert mid in view._projection_skip_detail

    shows.clear()
    view._projection_skip_detail = set()
    ModLibraryView._queue_projection_patch(view, "deploy-id", kind="full")
    assert "deploy-id" not in view._projection_skip_detail
