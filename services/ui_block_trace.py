"""Main-thread UI phase timing — find freezes after show().

Logs ``[UI_BLOCK]`` lines. Observability only; does not change scheduling.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_T0 = time.perf_counter()


def reset_ui_block_trace() -> None:
    global _T0
    _T0 = time.perf_counter()


def since_ms() -> float:
    return (time.perf_counter() - _T0) * 1000.0


def log_ui_block(phase: str, elapsed_ms: float, **extra: Any) -> None:
    parts = [
        f"[UI_BLOCK] phase={phase}",
        f"elapsed_ms={float(elapsed_ms):.1f}",
        f"since_show_ms={since_ms():.1f}",
    ]
    for key, val in extra.items():
        if val is None:
            continue
        parts.append(f"{key}={val}")
    logger.info(" ".join(parts))


@contextmanager
def ui_block_phase(phase: str, **extra: Any) -> Iterator[None]:
    t0 = time.perf_counter()
    try:
        yield
    finally:
        log_ui_block(phase, (time.perf_counter() - t0) * 1000.0, **extra)
