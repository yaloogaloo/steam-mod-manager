"""LibraryLoadWorker must start only after library-reconcile is idle."""

from __future__ import annotations

import inspect
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

import services.library_reconcile as rec
from services.library_reconcile import (
    ReconcileResult,
    hold_library_load_until_reconcile_idle,
    library_load_must_wait,
    reset_reconcile_async_state,
    start_reconcile_library_async,
)
from ui.library_view import ModLibraryView
from ui.main_window import MainWindow, PAGE_DEPLOY, PAGE_LIBRARY


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _reset_scheduler() -> None:
    reset_reconcile_async_state()
    yield
    reset_reconcile_async_state()


def _async_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ui.library_view._library_load_sync", lambda: False)


def _track_worker(view: ModLibraryView, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    starts: list[float] = []

    def _start(self: ModLibraryView, root: Path, *, force: bool = True) -> None:
        del self, root, force
        starts.append(time.perf_counter())

    monkeypatch.setattr(ModLibraryView, "_start_library_worker", _start)
    return starts


def _wait_n(qapp: QApplication, bag: list, n: int, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if len(bag) >= n:
            return
        time.sleep(0.01)


def _wait_idle(timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not library_load_must_wait():
            return
        time.sleep(0.01)


def _blocked_reconcile(monkeypatch: pytest.MonkeyPatch, gate: dict) -> list[float]:
    ended: list[float] = []

    def _slow(_root=None):
        while not gate["release"]:
            time.sleep(0.01)
        ended.append(time.perf_counter())
        return ReconcileResult()

    monkeypatch.setattr(rec, "reconcile_library", _slow)
    return ended


def test_a_library_load_starts_after_reconcile(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _async_refresh(monkeypatch)
    gate = {"release": False}
    rec_ended = _blocked_reconcile(monkeypatch, gate)
    view = ModLibraryView()
    starts = _track_worker(view, monkeypatch)
    view.set_target_root(str(tmp_path / "lib"))
    hold_library_load_until_reconcile_idle()
    view.refresh(force=False)
    qapp.processEvents()
    assert starts == []
    assert view._library_load_pending is True

    assert start_reconcile_library_async(tmp_path / "lib") is True
    qapp.processEvents()
    assert starts == []

    gate["release"] = True
    _wait_n(qapp, starts, 1)
    _wait_idle()
    assert len(starts) == 1
    assert rec_ended
    assert starts[0] >= rec_ended[0]


def test_b_library_not_visible_does_not_start_worker(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _async_refresh(monkeypatch)
    gate = {"release": False}
    _blocked_reconcile(monkeypatch, gate)
    view = ModLibraryView()
    starts = _track_worker(view, monkeypatch)
    view.set_target_root(str(tmp_path / "lib"))
    hold_library_load_until_reconcile_idle()
    # Deploy/Sync restore: no library refresh
    assert start_reconcile_library_async(tmp_path / "lib") is True
    gate["release"] = True
    _wait_idle()
    for _ in range(10):
        qapp.processEvents()
        time.sleep(0.01)
    assert starts == []
    assert view._library_load_pending is False


def test_c_switch_to_library_during_reconcile_starts_once(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _async_refresh(monkeypatch)
    gate = {"release": False}
    rec_ended = _blocked_reconcile(monkeypatch, gate)
    view = ModLibraryView()
    starts = _track_worker(view, monkeypatch)
    view.set_target_root(str(tmp_path / "lib"))

    assert start_reconcile_library_async(tmp_path / "lib") is True
    view.refresh(force=False)
    view.refresh(force=False)
    qapp.processEvents()
    assert starts == []
    assert view._library_load_pending is True

    gate["release"] = True
    _wait_n(qapp, starts, 1)
    _wait_idle()
    for _ in range(8):
        qapp.processEvents()
        time.sleep(0.01)
    assert len(starts) == 1
    assert starts[0] >= rec_ended[0]


def test_d_switch_away_before_reconcile_completes(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _async_refresh(monkeypatch)
    gate = {"release": False}
    _blocked_reconcile(monkeypatch, gate)
    view = ModLibraryView()
    starts = _track_worker(view, monkeypatch)
    view.set_target_root(str(tmp_path / "lib"))

    assert start_reconcile_library_async(tmp_path / "lib") is True
    view.refresh(force=False)
    assert view._library_load_pending is True
    view.cancel_pending_library_load()
    assert view._library_load_pending is False

    gate["release"] = True
    _wait_idle()
    for _ in range(10):
        qapp.processEvents()
        time.sleep(0.01)
    assert starts == []


def test_e_idle_reconcile_opens_library_immediately(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _async_refresh(monkeypatch)
    view = ModLibraryView()
    starts = _track_worker(view, monkeypatch)
    view.set_target_root(str(tmp_path / "lib"))
    assert library_load_must_wait() is False
    view.refresh(force=False)
    qapp.processEvents()
    assert len(starts) == 1


def test_f_duplicate_idle_does_not_start_two_workers(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _async_refresh(monkeypatch)
    gate = {"release": False}
    _blocked_reconcile(monkeypatch, gate)
    view = ModLibraryView()
    starts = _track_worker(view, monkeypatch)
    view.set_target_root(str(tmp_path / "lib"))
    hold_library_load_until_reconcile_idle()
    view.refresh(force=False)
    rec._notify_reconcile_idle()
    rec._notify_reconcile_idle()
    qapp.processEvents()
    assert starts == []

    assert start_reconcile_library_async(tmp_path / "lib") is True
    rec._notify_reconcile_idle()
    qapp.processEvents()
    assert starts == []

    gate["release"] = True
    _wait_n(qapp, starts, 1)
    rec._notify_reconcile_idle()
    rec._notify_reconcile_idle()
    for _ in range(8):
        qapp.processEvents()
        time.sleep(0.01)
    _wait_idle()
    assert len(starts) == 1


def test_restore_settings_holds_before_page_restore() -> None:
    src = inspect.getsource(MainWindow._restore_settings)
    hold_at = src.find("hold_library_load_until_reconcile_idle")
    row_at = src.find("setCurrentRow")
    assert 0 <= hold_at < row_at


def test_nav_away_cancels_pending_library_load() -> None:
    src = inspect.getsource(MainWindow._on_nav_changed)
    assert "cancel_pending_library_load" in src
    lib_at = src.find("PAGE_LIBRARY")
    cancel_at = src.find("cancel_pending_library_load")
    assert 0 <= lib_at < cancel_at


def test_page_constants_match_cases() -> None:
    assert PAGE_LIBRARY == 1
    assert PAGE_DEPLOY == 2
