"""Structured deploy pipeline stage timing logs."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SLOW_THRESHOLD_MS = 1000.0


@dataclass
class DeployStageContext:
    mod_id: str = ""
    app_id: int = 0
    strategy: str = ""
    source: str = ""
    target: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class StageTiming:
    stage: str
    elapsed_ms: float
    files: int = 0
    bytes: int = 0


@dataclass
class DeployTimingSession:
    mod_id: str = ""
    mod_name: str = ""
    source: str = ""
    target: str = ""
    strategy: str = ""
    archive_type: str = ""
    records: list[StageTiming] = field(default_factory=list)
    t0: float = 0.0
    files: int = 0
    bytes: int = 0

    def add(self, stage: str, elapsed_ms: float, files: int = 0, bytes_count: int = 0) -> None:
        rec = StageTiming(
            stage=stage, elapsed_ms=elapsed_ms, files=files, bytes=bytes_count
        )
        self.records.append(rec)

    def stage_ms(self, stage: str) -> float:
        for rec in self.records:
            if rec.stage == stage:
                return rec.elapsed_ms
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "mod_name": self.mod_name,
            "source": self.source,
            "target": self.target,
            "strategy": self.strategy,
            "archive_type": self.archive_type,
            "files": self.files,
            "bytes": self.bytes,
            "stages": [
                {
                    "stage": r.stage,
                    "elapsed_ms": round(r.elapsed_ms, 3),
                    "files": r.files,
                    "bytes": r.bytes,
                }
                for r in self.records
            ],
        }


_TIMING: ContextVar[DeployTimingSession | None] = ContextVar(
    "deploy_timing_session", default=None
)


def current_deploy_timing() -> DeployTimingSession | None:
    return _TIMING.get()


@contextmanager
def deploy_timing_session(
    *,
    mod_id: str = "",
    mod_name: str = "",
    source: str = "",
    target: str = "",
    strategy: str = "",
    archive_type: str = "",
) -> Iterator[DeployTimingSession]:
    sess = DeployTimingSession(
        mod_id=str(mod_id),
        mod_name=str(mod_name),
        source=str(source),
        target=str(target),
        strategy=str(strategy),
        archive_type=str(archive_type),
        t0=time.perf_counter(),
    )
    token = _TIMING.set(sess)
    logger.info(
        "[DEPLOY_START] mod_id=%s mod_name=%s source=%s target=%s strategy=%s archive_type=%s",
        sess.mod_id,
        sess.mod_name or "",
        sess.source or "",
        sess.target or "",
        sess.strategy or "",
        sess.archive_type or "",
    )
    try:
        yield sess
    finally:
        _TIMING.reset(token)


def log_deploy_result(
    sess: DeployTimingSession,
    *,
    status: str,
    error: str = "",
    files: int | None = None,
    bytes_count: int | None = None,
    source: str = "",
    target: str = "",
) -> None:
    total_ms = (time.perf_counter() - sess.t0) * 1000.0 if sess.t0 else 0.0
    nfiles = sess.files if files is None else files
    nbytes = sess.bytes if bytes_count is None else bytes_count
    src = source or sess.source
    tgt = target or sess.target
    logger.info(
        "[DEPLOY_RESULT] status=%s total_elapsed_ms=%.1f files=%s bytes=%s "
        "source=%s target=%s backup_elapsed_ms=%.1f extract_elapsed_ms=%.1f "
        "copy_elapsed_ms=%.1f validate_elapsed_ms=%.1f persist_elapsed_ms=%.1f "
        "conflict_scan_elapsed_ms=%.1f error=%s",
        status,
        total_ms,
        nfiles,
        nbytes,
        src,
        tgt,
        sess.stage_ms("backup"),
        sess.stage_ms("extract"),
        sess.stage_ms("copy"),
        sess.stage_ms("validate"),
        sess.stage_ms("persist"),
        sess.stage_ms("conflict_scan"),
        error,
    )


def write_deploy_timing(managed_folder: str | Path | None, sess: DeployTimingSession) -> None:
    if not managed_folder:
        return
    path = Path(managed_folder) / ".info" / "deploy_timing.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(sess.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("deploy timing write failed", exc_info=True)


@contextmanager
def deploy_stage(
    stage: str,
    *,
    ctx: DeployStageContext | None = None,
    mod_id: str = "",
    extra: str = "",
    files: int = 0,
    bytes_count: int = 0,
) -> Iterator[None]:
    """Log structured ``[DEPLOY_STAGE]`` start/finish; warn when slow."""
    c = ctx or DeployStageContext(mod_id=mod_id)
    parts = [
        f"mod_id={c.mod_id}" if c.mod_id else "",
        f"app_id={c.app_id}" if c.app_id else "",
        f"strategy={c.strategy}" if c.strategy else "",
        f"stage={stage}",
        "event=started",
    ]
    if extra:
        parts.append(extra)
    if c.source:
        parts.append(f"source={c.source}")
    if c.target:
        parts.append(f"target={c.target}")
    logger.info("[DEPLOY_STAGE] %s", " ".join(p for p in parts if p))

    t0 = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        sess = current_deploy_timing()
        if sess is not None:
            sess.add(stage, elapsed_ms, files=files, bytes_count=bytes_count)
        finished = [
            f"mod_id={c.mod_id}" if c.mod_id else "",
            f"app_id={c.app_id}" if c.app_id else "",
            f"strategy={c.strategy}" if c.strategy else "",
            f"stage={stage}",
            "event=finished",
            f"elapsed_ms={elapsed_ms:.1f}",
            f"files={files}" if files else "",
            f"bytes={bytes_count}" if bytes_count else "",
        ]
        line = "[DEPLOY_STAGE] " + " ".join(p for p in finished if p)
        if elapsed_ms >= SLOW_THRESHOLD_MS:
            logger.warning("[DEPLOY_SLOW] %s", line)
        else:
            logger.info(line)
