"""Library「导入 Mod」menu exposes single + batch directory entry points."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from ui.library_view import ModLibraryView


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "import_menu.db")
    yield manager
    DatabaseManager.reset_instance()


def test_import_button_has_single_and_batch_actions(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    db.update_game_deploy_config(1623730, name="Palworld")

    view = ModLibraryView()
    view.set_target_root(str(tmp_path / "mod"))
    menu = view.import_btn.menu()
    assert menu is not None
    texts = [a.text() for a in menu.actions()]
    assert any("单个" in t for t in texts)
    assert any("批量" in t for t in texts)


def test_batch_directory_starts_worker_with_is_batch_mode(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    db.update_game_deploy_config(1623730, name="Palworld")

    parent = tmp_path / "batch"
    for name in ("ModA", "ModB"):
        d = parent / name
        d.mkdir(parents=True)
        (d / "main.pak").write_bytes(b"x")

    view = ModLibraryView()
    view.set_target_root(str(tmp_path / "lib"))
    view.current_game_name = "Palworld"
    view._current_game_filter = "Palworld"
    view.current_game_id = 1623730

    monkeypatch.setattr(
        "ui.library_view.QFileDialog.getExistingDirectory",
        lambda *a, **k: str(parent),
    )

    captured: dict = {}

    class _FakeWorker:
        progress_changed = None
        import_finished = None
        import_failed = None

        def __init__(self, *, platform, library_root, params, parent=None):
            from PySide6.QtCore import QObject, Signal

            class _Sig(QObject):
                progress_changed = Signal(str)
                import_finished = Signal(object)
                import_failed = Signal(str)

            self._signals = _Sig()
            self.progress_changed = self._signals.progress_changed
            self.import_finished = self._signals.import_finished
            self.import_failed = self._signals.import_failed
            captured["platform"] = platform
            captured["params"] = dict(params)
            self._running = False

        def isRunning(self) -> bool:
            return self._running

        def start(self) -> None:
            self._running = True

        def requestInterruption(self) -> None:
            pass

    monkeypatch.setattr("ui.import_thread.ImportWorker", _FakeWorker)

    view._on_import_batch_directory()

    assert captured["params"]["is_batch_mode"] is True
    assert captured["params"]["folder"] == str(parent)
    assert captured["params"]["nexus_url"] == ""
    assert captured["params"]["game_id"] == 1623730
    assert str(captured["platform"]).lower() == "nexus"
    assert view._batch_import_worker is not None
