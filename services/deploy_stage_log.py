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

from services.deploy_op_profile import DeployOpProfile

logger = logging.getLogger(__name__)

SLOW_THRESHOLD_MS = 1000.0


@dataclass
class DeployStageContext:
    internal_id: str = ""
    mod_pk: int = 0
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
    internal_id: str = ""
    mod_pk: int = 0
    mod_name: str = ""
    source: str = ""
    target: str = ""
    strategy: str = ""
    archive_type: str = ""
    records: list[StageTiming] = field(default_factory=list)
    t0: float = 0.0
    files: int = 0
    bytes: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)
    ops: DeployOpProfile = field(default_factory=DeployOpProfile)

    def add(self, stage: str, elapsed_ms: float, files: int = 0, bytes_count: int = 0) -> None:
        rec = StageTiming(
            stage=stage, elapsed_ms=elapsed_ms, files=files, bytes=bytes_count
        )
        self.records.append(rec)

    def stage_ms(self, stage: str) -> float:
        return float(sum(r.elapsed_ms for r in self.records if r.stage == stage))

    def total_elapsed_ms(self) -> float:
        if self.t0:
            return (time.perf_counter() - self.t0) * 1000.0
        return 0.0

    def named_stage_ms(self) -> dict[str, float]:
        """Canonical Phase-7 deploy timing fields (milliseconds)."""
        return {
            "resolve_ms": round(self.stage_ms("resolve"), 3),
            "verify_source_ms": round(self.stage_ms("verify_source"), 3),
            "extract_ms": round(self.stage_ms("extract"), 3),
            "plan_ms": round(self.stage_ms("plan"), 3),
            "fileplan_ms": round(self.stage_ms("fileplan"), 3),
            "conflict_scan_ms": round(self.stage_ms("conflict_scan"), 3),
            "backup_ms": round(self.stage_ms("backup"), 3),
            "copy_ms": round(self.stage_ms("copy"), 3),
            "manifest_ms": round(self.stage_ms("manifest"), 3),
            "verify_ms": round(self.stage_ms("validate"), 3),
            "hash_ms": round(self.stage_ms("hash"), 3),
            "persist_ms": round(self.stage_ms("persist"), 3),
            "cleanup_ms": round(self.stage_ms("cleanup"), 3),
            "total_ms": round(self.total_elapsed_ms(), 3),
        }

    def accounted_stage_ms(self) -> float:
        named = self.named_stage_ms()
        return float(
            sum(value for key, value in named.items() if key != "total_ms")
        )

    def slowest_named_stage(self) -> tuple[str, float]:
        named = self.named_stage_ms()
        items = [(key, value) for key, value in named.items() if key != "total_ms"]
        if not items:
            return "", 0.0
        key, value = max(items, key=lambda item: item[1])
        return key, value

    def to_dict(self) -> dict[str, Any]:
        named = self.named_stage_ms()
        slowest, slowest_ms = self.slowest_named_stage()
        accounted = self.accounted_stage_ms()
        total = float(named.get("total_ms") or 0.0)
        out: dict[str, Any] = {
            "internal_id": self.internal_id,
            "mod_id": self.internal_id,
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
            "diagnostics": dict(self.diagnostics),
            "op_profile": self.ops.as_dict(),
            "slowest_stage": slowest,
            "slowest_ms": round(slowest_ms, 3),
            "accounted_ms": round(accounted, 3),
            "unaccounted_ms": round(max(0.0, total - accounted), 3),
        }
        out.update(named)
        if self.mod_pk:
            out["mod_pk"] = int(self.mod_pk)
        return out


_TIMING: ContextVar[DeployTimingSession | None] = ContextVar(
    "deploy_timing_session", default=None
)


def current_deploy_timing() -> DeployTimingSession | None:
    return _TIMING.get()


