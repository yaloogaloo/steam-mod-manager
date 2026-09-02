"""UI watchdog must not allow late worker SUCCESS to override TIMEOUT."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from ui.deploy_thread import DeployWorker
from ui.library_view import ModLibraryView


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_late_success_ignored_after_ui_timeout(qapp: QApplication) -> None:
    """t0 start → t1 watchdog TIMEOUT → t2 worker SUCCESS → UI stays TIMEOUT."""
    view = ModLibraryView()
    view._deploy_mod_id = "9001"
    view._deploy_ui_timed_out = True
    view.detail_panel.apply_deploy_failure("部署超时", status="TIMEOUT")

    view._on_deploy_finished({"success": True, "mod_id": "9001", "target": "/x"})
    QCoreApplication.processEvents()

    assert view.detail_panel.view_deploy.text() != "[Deploy] 状态：已部署"
    assert "超时" in view.detail_panel.view_deploy.text()


def test_success_before_watchdog_not_overwritten(qapp: QApplication) -> None:
    """t0 start → t1 worker SUCCESS → t2 watchdog → UI stays SUCCESS."""
    view = ModLibraryView()
    view._deploy_mod_id = "9002"
    view._deploy_ui_timed_out = False
    view.detail_panel.apply_deploy_result(
        {
            "success": True,
            "mod_id": "9002",
            "target": "C:/game/mods/Test",
            "deploy_type": "folder_copy",
        }
    )
    QCoreApplication.processEvents()
    assert view.detail_panel.view_deploy.text() == "[Deploy] 状态：已部署"

    # Worker already finished — watchdog must no-op.
    class _DoneWorker(DeployWorker):
        def isRunning(self) -> bool:  # noqa: N802
            return False

    view._deploy_worker = _DoneWorker("9002")
    view._on_deploy_watchdog_timeout()
    QCoreApplication.processEvents()

    assert view.detail_panel.view_deploy.text() == "[Deploy] 状态：已部署"
    assert not getattr(view, "_deploy_ui_timed_out", False)
