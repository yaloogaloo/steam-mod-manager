"""End-to-end Deploy user-perceived latency tracer.

Instrumentation only: unique job_id, lifecycle timestamps, 1:1 counts,
post-deploy operation timings, still-running background tasks.

Does not change copy / deploy behavior.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_JOBS: dict[str, "DeployE2EJob"] = {}
_ACTIVE_ID = ""
_THREAD_JOB = threading.local()


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def dump_dir() -> Path:
    path = _project_root() / "_tmp" / "deploy_e2e"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _thread_label() -> tuple[str, bool]:
    name = threading.current_thread().name
    ui = False
    try:
        from PySide6.QtCore import QCoreApplication, QThread

        app = QCoreApplication.instance()
        if app is not None and QThread.currentThread() is app.thread():
            ui = True
            name = f"ui:{name}"
        elif app is not None:
            name = f"qt:{name}"
    except Exception:  # noqa: BLE001
        pass
    return name, ui


def is_ui_thread() -> bool:
    try:
        from PySide6.QtCore import QCoreApplication, QThread

        app = QCoreApplication.instance()
        if app is None:
            return False
        return QThread.currentThread() is app.thread()
    except Exception:  # noqa: BLE001
        return False


def _now() -> float:
    return time.perf_counter()


def _wall_ms() -> float:
    return time.time() * 1000.0


@dataclass
class OpAgg:
    operation: str
    count: int = 0
    total_ms: float = 0.0
    largest_ms: float = 0.0
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    ui_thread_count: int = 0
    threads: set[str] = field(default_factory=set)

    def add(
        self,
        elapsed_ms: float,
        *,
        thread: str,
        ui_thread: bool,
        files: int = 0,
        dirs: int = 0,
        bytes_count: int = 0,
    ) -> None:
        self.count += 1
        self.total_ms += float(elapsed_ms)
        if elapsed_ms > self.largest_ms:
            self.largest_ms = float(elapsed_ms)
        self.files += int(files or 0)
        self.dirs += int(dirs or 0)
        self.bytes += int(bytes_count or 0)
        self.threads.add(thread)
        if ui_thread:
            self.ui_thread_count += 1


@dataclass
class BgTask:
    name: str
    started_at: float
    trigger_job_id: str
    thread: str
    finished_at: float | None = None

    @property
    def duration_ms(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at) * 1000.0


class DeployE2EJob:
    def __init__(self, job_id: str, *, internal_id: str = "", action: str = "deploy") -> None:
        self.job_id = job_id
        self.internal_id = str(internal_id or "").strip()
        self.action = str(action or "deploy")
        self.t0 = _now()
        self.wall0 = _wall_ms()
        self.events: list[dict[str, Any]] = []
        self.ops: dict[str, OpAgg] = {}
        self.pending: dict[str, BgTask] = {}
        self.finished_tasks: list[dict[str, Any]] = []
        self.counts: dict[str, int] = defaultdict(int)
        self.marks: dict[str, float] = {}
        self.core_deploy_ms: float | None = None
        self.ended = False
        self.end_reason = ""
        self.ui_slot_returned = False
        self.worker_thread_finished = False
        self.worker_started_mark = False
        self.dump_path: Path | None = None
        self.case: str = ""
        self.still_running_at_end: list[str] = []
        self.one_to_one_ok = True
        self.one_to_one_violations: list[str] = []

    def elapsed_ms(self) -> float:
        return (_now() - self.t0) * 1000.0

    def event(self, label: str, **extra: Any) -> None:
        thread, ui = _thread_label()
        prev = self.events[-1]["timestamp"] if self.events else self.t0
        ts = _now()
        rec = {
            "label": label,
            "timestamp": round(ts, 6),
            "wall_ms": round(_wall_ms(), 3),
            "delta_ms": round((ts - prev) * 1000.0, 3),
            "elapsed_ms": round((ts - self.t0) * 1000.0, 3),
            "thread": thread,
            "ui_thread": bool(ui),
            "job_id": self.job_id,
        }
        if extra:
            rec["extra"] = extra
        self.events.append(rec)
        self.counts[f"event:{label}"] += 1
        logger.info(
            "[DEPLOY_E2E] job_id=%s label=%s elapsed_ms=%.1f delta_ms=%.1f thread=%s",
            self.job_id,
            label,
            rec["elapsed_ms"],
            rec["delta_ms"],
            thread,
        )

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] += int(n)

    def mark(self, name: str) -> None:
        self.marks[name] = _now()

    def span_ms(self, start_mark: str, end_mark: str) -> float | None:
        a = self.marks.get(start_mark)
        b = self.marks.get(end_mark)
        if a is None or b is None:
            return None
        return (b - a) * 1000.0

    def add_op(
        self,
        operation: str,
        elapsed_ms: float,
        *,
        files: int = 0,
        dirs: int = 0,
        bytes_count: int = 0,
    ) -> None:
        thread, ui = _thread_label()
        agg = self.ops.get(operation)
        if agg is None:
            agg = OpAgg(operation=operation)
            self.ops[operation] = agg
        agg.add(
            elapsed_ms,
            thread=thread,
            ui_thread=bool(ui),
            files=files,
            dirs=dirs,
            bytes_count=bytes_count,
        )
        if ui:
            self.count("ui_thread_fs_ops" if "dir" in operation or "walk" in operation or "rglob" in operation or operation in {
                "directory_size",
                "os.walk",
                "rglob",
                "iterdir",
                "stat",
                "load_metadata",
                "load_manifest",
                "copytree",
            } else "ui_thread_ops")

    def task_started(self, name: str) -> str:
        key = f"{name}#{self.counts[f'task:{name}'] + 1}"
        self.counts[f"task:{name}"] += 1
        thread, _ui = _thread_label()
        self.pending[key] = BgTask(
            name=name,
            started_at=_now(),
            trigger_job_id=self.job_id,
            thread=thread,
        )
        self.event("task_started", task=name, task_key=key)
        return key

    def task_finished(self, key: str) -> None:
        task = self.pending.pop(key, None)
        if task is None:
            # allow finish by name prefix
            for pending_key, pending in list(self.pending.items()):
                if pending.name == key or pending_key.startswith(key):
                    task = self.pending.pop(pending_key)
                    key = pending_key
                    break
        if task is None:
            return
        task.finished_at = _now()
        rec = {
            "task": task.name,
            "task_key": key,
            "trigger_job_id": task.trigger_job_id,
            "thread": task.thread,
            "duration_ms": round(float(task.duration_ms or 0.0), 3),
        }
        self.finished_tasks.append(rec)
        self.event("task_finished", **rec)

    def pending_names(self) -> list[str]:
        return [t.name for t in self.pending.values()]

    def can_end(self) -> bool:
        if self.ended:
            return True
        if not self.ui_slot_returned:
            return False
        if not self.worker_thread_finished:
            return False
        if self.pending:
            return False
        return True

    def finish(self, *, reason: str = "settled") -> None:
        if self.ended:
            return
        self.end_reason = reason
        self.event("DEPLOY_E2E_END", reason=reason, pending=list(self.pending_names()))
        self.ended = True
        self.still_running_at_end = _snapshot_background_tasks()
        self._evaluate_one_to_one()
        self.dump()

    def _evaluate_one_to_one(self) -> None:
        expected = {
            "deploy_action_count": 1,
            "worker_count": 1,
            "deploy_mod_count": 1,
            "finish_signal_count": 1,
        }
        violations: list[str] = []
        for key, want in expected.items():
            got = int(self.counts.get(key) or 0)
            if got != want:
                violations.append(f"{key}={got} (want {want})")
        if int(self.counts.get("worker_count") or 0) > 1:
            violations.append("one click started multiple workers")
        if int(self.counts.get("deploy_mod_count") or 0) > 1:
            violations.append("one worker invoked deploy_mod more than once")
        if int(self.counts.get("finish_signal_count") or 0) > 1:
            violations.append("finished signal emitted more than once")
        if int(self.counts.get("post_refresh_count") or 0) > 1:
            violations.append(
                f"post_refresh_count={self.counts.get('post_refresh_count')} (duplicate refresh)"
            )
        if int(self.counts.get("audit_count") or 0) > 0:
            violations.append(f"audit_count={self.counts.get('audit_count')}")
        if int(self.counts.get("reconcile_count") or 0) > 0:
            violations.append(
                f"reconcile_count={self.counts.get('reconcile_count')} (reconcile started after deploy)"
            )
        self.one_to_one_violations = violations
        self.one_to_one_ok = not violations

    def classify_case(self) -> str:
        core = float(self.core_deploy_ms or 0.0)
        total = self.elapsed_ms()
        post = max(0.0, total - core)
        multi = int(self.counts.get("worker_count") or 0) > 1 or int(
            self.counts.get("deploy_mod_count") or 0
        ) > 1
        if multi:
            self.case = "D"
            return self.case
        if core >= 60_000 and post < 5_000:
            self.case = "A"
            return self.case
        if core < 60_000 and post >= 60_000:
            # Distinguish UI slot stall vs post filesystem.
            ui_slot = self.span_ms("ui_finished_entered", "ui_finished_returned") or 0.0
            if ui_slot >= 60_000:
                self.case = "B"
            else:
                self.case = "C"
            return self.case
        if post >= 5_000 and core < total * 0.7:
            ui_slot = self.span_ms("ui_finished_entered", "ui_finished_returned") or 0.0
            self.case = "B" if ui_slot >= post * 0.5 else "C"
            return self.case
        self.case = "A" if core >= post else "mixed"
        return self.case

    def summary(self) -> dict[str, Any]:
        total = self.elapsed_ms() if not self.ended else (
            float(self.events[-1]["elapsed_ms"]) if self.events else self.elapsed_ms()
        )
        core = float(self.core_deploy_ms or 0.0)
        ui_slot = self.span_ms("ui_finished_entered", "ui_finished_returned")
        worker_life = self.span_ms("worker_created", "worker_thread_finished")
        post = max(0.0, total - core)
        settle = 0.0
        if ui_slot is not None:
            settle = max(0.0, post - float(ui_slot))
        self.classify_case()
        return {
            "job_id": self.job_id,
            "internal_id": self.internal_id,
            "action": self.action,
            "ended": self.ended,
            "end_reason": self.end_reason,
            "USER_E2E_s": round(total / 1000.0, 3),
            "CORE_DEPLOY_s": round(core / 1000.0, 3),
            "POST_DEPLOY_s": round(post / 1000.0, 3),
            "UI_SETTLE_s": round(settle / 1000.0, 3),
            "total_e2e_ms": round(total, 3),
            "core_deploy_ms": round(core, 3),
            "post_deploy_ms": round(post, 3),
            "ui_settle_ms": round(settle, 3),
            "ui_finished_slot_ms": round(float(ui_slot or 0.0), 3),
            "worker_lifecycle_ms": round(float(worker_life or 0.0), 3),
            "deploy_action_count": int(self.counts.get("deploy_action_count") or 0),
            "worker_count": int(self.counts.get("worker_count") or 0),
            "deploy_mod_count": int(self.counts.get("deploy_mod_count") or 0),
            "finish_signal_count": int(self.counts.get("finish_signal_count") or 0),
            "qthread_finished_count": int(self.counts.get("qthread_finished_count") or 0),
            "post_refresh_count": int(self.counts.get("post_refresh_count") or 0),
            "notify_mod_changed_count": int(self.counts.get("notify_mod_changed_count") or 0),
            "show_mod_count": int(self.counts.get("show_mod_count") or 0),
            "audit_count": int(self.counts.get("audit_count") or 0),
            "reconcile_count": int(self.counts.get("reconcile_count") or 0),
            "library_refresh_count": int(self.counts.get("library_refresh_count") or 0),
            "library_load_worker_count": int(self.counts.get("library_load_worker_count") or 0),
            "one_to_one_ok": self.one_to_one_ok,
            "one_to_one_violations": list(self.one_to_one_violations),
            "case": self.case,
            "pending_at_end": list(self.pending_names()),
            "still_running_background_tasks": list(self.still_running_at_end),
            "ops": [
                {
                    "operation": op.operation,
                    "thread": ",".join(sorted(op.threads)) or "",
                    "count": op.count,
                    "total_ms": round(op.total_ms, 3),
                    "largest_ms": round(op.largest_ms, 3),
                    "files": op.files,
                    "dirs": op.dirs,
                    "bytes": op.bytes,
                    "ui_thread_count": op.ui_thread_count,
                }
                for op in sorted(self.ops.values(), key=lambda x: -x.total_ms)
            ],
            "events": self.events,
            "finished_tasks": self.finished_tasks,
            "counts": dict(self.counts),
        }

    def dump(self) -> Path:
        payload = self.summary()
        path = dump_dir() / f"{self.job_id}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        ndjson = dump_dir() / "events.ndjson"
        with ndjson.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.dump_path = path
        logger.info("[DEPLOY_E2E] dumped %s", path)
        return path


def _snapshot_background_tasks() -> list[str]:
    out: list[str] = []
    for thread in threading.enumerate():
        if not thread.is_alive():
            continue
        name = thread.name or ""
        if name in {"MainThread"}:
            continue
        out.append(f"thread:{name}")
    try:
        from PySide6.QtCore import QThreadPool

        pool = QThreadPool.globalInstance()
        if pool is not None:
            active = int(pool.activeThreadCount())
            if active:
                out.append(f"QThreadPool.active={active}")
    except Exception:  # noqa: BLE001
        pass
    try:
        from services.library_reconcile import is_reconcile_running

        if is_reconcile_running():
            out.append("reconcile_library:running")
    except Exception:  # noqa: BLE001
        pass
    return out


def start_deploy_job(
    *,
    internal_id: str = "",
    action: str = "deploy",
    source: str = "ui_click",
) -> DeployE2EJob:
    global _ACTIVE_ID
    job_id = uuid.uuid4().hex[:12]
    job = DeployE2EJob(job_id, internal_id=internal_id, action=action)
    with _LOCK:
        _JOBS[job_id] = job
        _ACTIVE_ID = job_id
    _bind_thread(job_id)
    job.count("deploy_action_count")
    job.event("DEPLOY_E2E_START", source=source, action=action, internal_id=internal_id)
    if source in {"ui_click", "measure_ui"}:
        job.event("UI click")
        job.mark("ui_click")
    return job


def bind_job(job_id: str) -> DeployE2EJob | None:
    job_id = str(job_id or "").strip()
    if not job_id:
        return current_job()
    _bind_thread(job_id)
    return _JOBS.get(job_id)


def _bind_thread(job_id: str) -> None:
    _THREAD_JOB.job_id = job_id


def current_job() -> DeployE2EJob | None:
    job_id = str(getattr(_THREAD_JOB, "job_id", "") or "")
    if job_id and job_id in _JOBS:
        return _JOBS[job_id]
    if _ACTIVE_ID and _ACTIVE_ID in _JOBS:
        job = _JOBS[_ACTIVE_ID]
        if not job.ended:
            return job
    return None


def e2e_event(label: str, **extra: Any) -> None:
    job = current_job()
    if job is None:
        return
    job.event(label, **extra)


def e2e_count(key: str, n: int = 1) -> None:
    job = current_job()
    if job is None:
        return
    job.count(key, n)


def e2e_mark(name: str) -> None:
    job = current_job()
    if job is None:
        return
    job.mark(name)


def e2e_core_deploy_ms(ms: float) -> None:
    job = current_job()
    if job is None:
        return
    job.core_deploy_ms = float(ms)


@contextmanager
def e2e_span(label: str, **extra: Any) -> Iterator[DeployE2EJob | None]:
    job = current_job()
    if job is None:
        yield None
        return
    job.event(f"{label} entered", **extra)
    t0 = _now()
    try:
        yield job
    finally:
        elapsed = (_now() - t0) * 1000.0
        job.add_op(label, elapsed)
        job.event(f"{label} returned", duration_ms=round(elapsed, 3), **extra)


def e2e_op(
    operation: str,
    elapsed_ms: float,
    *,
    files: int = 0,
    dirs: int = 0,
    bytes_count: int = 0,
) -> None:
    job = current_job()
    if job is None:
        return
    job.add_op(
        operation,
        elapsed_ms,
        files=files,
        dirs=dirs,
        bytes_count=bytes_count,
    )


def e2e_task_started(name: str) -> str:
    job = current_job()
    if job is None:
        return ""
    return job.task_started(name)


def e2e_task_finished(key: str) -> None:
    job = current_job()
    if job is None:
        return
    if key:
        job.task_finished(key)


def note_ui_slot_returned() -> None:
    job = current_job()
    if job is None:
        return
    job.ui_slot_returned = True
    job.mark("ui_finished_returned")
    maybe_end()


def note_worker_thread_finished() -> None:
    job = current_job()
    if job is None:
        return
    job.worker_thread_finished = True
    job.mark("worker_thread_finished")
    maybe_end()


def maybe_end(*, force: bool = False, reason: str = "settled") -> None:
    job = current_job()
    if job is None or job.ended:
        return
    if force or job.can_end():
        job.finish(reason=reason)


def wait_until_ended(timeout_s: float = 600.0, *, pump_qt: bool = True) -> DeployE2EJob | None:
    job = current_job()
    if job is None:
        return None
    deadline = time.time() + float(timeout_s)
    idle_rounds = 0
    while time.time() < deadline:
        if pump_qt:
            try:
                from PySide6.QtCore import QCoreApplication

                app = QCoreApplication.instance()
                if app is not None:
                    app.processEvents()
            except Exception:  # noqa: BLE001
                pass
        if job.ended:
            return job
        if job.can_end():
            idle_rounds += 1
            # Drain one extra Qt 0ms timer / queued slot before END.
            if idle_rounds >= 2:
                job.finish(reason="settled")
                return job
        else:
            idle_rounds = 0
        time.sleep(0.02)
    job.finish(reason="timeout")
    return job


def reset_e2e() -> None:
    global _ACTIVE_ID
    with _LOCK:
        _JOBS.clear()
        _ACTIVE_ID = ""
    _THREAD_JOB.job_id = ""


def latest_job() -> DeployE2EJob | None:
    if _ACTIVE_ID and _ACTIVE_ID in _JOBS:
        return _JOBS[_ACTIVE_ID]
    return None
