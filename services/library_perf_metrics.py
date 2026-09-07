"""Library performance metrics — measurable acceptance, not guessed timings.

ARCHITECTURE RULE
-----------------
These counters exist to prove DB-index + viewport lifecycle under scale.
They must not introduce caches, background workarounds, or display caps.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LibraryPerfSnapshot:
    startup_total_ms: float = 0.0
    library_index_load_ms: float = 0.0
    database_query_ms: float = 0.0
    viewmodel_create_ms: float = 0.0
    game_switch_total_ms: float = 0.0
    cards_created: int = 0
    cards_reused: int = 0
    visible_cards: int = 0
    backup_queue_length: int = 0
    backup_worker_latency_ms: float = 0.0
    last_event: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


class LibraryPerfMetrics:
    """Process-wide Library / Backup timing recorder (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap = LibraryPerfSnapshot()
        self._switch_t0: float | None = None
        self._startup_t0: float | None = None

    def reset(self) -> None:
        with self._lock:
            self._snap = LibraryPerfSnapshot()
            self._switch_t0 = None
            self._startup_t0 = None

    def snapshot(self) -> LibraryPerfSnapshot:
        with self._lock:
            s = self._snap
            return LibraryPerfSnapshot(
                startup_total_ms=s.startup_total_ms,
                library_index_load_ms=s.library_index_load_ms,
                database_query_ms=s.database_query_ms,
                viewmodel_create_ms=s.viewmodel_create_ms,
                game_switch_total_ms=s.game_switch_total_ms,
                cards_created=s.cards_created,
                cards_reused=s.cards_reused,
                visible_cards=s.visible_cards,
                backup_queue_length=s.backup_queue_length,
                backup_worker_latency_ms=s.backup_worker_latency_ms,
                last_event=s.last_event,
                extras=dict(s.extras),
            )

    def mark_startup_begin(self) -> None:
        with self._lock:
            self._startup_t0 = time.perf_counter()
            self._snap.last_event = "startup_begin"

    def mark_startup_end(self) -> None:
        with self._lock:
            if self._startup_t0 is not None:
                self._snap.startup_total_ms = (
                    time.perf_counter() - self._startup_t0
                ) * 1000.0
            self._snap.last_event = "startup_end"

    def record_index_load(
        self,
        *,
        database_query_ms: float,
        viewmodel_create_ms: float,
        total_ms: float | None = None,
    ) -> None:
        with self._lock:
            self._snap.database_query_ms = float(database_query_ms)
            self._snap.viewmodel_create_ms = float(viewmodel_create_ms)
            if total_ms is None:
                total_ms = float(database_query_ms) + float(viewmodel_create_ms)
            self._snap.library_index_load_ms = float(total_ms)
            self._snap.last_event = "index_load"

    def mark_game_switch_begin(self) -> None:
        with self._lock:
            self._switch_t0 = time.perf_counter()
            self._snap.last_event = "game_switch_begin"

    def mark_game_switch_end(self) -> None:
        with self._lock:
            if self._switch_t0 is not None:
                self._snap.game_switch_total_ms = (
                    time.perf_counter() - self._switch_t0
                ) * 1000.0
            self._snap.last_event = "game_switch_end"

    def record_viewport(
        self,
        *,
        cards_created: int,
        cards_reused: int,
        visible_cards: int,
    ) -> None:
        with self._lock:
            self._snap.cards_created = int(cards_created)
            self._snap.cards_reused = int(cards_reused)
            self._snap.visible_cards = int(visible_cards)
            self._snap.last_event = "viewport"

    def record_backup_queue(self, length: int) -> None:
        with self._lock:
            self._snap.backup_queue_length = max(0, int(length))
            self._snap.last_event = "backup_queue"

    def record_backup_worker_latency(self, latency_ms: float) -> None:
        with self._lock:
            self._snap.backup_worker_latency_ms = float(latency_ms)
            self._snap.last_event = "backup_worker"


_METRICS = LibraryPerfMetrics()


def get_library_perf_metrics() -> LibraryPerfMetrics:
    return _METRICS


def reset_library_perf_metrics() -> None:
    _METRICS.reset()