@contextmanager
def deploy_timing_session(
    *,
    internal_id: str = "",
    mod_pk: int = 0,
    mod_name: str = "",
    source: str = "",
    target: str = "",
    strategy: str = "",
    archive_type: str = "",
) -> Iterator[DeployTimingSession]:
    sess = DeployTimingSession(
        internal_id=str(internal_id),
        mod_pk=int(mod_pk or 0),
        mod_name=str(mod_name),
        source=str(source),
        target=str(target),
        strategy=str(strategy),
        archive_type=str(archive_type),
        t0=time.perf_counter(),
    )
    token = _TIMING.set(sess)
    logger.info(
        "[DEPLOY_START] internal_id=%s mod_pk=%s mod_name=%s source=%s target=%s strategy=%s archive_type=%s",
        sess.internal_id,
        sess.mod_pk or "",
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
    named = sess.named_stage_ms()
    total_ms = named["total_ms"]
    nfiles = sess.files if files is None else files
    nbytes = sess.bytes if bytes_count is None else bytes_count
    src = source or sess.source
    tgt = target or sess.target
    slowest, slowest_ms = sess.slowest_named_stage()
    logger.info(
        "[DEPLOY_TIMING] internal_id=%s mod_pk=%s "
        "resolve_ms=%.1f verify_source_ms=%.1f extract_ms=%.1f plan_ms=%.1f "
        "backup_ms=%.1f copy_ms=%.1f manifest_ms=%.1f verify_ms=%.1f "
        "total_ms=%.1f slowest_stage=%s slowest_ms=%.1f files=%s bytes=%s",
        sess.internal_id,
        sess.mod_pk or "",
        named["resolve_ms"],
        named["verify_source_ms"],
        named["extract_ms"],
        named["plan_ms"],
        named["backup_ms"],
        named["copy_ms"],
        named["manifest_ms"],
        named["verify_ms"],
        total_ms,
        slowest,
        slowest_ms,
        nfiles,
        nbytes,
    )
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
    root = Path(managed_folder)
    try:
        if not root.is_dir():
            return
    except OSError:
        return
    path = root / ".info" / "deploy_timing.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(sess.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        traces = list(getattr(sess.ops, "copy_files", None) or [])
        if traces:
            plan_path = Path(__file__).resolve().parents[1] / "_tmp" / "copy_plan.json"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text(
                json.dumps(
                    [
                        {
                            "relative": r.relative,
                            "source": r.source,
                            "target": r.target,
                            "size": int(r.size),
                            "elapsed_ms": round(float(r.elapsed_ms), 3),
                            "read_write_ms": round(float(r.read_write_ms), 3),
                            "hash_ms": round(float(r.hash_ms), 3),
                            "copystat_ms": round(float(r.copystat_ms), 3),
                            "source_volume": r.source_volume,
                            "target_volume": r.target_volume,
                        }
                        for r in traces
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
    except OSError:
        logger.debug("deploy timing write failed", exc_info=True)


@contextmanager
def deploy_stage(
    stage: str,
    *,
    ctx: DeployStageContext | None = None,
    internal_id: str = "",
    mod_pk: int | str = 0,
    extra: str = "",
    files: int = 0,
    bytes_count: int = 0,
) -> Iterator[None]:
    """Log structured ``[DEPLOY_STAGE]`` start/finish; warn when slow."""
    c = ctx or DeployStageContext(internal_id=internal_id, mod_pk=int(mod_pk or 0))
    pk = int(c.mod_pk or 0)
    parts = [
        f"internal_id={c.internal_id}" if c.internal_id else "",
        f"mod_pk={pk}" if pk else "",
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
    sess = current_deploy_timing()
    if sess is not None and sess.ops.last_stage_end:
        gap_ms = (t0 - sess.ops.last_stage_end) * 1000.0
        if gap_ms >= 0.05:
            sess.ops.note_gap(
                after=sess.ops.last_stage_name or "start",
                before=stage,
                ms=gap_ms,
            )
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        sess = current_deploy_timing()
        if sess is not None:
            sess.add(stage, elapsed_ms, files=files, bytes_count=bytes_count)
            sess.ops.last_stage_end = time.perf_counter()
            sess.ops.last_stage_name = stage
        finished = [
            f"internal_id={c.internal_id}" if c.internal_id else "",
            f"mod_pk={pk}" if pk else "",
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
