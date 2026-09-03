"""Startup I/O overlap math — no GUI."""

from __future__ import annotations

from services.startup_io_trace import (
    begin,
    end,
    note_reconcile_queued,
    reset_startup_io_trace,
    summarize,
)


def test_overlap_summary_three_components() -> None:
    reset_startup_io_trace()
    begin("reconcile")
    begin("library_load")
    begin("cover_loader")
    end("cover_loader")
    end("library_load")
    end("reconcile")
    payload = summarize()
    assert payload["max_concurrent_startup_io_tasks"] >= 3
    assert payload["all_three_overlap_ms"] >= 0
    assert payload["reconcile_elapsed_ms"] >= 0
    assert payload["reconcile_overlap_detected"] is False


def test_queued_follow_up_sets_flag() -> None:
    reset_startup_io_trace()
    note_reconcile_queued()
    payload = summarize()
    assert payload["reconcile_overlap_detected"] is True
    assert payload["reconcile_queued_count"] == 1
