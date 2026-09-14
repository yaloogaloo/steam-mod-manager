"""Structured ``[PERF_STAGE]`` timing for startup / Library / Detail click.

Observability + acceptance measurements. Safe to call from UI or workers.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_T0 = time.perf_counter()
_last: dict[str, float] = {}


def reset_perf_stage(t0: float | None = None) -> None:
    global _T0
    _T0 = float(t0) if t0 is not None else time.perf_counter()
    _last.clear()


def since_ms() -> float:
    return (time.perf_counter() - _T0) * 1000.0


def log_perf_stage(stage: str, elapsed_ms: float, **extra: Any) -> None:
    parts = [
        f"[PERF_STAGE] stage={stage}",
        f"elapsed_ms={float(elapsed_ms):.1f}",
        f"since_ms={since_ms():.1f}",
    ]
    for key, val in extra.items():
        if val is None:
            continue
        parts.append(f"{key}={val}")
    line = " ".join(parts)
    logger.info(line)
    _last[str(stage)] = float(elapsed_ms)


def last_stage_ms(stage: str) -> float:
    return float(_last.get(str(stage), 0.0))


@contextmanager
def perf_stage(stage: str, **extra: Any) -> Iterator[dict[str, Any]]:
    """Time a block; extras may be mutated inside the ``with`` body."""
    bag: dict[str, Any] = dict(extra)
    t0 = time.perf_counter()
    try:
        yield bag
    finally:
        merged = {**extra, **bag}
        log_perf_stage(stage, (time.perf_counter() - t0) * 1000.0, **merged)
