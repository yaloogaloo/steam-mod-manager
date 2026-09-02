"""UI phase timing helpers (structured logs for profiling)."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class PerfScope:
    """Accumulate phase timings and emit a single END line."""

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self._t0 = time.perf_counter()
        logger.info("[%s] START", scope)

    def phase(self, name: str) -> None:
        logger.info("[%s] phase=%s", self.scope, name)

    def end(self) -> None:
        elapsed_ms = (time.perf_counter() - self._t0) * 1000.0
        logger.info("[%s] END elapsed=%.1fms", self.scope, elapsed_ms)
