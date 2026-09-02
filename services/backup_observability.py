"""Structured metadata-backup timing. Observability only — not an optimizer."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class BackupStageTiming:
    stage: str
    elapsed_ms: float
    files: int = 0
    bytes: int = 0
    mods: int = 0
    changed_files: int = 0
    unchanged_files: int = 0


@dataclass
class BackupTimingSession:
    reason: str = ""
    records: list[BackupStageTiming] = field(default_factory=list)
    t0: float = 0.0
    files: int = 0
    bytes: int = 0
    mods: int = 0
    changed_files: int = 0
    unchanged_files: int = 0
    copied: bool = False

    def add(self, stage: str, elapsed_ms: float, **counts: int) -> None:
        rec = BackupStageTiming(
            stage=stage,
            elapsed_ms=elapsed_ms,
            files=int(counts.get("files") or 0),
            bytes=int(counts.get("bytes") or 0),
            mods=int(counts.get("mods") or 0),
            changed_files=int(counts.get("changed_files") or 0),
            unchanged_files=int(counts.get("unchanged_files") or 0),
        )
        self.records.append(rec)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "files": self.files,
            "bytes": self.bytes,
            "mods": self.mods,
            "changed_files": self.changed_files,
            "unchanged_files": self.unchanged_files,
            "copied": self.copied,
            "total_elapsed_ms": round((time.perf_counter() - self.t0) * 1000.0, 3)
            if self.t0
            else 0.0,
            "stages": [
                {
                    "stage": r.stage,
                    "elapsed_ms": round(r.elapsed_ms, 3),
                    "files": r.files,
                    "bytes": r.bytes,
                    "mods": r.mods,
                    "changed_files": r.changed_files,
                    "unchanged_files": r.unchanged_files,
                }
                for r in self.records
            ],
        }


def log_backup_start(*, reason: str, mods: int = 0, extra: str = "") -> None:
    logger.info(
        "[BACKUP_START] reason=%s mods=%s %s",
        reason,
        mods,
        extra,
    )


def log_backup_stage(
    stage: str,
    *,
    elapsed_ms: float,
    files: int = 0,
    bytes_count: int = 0,
    mods: int = 0,
    changed_files: int = 0,
    unchanged_files: int = 0,
) -> None:
    logger.info(
        "[BACKUP_STAGE] stage=%s elapsed_ms=%.1f files=%s bytes=%s mods=%s "
        "changed=%s unchanged=%s",
        stage,
        elapsed_ms,
        files,
        bytes_count,
        mods,
        changed_files,
        unchanged_files,
    )


def log_backup_result(session: BackupTimingSession, *, status: str = "ok") -> None:
    payload = session.to_dict()
    logger.info(
        "[BACKUP_RESULT] status=%s reason=%s total_elapsed_ms=%s mods=%s "
        "files=%s bytes=%s changed=%s unchanged=%s copied=%s stages=%s",
        status,
        session.reason,
        payload["total_elapsed_ms"],
        session.mods,
        session.files,
        session.bytes,
        session.changed_files,
        session.unchanged_files,
        session.copied,
        payload["stages"],
    )


@contextmanager
def backup_stage(stage: str, **counts: int) -> Iterator[None]:
    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        log_backup_stage(
            stage,
            elapsed_ms=elapsed_ms,
            files=int(counts.get("files") or 0),
            bytes_count=int(counts.get("bytes") or 0),
            mods=int(counts.get("mods") or 0),
            changed_files=int(counts.get("changed_files") or 0),
            unchanged_files=int(counts.get("unchanged_files") or 0),
        )
