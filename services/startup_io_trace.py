"""Startup I/O overlap tracing. Observability only — no scheduling changes."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_T0 = time.perf_counter()
_lock = threading.Lock()
_active: dict[str, float] = {}
_intervals: list[tuple[str, float, float]] = []
_max_concurrent = 0
_reconcile_queued = 0
_cover_wave_open = False
_cover_failed = 0


def reset_cover_wave() -> None:
    """Drop an in-flight cover wave without emitting end (test reset)."""
    global _cover_wave_open, _cover_failed
    with _lock:
        _cover_wave_open = False
        _cover_failed = 0
        _active.pop("cover_loader", None)


def reset_startup_io_trace() -> None:
    global _T0, _max_concurrent, _reconcile_queued, _cover_wave_open, _cover_failed
    with _lock:
        _T0 = time.perf_counter()
        _active.clear()
        _intervals.clear()
        _max_concurrent = 0
        _reconcile_queued = 0
        _cover_wave_open = False
        _cover_failed = 0


def _now() -> float:
    return time.perf_counter()


def _since_ms(ts: float | None = None) -> float:
    return ((ts if ts is not None else _now()) - _T0) * 1000.0


def log_io_event(
    component: str,
    event: str,
    *,
    elapsed_ms: float | None = None,
    **extra: Any,
) -> None:
    parts = [
        f"[STARTUP_IO_TRACE] component={component} event={event}",
        f"timestamp={_since_ms():.1f}",
        f"thread={threading.current_thread().name}",
    ]
    if elapsed_ms is not None:
        parts.append(f"elapsed_ms={elapsed_ms:.1f}")
    for key, val in extra.items():
        if val is None:
            continue
        parts.append(f"{key}={val}")
    logger.info(" ".join(parts))


def begin(component: str, **extra: Any) -> None:
    global _max_concurrent
    t = _now()
    with _lock:
        _active[component] = t
        _max_concurrent = max(_max_concurrent, len(_active))
        concurrent = len(_active)
        max_c = _max_concurrent
    log_io_event(
        component,
        "start",
        concurrent=concurrent,
        max_concurrent=max_c,
        **extra,
    )


def end(component: str, **extra: Any) -> None:
    t = _now()
    elapsed = None
    with _lock:
        t0 = _active.pop(component, None)
        if t0 is not None:
            elapsed = (t - t0) * 1000.0
            _intervals.append((component, t0, t))
        concurrent = len(_active)
    log_io_event(
        component,
        "end",
        elapsed_ms=elapsed if elapsed is not None else 0.0,
        concurrent=concurrent,
        **extra,
    )


def note_reconcile_queued() -> None:
    global _reconcile_queued
    with _lock:
        _reconcile_queued += 1
        n = _reconcile_queued
    log_io_event("reconcile", "queued", queued_count=n)


def cover_wave_on_request() -> None:
    global _cover_wave_open
    with _lock:
        already = _cover_wave_open
        if not already:
            _cover_wave_open = True
    if not already:
        begin("cover_loader")


def cover_wave_on_task_done(*, failed: bool) -> None:
    global _cover_wave_open, _cover_failed
    from services import cover_loader as cl

    with _lock:
        if failed:
            _cover_failed += 1
        failed_n = _cover_failed
        submitted = int(cl.COVER_LOAD_REQUESTS)
        completed = int(cl.COVER_LOAD_COMPLETED)
        should_end = _cover_wave_open and completed >= submitted and submitted > 0
        if should_end:
            _cover_wave_open = False
    if should_end:
        end(
            "cover_loader",
            tasks_submitted=submitted,
            tasks_completed=completed,
            tasks_failed=failed_n,
            task_cpu_ms=round(float(cl.COVER_LOAD_MS_TOTAL), 1),
        )


def is_active(component: str) -> bool:
    with _lock:
        return component in _active


def has_ended(component: str) -> bool:
    with _lock:
        return any(c == component for c, _t0, _t1 in _intervals)


def active_names() -> list[str]:
    with _lock:
        return sorted(_active.keys())


def _overlap_s(a: tuple[float, float], b: tuple[float, float]) -> float:
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    return max(0.0, hi - lo)


def _merge_component(
    name: str, intervals: list[tuple[str, float, float]] | None = None
) -> tuple[float, float] | None:
    src = intervals if intervals is not None else _intervals
    spans = [(t0, t1) for c, t0, t1 in src if c == name]
    if not spans:
        return None
    return (min(s[0] for s in spans), max(s[1] for s in spans))


def _span_ms(span: tuple[float, float] | None) -> float:
    if span is None:
        return 0.0
    return (span[1] - span[0]) * 1000.0


def summarize() -> dict[str, Any]:
    with _lock:
        intervals = list(_intervals)
        max_c = _max_concurrent
        queued = _reconcile_queued

    rec = _merge_component("reconcile", intervals)
    lib = _merge_component("library_load", intervals)
    cov = _merge_component("cover_loader", intervals)

    rec_x_lib = _overlap_s(rec, lib) * 1000.0 if rec and lib else 0.0
    rec_x_cov = _overlap_s(rec, cov) * 1000.0 if rec and cov else 0.0
    lib_x_cov = _overlap_s(lib, cov) * 1000.0 if lib and cov else 0.0
    all_three = 0.0
    if rec and lib and cov:
        lo = max(rec[0], lib[0], cov[0])
        hi = min(rec[1], lib[1], cov[1])
        all_three = max(0.0, hi - lo) * 1000.0

    payload = {
        "reconcile_elapsed_ms": round(_span_ms(rec), 1),
        "library_load_elapsed_ms": round(_span_ms(lib), 1),
        "cover_loader_elapsed_ms": round(_span_ms(cov), 1),
        "reconcile_x_library_load_ms": round(rec_x_lib, 1),
        "reconcile_x_cover_loader_ms": round(rec_x_cov, 1),
        "library_load_x_cover_loader_ms": round(lib_x_cov, 1),
        "all_three_overlap_ms": round(all_three, 1),
        "max_concurrent_startup_io_tasks": max_c,
        "reconcile_queued_count": queued,
        "reconcile_overlap_detected": queued > 0,
        "intervals": [
            {
                "component": c,
                "t0_ms": round(_since_ms(t0), 1),
                "t1_ms": round(_since_ms(t1), 1),
                "elapsed_ms": round((t1 - t0) * 1000.0, 1),
            }
            for c, t0, t1 in intervals
        ],
    }
    logger.info(
        "[STARTUP_IO_SUMMARY] reconcile_ms=%s library_load_ms=%s cover_loader_ms=%s "
        "rec_x_lib_ms=%s rec_x_cover_ms=%s lib_x_cover_ms=%s all_three_ms=%s "
        "max_concurrent=%s reconcile_queued=%s",
        payload["reconcile_elapsed_ms"],
        payload["library_load_elapsed_ms"],
        payload["cover_loader_elapsed_ms"],
        payload["reconcile_x_library_load_ms"],
        payload["reconcile_x_cover_loader_ms"],
        payload["library_load_x_cover_loader_ms"],
        payload["all_three_overlap_ms"],
        payload["max_concurrent_startup_io_tasks"],
        payload["reconcile_queued_count"],
    )
    return payload
