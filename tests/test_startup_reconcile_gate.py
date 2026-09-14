"""Startup Identity Reconcile must not walk the full library by default."""

from __future__ import annotations

import inspect
import time
from pathlib import Path

import pytest

from services.library_reconcile import (
    ReconcilePacing,
    pause_reconcile,
    reconcile_library,
    reset_reconcile_async_state,
    resume_reconcile,
    schedule_startup_library_reconcile,
    start_reconcile_library_async,
)
from services.startup_reconcile_policy import (
    reset_startup_reconcile_policy_cache,
    load_startup_reconcile_policy,
)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_reconcile_async_state()
    reset_startup_reconcile_policy_cache()
    monkeypatch.delenv("SMM_STARTUP_RECONCILE_ENABLED", raising=False)
    yield
    reset_reconcile_async_state()
    reset_startup_reconcile_policy_cache()


def test_startup_policy_defaults_disabled() -> None:
    policy = load_startup_reconcile_policy(force_reload=True)
    assert policy.enabled is False


def test_env_enables_startup_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMM_STARTUP_RECONCILE_ENABLED", "1")
    reset_startup_reconcile_policy_cache()
    policy = load_startup_reconcile_policy(force_reload=True)
    assert policy.enabled is True


def test_schedule_startup_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def _fake_start(*_a, **_k):
        calls.append(True)
        return True

    monkeypatch.setattr(
        "services.library_reconcile.start_reconcile_library_async", _fake_start
    )
    assert schedule_startup_library_reconcile(None) == "skipped"
    assert calls == []


def test_schedule_startup_delayed_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def _fake_start(*_a, **_k):
        calls.append(_k.get("pacing"))
        return True

    monkeypatch.setattr(
        "services.library_reconcile.start_reconcile_library_async", _fake_start
    )
    monkeypatch.setenv("SMM_STARTUP_RECONCILE_ENABLED", "1")
    reset_startup_reconcile_policy_cache()

    # Force zero delay via monkeypatched policy loader.
    from services.startup_reconcile_policy import StartupReconcilePolicy

    monkeypatch.setattr(
        "services.startup_reconcile_policy.load_startup_reconcile_policy",
        lambda **_k: StartupReconcilePolicy(
            enabled=True,
            delay_ms=0,
            batch_size=10,
            batch_pause_ms=5,
            max_mods=0,
        ),
    )
    assert schedule_startup_library_reconcile(None) == "started"
    assert len(calls) == 1
    pace = calls[0]
    assert isinstance(pace, ReconcilePacing)
    assert pace.cooperative is True
    assert pace.low_priority is True
    assert pace.batch_size == 10


def test_main_window_uses_startup_gate() -> None:
    from ui.main_window import MainWindow

    src = inspect.getsource(MainWindow._run_startup_library_reconcile)
    assert "schedule_startup_library_reconcile" in src
    assert "start_reconcile_library_async" not in src
    restore = inspect.getsource(MainWindow._restore_settings)
    assert "start_reconcile_library_async" not in restore


def test_reconcile_max_mods_skips_missing_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Capped runs must not false-mark unvisited folders as missing."""
    from core.db_manager import DatabaseManager

    DatabaseManager.reset_instance()
    DatabaseManager.instance(tmp_path / "cap.db")
    library = tmp_path / "mod"
    library.mkdir()
    folders = [library / f"g/m{i}" for i in range(3)]
    for folder in folders:
        folder.mkdir(parents=True)

    monkeypatch.setattr(
        "services.file_ops.ModFileManager.list_managed_mods",
        lambda self: folders,
    )
    # Empty metadata → IGNORE_NO_INFO after scan++ (still counts toward cap).
    monkeypatch.setattr(
        "services.library_reconcile.read_info_metadata_dict",
        lambda _folder: {},
    )
    monkeypatch.setattr(
        "core.db_manager.DatabaseManager.find_mod_by_last_known_path",
        lambda self, _p: None,
    )
    missing_calls: list[str] = []
    monkeypatch.setattr(
        "services.library_reconcile.mark_missing",
        lambda mid: missing_calls.append(str(mid)),
    )
    monkeypatch.setattr(
        "services.status_recovery.run_status_recovery", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "services.status_recovery.run_status_model_cleanup_v2",
        lambda *_a, **_k: None,
    )

    result = reconcile_library(
        library,
        pacing=ReconcilePacing(max_mods=1, cooperative=False),
    )
    assert result.scanned == 1
    assert "STARTUP_RECONCILE_CAP: 1" in result.notes
    assert "SKIP_MISSING_AND_ORPHAN_PHASES" in result.notes
    assert missing_calls == []
    DatabaseManager.reset_instance()



def test_pause_and_resume_gate() -> None:
    pause_reconcile()
    resumed = {"ok": False}

    def _waiter() -> None:
        from services.library_reconcile import _cooperative_wait

        resumed["ok"] = _cooperative_wait(pause_ms=0)

    import threading

    t = threading.Thread(target=_waiter, daemon=True)
    t.start()
    time.sleep(0.05)
    assert t.is_alive()
    resume_reconcile()
    t.join(1.0)
    assert not t.is_alive()
    assert resumed["ok"] is True


def test_explicit_async_reconcile_still_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """User/tool path: start_reconcile_library_async remains a full pass."""
    ran = {"n": 0}

    def _fake_reconcile(root, pacing=None):
        del root, pacing
        ran["n"] += 1
        from services.library_reconcile import ReconcileResult

        return ReconcileResult()

    monkeypatch.setattr(
        "services.library_reconcile.reconcile_library", _fake_reconcile
    )
    assert start_reconcile_library_async(tmp_path) is True
    deadline = time.monotonic() + 2.0
    while ran["n"] < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ran["n"] == 1
