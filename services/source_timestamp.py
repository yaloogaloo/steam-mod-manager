"""Steam Workshop source timestamp comparison for full-library sync."""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class SourceTimestampDecision(str, Enum):
    """Outcome of comparing a remote/source timestamp with the local sync baseline."""

    UPDATED = "UPDATED"
    SKIPPED = "SKIPPED"
    UNKNOWN = "UNKNOWN"
    BASELINE = "BASELINE"


def normalize_source_timestamp(value: int | float | None) -> int | None:
    """Return a positive Unix epoch second or ``None`` when missing/invalid."""
    if value is None:
        return None
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    return ts if ts > 0 else None


def compare_source_timestamps(
    source_timestamp: int | None,
    local_timestamp: int | None,
    *,
    local_sync_complete: bool,
) -> SourceTimestampDecision:
    """
    Compare Steam/source content time with the last successfully synced baseline.

    Rules:
    - ``source > local`` → UPDATED
    - ``source == local`` → SKIPPED
    - ``source < local`` → SKIPPED (regression; local is not lowered)
    - both missing → UNKNOWN
    - source missing, local present → UNKNOWN (do not force resync)
    - source present, local missing, local complete → BASELINE (establish only)
    - source present, local missing, local incomplete → UPDATED (needs sync)
    """
    source = normalize_source_timestamp(source_timestamp)
    local = normalize_source_timestamp(local_timestamp)

    if source is None and local is None:
        return SourceTimestampDecision.UNKNOWN
    if source is None:
        return SourceTimestampDecision.UNKNOWN
    if local is None:
        if local_sync_complete:
            return SourceTimestampDecision.BASELINE
        return SourceTimestampDecision.UPDATED
    if source > local:
        return SourceTimestampDecision.UPDATED
    if source < local:
        logger.info(
            "[SYNC] source_timestamp_regression source=%s local=%s",
            source,
            local,
        )
    return SourceTimestampDecision.SKIPPED
