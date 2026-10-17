"""P0-5B graceful shutdown — workers before DatabaseManager.close()."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager, DatabaseShutdownError, get_db
from services.app_instance import (
    acquire_instance_lock,
    release_instance_lock,
    reset_instance_lock_for_tests,
)
from services.app_shutdown import (
    db_closed_on_shutdown,
    reset_shutdown_state_for_tests,
    set_test_workers_busy,
    shutdown_order,
    shutdown_runtime,
)


@pytest.fixture(autouse=True)
def _reset_shutdown() -> None:
    reset_shutdown_state_for_tests()
    reset_instance_lock_for_tests()
    yield
    reset_shutdown_state_for_tests()
    reset_instance_lock_for_tests()
    DatabaseManager.reset_instance()


def test_a_normal_shutdown_closes_db() -> None:
    db = DatabaseManager.instance()
    assert db.is_closed() is False
    assert shutdown_runtime() is True
    assert db.is_closed() is True
    assert db_closed_on_shutdown() is True
    assert "db_close" in shutdown_order()


def test_b_close_only_once() -> None:
    db = DatabaseManager.instance()
    assert shutdown_runtime() is True
    assert shutdown_runtime() is True
    db.close()
    db.close()
    assert db.is_closed() is True


def test_c_workers_stop_before_db_close() -> None:
    assert shutdown_runtime() is True
    order = shutdown_order()
    assert order.index("workers_stopped") < order.index("db_close")
    src = inspect.getsource(shutdown_runtime)
    assert src.find("workers_stopped") < src.find("close_singleton")
    assert src.find("join_reconcile_thread") < src.find("close_singleton")


def test_d_get_db_does_not_reopen_after_close() -> None:
    first = DatabaseManager.instance()
    first_id = id(first)
    assert shutdown_runtime() is True
    again = get_db()
    assert id(again) == first_id
    assert again.is_closed() is True
    with pytest.raises(DatabaseShutdownError):
        DatabaseManager._instance = None
        get_db()


def test_e_busy_worker_does_not_close_db() -> None:
    db = DatabaseManager.instance()
    set_test_workers_busy(True)
    assert shutdown_runtime() is False
    assert db.is_closed() is False
    assert "db_close" not in shutdown_order()
    assert "db_close_skipped" in shutdown_order()


def test_e_worker_shutdown_exception_does_not_close_db() -> None:
    db = DatabaseManager.instance()

    class _Boom:
        def shutdown(self) -> None:
            raise RuntimeError("sync stop failed")

        def shutdown_workers(self) -> None:
            raise RuntimeError("library stop failed")

        def library_load_is_running(self) -> bool:
            return True

    class _Win:
        sync_view = _Boom()
        library_view = _Boom()

    assert shutdown_runtime(_Win()) is False
    assert db.is_closed() is False
    assert "db_close_skipped" in shutdown_order()


def test_f_reset_instance_after_shutdown(tmp_path: Path) -> None:
    assert shutdown_runtime() is True
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "after_shutdown.db")
    assert db.is_closed() is False
    row = db._conn.execute("SELECT 1").fetchone()
    assert row[0] == 1


def test_g_instance_lock_still_releases(tmp_path: Path) -> None:
    lock_path = tmp_path / "app_instance.lock"
    assert acquire_instance_lock(path=lock_path) is True
    assert shutdown_runtime() is True
    release_instance_lock()
    assert acquire_instance_lock(path=lock_path) is True


def test_close_event_waits_for_web_workers_before_super():
    from frontend.web_window import WebLibraryWindow
    src=inspect.getsource(WebLibraryWindow.closeEvent)
    assert src.index('bridge.shutdown') < src.index('super().closeEvent')
    assert 'event.ignore()' in src


def test_database_closes_before_instance_lock_released():
    import main
    from frontend.web_application import _execute
    src=inspect.getsource(_execute)
    assert src.index('bridge.shutdown') < src.index('db.close()')
    assert src.index('delete(window)') < src.index('db.close()')
    launch=inspect.getsource(main.launch_gui)
    assert launch.index('_run_gui(next_frontend=True') < launch.index('release_instance_lock()')
