"""Regression: Import Single Mod must not spawn orphan Qt floating windows.

ARCHITECTURE RULE
-----------------
Import Mod once produced semi-transparent orphan Qt floats because platform
radios called ``setVisible`` while still parentless (layout not yet attached).
Never paper over with hide/close — fix ownership via ``ui.window_lifecycle``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QPushButton,
    QRadioButton,
    QWidget,
)

from core.db_manager import DatabaseManager
from ui.library_view import ModLibraryView
from ui.mod_import_dialog import ModImportDialog
from ui.window_lifecycle import (
    WindowOwnershipError,
    install_window_ownership_guard,
    is_illegal_toplevel,
    require_dialog_parent,
    visible_illegal_toplevels,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    install_window_ownership_guard(app)
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "window_lifecycle.db")
    manager.update_game_deploy_config(1623730, name="Palworld")
    yield manager
    DatabaseManager.reset_instance()


def _visible_control_toplevels() -> list[QWidget]:
    return [
        w
        for w in QApplication.topLevelWidgets()
        if w.isVisible()
        and isinstance(w, (QRadioButton, QPushButton))
        and w.parent() is None
    ]


def test_require_dialog_parent_rejects_none() -> None:
    with pytest.raises(WindowOwnershipError):
        require_dialog_parent(None, what="ProbeDialog")


def test_mod_import_dialog_requires_parent(qapp: QApplication, tmp_path: Path) -> None:
    with pytest.raises(WindowOwnershipError):
        ModImportDialog(tmp_path / "lib", parent=None)


def test_import_dialog_construct_spawns_no_orphan_radios(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening「导入单个 Mod」must not map parentless QRadioButton windows."""
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    host = QWidget()
    host.resize(800, 600)
    host.show()
    qapp.processEvents()

    before_illegal = {id(w) for w in visible_illegal_toplevels()}
    before_controls = {id(w) for w in _visible_control_toplevels()}

    dlg = ModImportDialog(
        tmp_path / "lib",
        parent=host,
        game_context={"game_id": 1623730, "game_name": "Palworld"},
    )
    qapp.processEvents()

    assert dlg.parent() is host
    assert not is_illegal_toplevel(dlg)
    for radio in dlg._platform_radios.values():
        assert radio.parent() is not None
        assert not radio.isWindow()

    assert _visible_control_toplevels() == []
    assert [
        w for w in visible_illegal_toplevels() if id(w) not in before_illegal
    ] == []
    assert [
        w for w in _visible_control_toplevels() if id(w) not in before_controls
    ] == []

    QTimer.singleShot(0, dlg.reject)
    dlg.exec()
    qapp.processEvents()
    assert _visible_control_toplevels() == []
    host.close()


def test_import_single_mod_exec_spawns_no_orphan_floats(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    view = ModLibraryView()
    view.set_target_root(str(tmp_path / "lib"))
    view.current_game_name = "Palworld"
    view._current_game_filter = "Palworld"
    view.current_game_id = 1623730
    view.show()
    qapp.processEvents()

    real_exec = QDialog.exec

    def _auto_close(self: QDialog) -> int:
        QTimer.singleShot(0, self.reject)
        return int(real_exec(self))

    monkeypatch.setattr(QDialog, "exec", _auto_close)

    before = list(visible_illegal_toplevels())
    view._on_import_single_mod()
    qapp.processEvents()

    after = [
        w
        for w in visible_illegal_toplevels()
        if w not in before and w.isVisible()
    ]
    assert after == []
    assert _visible_control_toplevels() == []
    view.close()


def test_ownership_guard_refuses_parentless_control_show(qapp: QApplication) -> None:
    from ui.window_lifecycle import ownership_state

    state = ownership_state()
    assert state is not None
    before = state.blocked_count

    orphan = QRadioButton("Steam Workshop")
    orphan.setObjectName("orphanRadioProbe")
    orphan.show()
    qapp.processEvents()

    assert state.blocked_count > before
    assert not orphan.isVisible()
    assert orphan.parent() is None
    orphan.deleteLater()


def test_post_import_refresh_runs_after_dialog_closes() -> None:
    """Source contract: refresh must not run under a live modal import dialog."""
    import inspect

    src = inspect.getsource(ModLibraryView._on_import_single_mod)
    assert "exec_dialog" in src
    assert "imported_ok" in src
    # refresh follows exec — not connected as a live slot during modal lifetime
    assert src.index("exec_dialog") < src.index("refresh(force=True, reconcile=False)")
