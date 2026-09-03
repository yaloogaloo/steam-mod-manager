"""P0-5A single-instance lock — QLockFile before production DB / GUI."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QLockFile

from core.paths import DATABASE_FILENAME, project_root
from services.app_instance import (
    LOCK_FILENAME,
    acquire_instance_lock,
    instance_lock_is_database_path,
    instance_lock_path,
    release_instance_lock,
    reset_instance_lock_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_lock() -> None:
    reset_instance_lock_for_tests()
    yield
    reset_instance_lock_for_tests()


def test_a_first_instance_acquires_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / LOCK_FILENAME
    assert acquire_instance_lock(path=lock_path) is True
    assert lock_path.name != DATABASE_FILENAME


def test_b_second_instance_fails(tmp_path: Path) -> None:
    lock_path = tmp_path / LOCK_FILENAME
    assert acquire_instance_lock(path=lock_path) is True
    other = QLockFile(str(lock_path))
    assert other.tryLock(0) is False


def test_c_second_instance_skips_db_gui_reconcile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as main_mod

    monkeypatch.setattr(
        "services.app_instance.acquire_instance_lock", lambda **_kwargs: False
    )
    monkeypatch.setattr(main_mod, "_notify_already_running", lambda: None)

    def _boom_run() -> int:
        raise AssertionError("_run_gui must not start")

    def _boom_db(*_a, **_k):  # noqa: ANN002
        raise AssertionError("get_db must not run")

    def _boom_rec(*_a, **_k):  # noqa: ANN002
        raise AssertionError("reconcile_library must not run")

    monkeypatch.setattr(main_mod, "_run_gui", _boom_run)
    monkeypatch.setattr("core.db_manager.get_db", _boom_db)
    monkeypatch.setattr("services.library_reconcile.reconcile_library", _boom_rec)

    assert main_mod.launch_gui() == 1


def test_c_launch_gui_source_lock_before_get_db() -> None:
    import main as main_mod

    launch_src = inspect.getsource(main_mod.launch_gui)
    run_src = inspect.getsource(main_mod._run_gui)
    assert "acquire_instance_lock" in launch_src
    assert "get_db" not in launch_src
    assert "MainWindow" not in launch_src
    assert "reconcile_library" not in launch_src
    assert launch_src.find("acquire_instance_lock") < launch_src.find("_run_gui")
    assert "get_db" in run_src
    get_db_at = run_src.find("get_db()")
    assert get_db_at >= 0
    assert "acquire_instance_lock" not in run_src


def test_d_release_allows_new_instance(tmp_path: Path) -> None:
    lock_path = tmp_path / LOCK_FILENAME
    assert acquire_instance_lock(path=lock_path) is True
    release_instance_lock()
    assert acquire_instance_lock(path=lock_path) is True


def test_e_stale_lock_recovered_by_qlockfile(tmp_path: Path) -> None:
    """Crash a holder without unlock; QLockFile must allow a new acquire."""
    import subprocess
    import sys

    lock_path = tmp_path / LOCK_FILENAME
    crash = (
        "from PySide6.QtCore import QLockFile\n"
        "import os\n"
        f"lock = QLockFile({str(lock_path)!r})\n"
        "assert lock.tryLock(0)\n"
        "os._exit(1)\n"
    )
    crashed = subprocess.run(
        [sys.executable, "-c", crash],
        check=False,
        capture_output=True,
        text=True,
    )
    assert crashed.returncode == 1, crashed.stderr
    assert acquire_instance_lock(path=lock_path) is True


def test_f_lock_path_is_not_sqlite_db(tmp_path: Path) -> None:
    lock_path = tmp_path / LOCK_FILENAME
    assert LOCK_FILENAME != DATABASE_FILENAME
    assert lock_path.name != "mod_manager.db"
    assert instance_lock_is_database_path(lock_path) is False
    default = instance_lock_path()
    assert default.name == LOCK_FILENAME
    assert default.name != DATABASE_FILENAME
    assert instance_lock_is_database_path(default) is False
    db_collision = tmp_path / DATABASE_FILENAME
    assert instance_lock_is_database_path(db_collision) is True
    assert acquire_instance_lock(path=db_collision) is False


def test_g_tests_do_not_write_production_lock() -> None:
    production = project_root() / "data" / LOCK_FILENAME
    before = production.exists()
    isolated = instance_lock_path()
    assert isolated != production
    assert acquire_instance_lock() is True
    assert production.exists() is before
    # conftest redirects data_dir to tmp_path/_smm_isolate_data
    assert isolated.parent.name == "_smm_isolate_data"
