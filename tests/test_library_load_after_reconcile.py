"""LibraryLoadWorker scheduling vs reconcile idle (architecture contract).

Library is a read projection: refresh must start snapshot load without waiting
for reconcile. Reconcile remains a background consistency worker. These tests
pin that boundary and ensure blocked reconcile stubs cannot leak threads.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from types import SimpleNamespace

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
def _reset_scheduler() -> list[dict]:
    """Release blocked reconcile gates and join the daemon worker every test."""
    gates: list[dict] = []
    reset_reconcile_async_state()
    yield gates
    for gate in gates:
        gate["release"] = True
    try:
        from services.library_reconcile import (
            join_reconcile_thread,
            request_reconcile_shutdown,
        )

        request_reconcile_shutdown()
        join_reconcile_thread(2.0)
    except Exception:  # noqa: BLE001
        pass
    reset_reconcile_async_state()


def _async_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ui.library_view._library_load_sync", lambda: False)


def _track_worker(view: ModLibraryView, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    starts: list[float] = []

    def _start(self: ModLibraryView, root: Path, *, force: bool = True) -> None:
        del root, force
        starts.append(time.perf_counter())
        # Pretend a worker is live so duplicate flush cannot spawn extras.
        self._load_worker = SimpleNamespace(
            isRunning=lambda: True,
            requestInterruption=lambda: None,
            wait=lambda *_a, **_k: True,
        )

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


def _blocked_reconcile(
    monkeypatch: pytest.MonkeyPatch, gate: dict, gates: list[dict]
) -> list[float]:
    """Patch reconcile to wait on *gate*; always bound by a hard deadline."""
    ended: list[float] = []
    gates.append(gate)

    def _slow(_root=None):
        deadline = time.monotonic() + 8.0
        while not gate["release"] and time.monotonic() < deadline:
            time.sleep(0.01)
        ended.append(time.perf_counter())
        return ReconcileResult()

    monkeypatch.setattr(rec, "reconcile_library", _slow)
    return ended


def test_a_library_load_does_not_wait_for_reconcile(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _reset_scheduler: list[dict],
) -> None:
    """Refresh starts LibraryLoadWorker even while reconcile is held/running."""
    _async_refresh(monkeypatch)
    gate = {"release": False}
    _blocked_reconcile(monkeypatch, gate, _reset_scheduler)
    view = ModLibraryView()
    starts = _track_worker(view, monkeypatch)
    view.set_target_root(str(tmp_path / "lib"))
    hold_library_load_until_reconcile_idle()
    assert library_load_must_wait() is True
    view.refresh(force=False)
    qapp.processEvents()
    assert len(starts) == 1

    assert start_reconcile_library_async(tmp_path / "lib") is True
    gate["release"] = True
    _wait_idle()
    assert len(starts) == 1


def test_b_library_not_visible_does_not_start_worker(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _reset_scheduler: list[dict],
) -> None:
    _async_refresh(monkeypatch)
    gate = {"release": False}
    _blocked_reconcile(monkeypatch, gate, _reset_scheduler)
    view = ModLibraryView()
    starts = _track_worker(view, monkeypatch)
    view.set_target_root(str(tmp_path / "lib"))
    hold_library_load_until_reconcile_idle()
    assert start_reconcile_library_async(tmp_path / "lib") is True
    gate["release"] = True
    _wait_idle()
    for _ in range(10):
        qapp.processEvents()
        time.sleep(0.01)
    assert starts == []
    assert view._library_load_pending is False


def test_c_refresh_during_reconcile_starts_once(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _reset_scheduler: list[dict],
) -> None:
    _async_refresh(monkeypatch)
    gate = {"release": False}
    _blocked_reconcile(monkeypatch, gate, _reset_scheduler)
    view = ModLibraryView()
    starts = _track_worker(view, monkeypatch)
    view.set_target_root(str(tmp_path / "lib"))

    assert start_reconcile_library_async(tmp_path / "lib") is True
    view.refresh(force=False)
    view.refresh(force=False)
    qapp.processEvents()
    assert len(starts) == 1

    gate["release"] = True
    _wait_idle()
    for _ in range(8):
        qapp.processEvents()
        time.sleep(0.01)
    assert len(starts) == 1


def test_d_cancel_pending_library_load(
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _async_refresh(monkeypatch)
    view = ModLibraryView()
    starts = _track_worker(view, monkeypatch)
    view.set_target_root(str(tmp_path / "lib"))
    view._library_load_pending = True
    view.cancel_pending_library_load()
    assert view._library_load_pending is False
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
    qapp: QApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _reset_scheduler: list[dict],
) -> None:
    _async_refresh(monkeypatch)
    gate = {"release": False}
    _blocked_reconcile(monkeypatch, gate, _reset_scheduler)
    view = ModLibraryView()
    starts = _track_worker(view, monkeypatch)
    view.set_target_root(str(tmp_path / "lib"))
    view.refresh(force=False)
    qapp.processEvents()
    assert len(starts) == 1

    rec._notify_reconcile_idle()
    rec._notify_reconcile_idle()
    for _ in range(8):
        qapp.processEvents()
        time.sleep(0.01)
    assert len(starts) == 1

    assert start_reconcile_library_async(tmp_path / "lib") is True
    gate["release"] = True
    _wait_idle()
    for _ in range(8):
        qapp.processEvents()
        time.sleep(0.01)
    assert len(starts) == 1


def test_restore_settings_scheduling_contract() -> None:
    """Startup may hold library load; must not nest reconcile inside Library refresh."""
    src = inspect.getsource(MainWindow._restore_settings)
    assert "reconcile_library(" not in src
    hold_at = src.find("hold_library_load_until_reconcile_idle")
    row_at = src.find("setCurrentRow")
    if hold_at >= 0 and row_at >= 0:
        assert hold_at < row_at


def test_nav_away_cancels_pending_library_load() -> None:
    src = inspect.getsource(MainWindow._on_nav_changed)
    assert "cancel_pending_library_load" in src
    lib_at = src.find("PAGE_LIBRARY")
    cancel_at = src.find("cancel_pending_library_load")
    assert 0 <= lib_at < cancel_at


def test_page_constants_match_cases() -> None:
    assert PAGE_LIBRARY == 1
    assert PAGE_DEPLOY == 2
