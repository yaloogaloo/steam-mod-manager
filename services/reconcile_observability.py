"""Reconcile / metadata-backup timing. Observability only — no behavior changes."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    idx = int(round((p / 100.0) * (len(sorted_vals) - 1)))
    idx = max(0, min(idx, len(sorted_vals) - 1))
    return float(sorted_vals[idx])


@dataclass
class ModReconcileTiming:
    internal_id: str = ""
    folder: str = ""
    reason: str = ""
    discover_ms: float = 0.0
    metadata_scan_ms: float = 0.0
    compare_ms: float = 0.0
    hash_ms: float = 0.0
    hash_files: int = 0
    hash_bytes: int = 0
    manifest_ms: float = 0.0
    backup_copy_ms: float = 0.0
    copy_files: int = 0
    copy_bytes: int = 0
    persist_ms: float = 0.0
    payload_walk_ms: float = 0.0
    payload_files: int = 0
    identity_ms: float = 0.0
    total_ms: float = 0.0
    skipped: bool = False
    metadata_rewritten: bool = False
    files_copied: bool = False
    size_mismatch_skips: int = 0
    size_match_then_hash: int = 0
    mtime_equal_then_hash: int = 0
    mtime_unequal_then_hash: int = 0
    t0: float = 0.0
    depth: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.metadata_rewritten or self.files_copied)


@dataclass
class ReconcileSummary:
    mods_seen: int = 0
    mods_changed: int = 0
    mods_unchanged: int = 0
    backups_started: int = 0
    backups_skipped: int = 0
    total_ms: float = 0.0
    discover_ms: float = 0.0
    scan_ms: float = 0.0
    compare_ms: float = 0.0
    hash_ms: float = 0.0
    hash_files: int = 0
    hash_bytes: int = 0
    manifest_ms: float = 0.0
    copy_ms: float = 0.0
    copy_files: int = 0
    copy_bytes: int = 0
    persist_ms: float = 0.0
    payload_walk_ms: float = 0.0
    payload_files: int = 0
    identity_ms: float = 0.0
    size_mismatch_skips: int = 0
    size_match_then_hash: int = 0
    mtime_equal_then_hash: int = 0
    mtime_unequal_then_hash: int = 0
    p50_mod_ms: float = 0.0
    p95_mod_ms: float = 0.0
    max_mod_ms: float = 0.0
    top_slow_mods: list[dict[str, Any]] = field(default_factory=list)
    per_mod: list[ModReconcileTiming] = field(default_factory=list)


_tls = threading.local()


def _state() -> dict[str, Any]:
    st = getattr(_tls, "state", None)
    if st is None:
        st = {"bucket": None, "session": None, "session_t0": 0.0, "list_discover_ms": 0.0}
        _tls.state = st
    return st


def current_bucket() -> ModReconcileTiming | None:
    bucket = _state().get("bucket")
    return bucket if isinstance(bucket, ModReconcileTiming) else None


def start_reconcile_session() -> None:
    st = _state()
    st["session"] = ReconcileSummary()
    st["session_t0"] = time.perf_counter()
    st["list_discover_ms"] = 0.0
    st["bucket"] = None
    logger.info("[RECONCILE_SESSION] reconcile_begin")


def add_list_discover_ms(ms: float) -> None:
    st = _state()
    st["list_discover_ms"] = float(ms)
    summary = st.get("session")
    if isinstance(summary, ReconcileSummary):
        summary.discover_ms += float(ms)


def _ensure_bucket() -> ModReconcileTiming | None:
    return current_bucket()


def push_mod_timing(
    *,
    folder: str = "",
    reason: str = "",
    internal_id: str = "",
) -> ModReconcileTiming:
    st = _state()
    bucket = st.get("bucket")
    if isinstance(bucket, ModReconcileTiming):
        bucket.depth += 1
        if folder and not bucket.folder:
            bucket.folder = folder
        if reason and not bucket.reason:
            bucket.reason = reason
        if internal_id and not bucket.internal_id:
            bucket.internal_id = str(internal_id)
        return bucket
    bucket = ModReconcileTiming(
        folder=str(folder or ""),
        reason=str(reason or ""),
        internal_id=str(internal_id or ""),
        t0=time.perf_counter(),
        depth=1,
    )
    st["bucket"] = bucket
    return bucket


def note_mod_id(internal_id: str) -> None:
    bucket = current_bucket()
    if bucket is not None and str(internal_id or "").strip():
        bucket.internal_id = str(internal_id).strip()


def add_ms(field: str, ms: float) -> None:
    bucket = current_bucket()
    if bucket is None:
        return
    cur = float(getattr(bucket, field, 0.0) or 0.0)
    setattr(bucket, field, cur + float(ms))


def add_scan_ms(ms: float) -> None:
    add_ms("metadata_scan_ms", ms)


def add_compare_ms(ms: float) -> None:
    add_ms("compare_ms", ms)


def add_identity_ms(ms: float) -> None:
    add_ms("identity_ms", ms)


def add_persist_ms(ms: float) -> None:
    add_ms("persist_ms", ms)


def add_hash(*, files: int = 0, nbytes: int = 0, ms: float = 0.0) -> None:
    bucket = current_bucket()
    if bucket is None:
        return
    bucket.hash_files += int(files)
    bucket.hash_bytes += int(nbytes)
    bucket.hash_ms += float(ms)


def add_copy(*, files: int = 0, nbytes: int = 0, ms: float = 0.0) -> None:
    bucket = current_bucket()
    if bucket is None:
        return
    bucket.copy_files += int(files)
    bucket.copy_bytes += int(nbytes)
    bucket.backup_copy_ms += float(ms)
    if int(files) > 0 or int(nbytes) > 0 or float(ms) > 0:
        bucket.files_copied = True


def add_payload_walk(*, files: int = 0, ms: float = 0.0) -> None:
    bucket = current_bucket()
    if bucket is None:
        return
    bucket.payload_files += int(files)
    bucket.payload_walk_ms += float(ms)


def note_size_mismatch() -> None:
    bucket = current_bucket()
    if bucket is not None:
        bucket.size_mismatch_skips += 1


def note_size_match_then_hash(*, mtime_equal: bool) -> None:
    bucket = current_bucket()
    if bucket is None:
        return
    bucket.size_match_then_hash += 1
    if mtime_equal:
        bucket.mtime_equal_then_hash += 1
    else:
        bucket.mtime_unequal_then_hash += 1


def note_metadata_rewritten() -> None:
    bucket = current_bucket()
    if bucket is not None:
        bucket.metadata_rewritten = True


def note_backup_started() -> None:
    bucket = current_bucket()
    summary = _state().get("session")
    if isinstance(summary, ReconcileSummary):
        summary.backups_started += 1
    if bucket is not None:
        bucket.skipped = False


def note_backup_skipped() -> None:
    bucket = current_bucket()
    summary = _state().get("session")
    if isinstance(summary, ReconcileSummary):
        summary.backups_skipped += 1
    if bucket is not None:
        bucket.skipped = True


def log_mod_timing(bucket: ModReconcileTiming) -> None:
    logger.info(
        "[RECONCILE_TIMING] mod=%s folder=%s reason=%s skipped=%s changed=%s "
        "discover_ms=%.1f metadata_scan_ms=%.1f compare_ms=%.1f hash_ms=%.1f "
        "manifest_ms=%.1f backup_copy_ms=%.1f persist_ms=%.1f total_ms=%.1f "
        "hash_files=%s hash_bytes=%s copy_files=%s copy_bytes=%s "
        "payload_walk_ms=%.1f payload_files=%s identity_ms=%.1f "
        "size_match_then_hash=%s size_mismatch_skips=%s "
        "mtime_equal_then_hash=%s mtime_unequal_then_hash=%s "
        "metadata_rewritten=%s",
        bucket.internal_id or "?",
        bucket.folder,
        bucket.reason or "?",
        int(bucket.skipped),
        int(bucket.changed),
        bucket.discover_ms,
        bucket.metadata_scan_ms,
        bucket.compare_ms,
        bucket.hash_ms,
        bucket.manifest_ms,
        bucket.backup_copy_ms,
        bucket.persist_ms,
        bucket.total_ms,
        bucket.hash_files,
        bucket.hash_bytes,
        bucket.copy_files,
        bucket.copy_bytes,
        bucket.payload_walk_ms,
        bucket.payload_files,
        bucket.identity_ms,
        bucket.size_match_then_hash,
        bucket.size_mismatch_skips,
        bucket.mtime_equal_then_hash,
        bucket.mtime_unequal_then_hash,
        int(bucket.metadata_rewritten),
    )


def pop_mod_timing() -> ModReconcileTiming | None:
    st = _state()
    bucket = st.get("bucket")
    if not isinstance(bucket, ModReconcileTiming):
        return None
    bucket.depth -= 1
    if bucket.depth > 0:
        return bucket
    bucket.total_ms = (time.perf_counter() - bucket.t0) * 1000.0 if bucket.t0 else 0.0
    st["bucket"] = None
    log_mod_timing(bucket)
    summary = st.get("session")
    if isinstance(summary, ReconcileSummary):
        summary.per_mod.append(bucket)
        summary.mods_seen += 1
        if bucket.skipped:
            pass
        elif bucket.changed:
            summary.mods_changed += 1
        else:
            summary.mods_unchanged += 1
        summary.scan_ms += bucket.metadata_scan_ms
        summary.compare_ms += bucket.compare_ms
        summary.hash_ms += bucket.hash_ms
        summary.hash_files += bucket.hash_files
        summary.hash_bytes += bucket.hash_bytes
        summary.manifest_ms += bucket.manifest_ms
        summary.copy_ms += bucket.backup_copy_ms
        summary.copy_files += bucket.copy_files
        summary.copy_bytes += bucket.copy_bytes
        summary.persist_ms += bucket.persist_ms
        summary.payload_walk_ms += bucket.payload_walk_ms
        summary.payload_files += bucket.payload_files
        summary.identity_ms += bucket.identity_ms
        summary.size_mismatch_skips += bucket.size_mismatch_skips
        summary.size_match_then_hash += bucket.size_match_then_hash
        summary.mtime_equal_then_hash += bucket.mtime_equal_then_hash
        summary.mtime_unequal_then_hash += bucket.mtime_unequal_then_hash
    return bucket


class ModTimingGuard:
    """Close the previous folder timing before opening the next (handles continue)."""

    def __init__(self, *, reason: str = "reconcile") -> None:
        self.reason = reason
        self._open = False

    def begin(self, folder: str, *, internal_id: str = "") -> None:
        self.close()
        push_mod_timing(folder=folder, reason=self.reason, internal_id=internal_id)
        self._open = True

    def close(self) -> None:
        if not self._open:
            return
        # Nested push from sync_after_metadata_change may still be open.
        while True:
            bucket = current_bucket()
            if bucket is None:
                break
            pop_mod_timing()
        self._open = False

    def __enter__(self) -> ModTimingGuard:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def finish_reconcile_session() -> ReconcileSummary:
    st = _state()
    summary = st.get("session")
    if not isinstance(summary, ReconcileSummary):
        summary = ReconcileSummary()
    if st.get("session_t0"):
        summary.total_ms = (time.perf_counter() - float(st["session_t0"])) * 1000.0
    totals = sorted(float(m.total_ms) for m in summary.per_mod)
    summary.p50_mod_ms = _percentile(totals, 50)
    summary.p95_mod_ms = _percentile(totals, 95)
    summary.max_mod_ms = float(totals[-1]) if totals else 0.0
    ranked = sorted(summary.per_mod, key=lambda m: m.total_ms, reverse=True)[:8]
    summary.top_slow_mods = [
        {
            "internal_id": m.internal_id,
            "folder": m.folder,
            "total_ms": round(m.total_ms, 1),
            "hash_ms": round(m.hash_ms, 1),
            "copy_ms": round(m.backup_copy_ms, 1),
            "payload_walk_ms": round(m.payload_walk_ms, 1),
            "payload_files": m.payload_files,
            "persist_ms": round(m.persist_ms, 1),
            "identity_ms": round(m.identity_ms, 1),
            "changed": m.changed,
        }
        for m in ranked
    ]
    logger.info(
        "[RECONCILE_SUMMARY] mods_seen=%s mods_changed=%s mods_unchanged=%s "
        "backups_started=%s backups_skipped=%s total_ms=%.1f discover_ms=%.1f "
        "scan_ms=%.1f compare_ms=%.1f hash_ms=%.1f hash_files=%s hash_bytes=%s "
        "manifest_ms=%.1f copy_ms=%.1f copy_files=%s copy_bytes=%s persist_ms=%.1f "
        "payload_walk_ms=%.1f payload_files=%s identity_ms=%.1f "
        "p50_mod_ms=%.1f p95_mod_ms=%.1f max_mod_ms=%.1f "
        "size_match_then_hash=%s size_mismatch_skips=%s "
        "mtime_equal_then_hash=%s mtime_unequal_then_hash=%s",
        summary.mods_seen,
        summary.mods_changed,
        summary.mods_unchanged,
        summary.backups_started,
        summary.backups_skipped,
        summary.total_ms,
        summary.discover_ms,
        summary.scan_ms,
        summary.compare_ms,
        summary.hash_ms,
        summary.hash_files,
        summary.hash_bytes,
        summary.manifest_ms,
        summary.copy_ms,
        summary.copy_files,
        summary.copy_bytes,
        summary.persist_ms,
        summary.payload_walk_ms,
        summary.payload_files,
        summary.identity_ms,
        summary.p50_mod_ms,
        summary.p95_mod_ms,
        summary.max_mod_ms,
        summary.size_match_then_hash,
        summary.size_mismatch_skips,
        summary.mtime_equal_then_hash,
        summary.mtime_unequal_then_hash,
    )
    logger.info("[RECONCILE_TOP] %s", json.dumps(summary.top_slow_mods, ensure_ascii=False))
    st["session"] = None
    st["session_t0"] = 0.0
    st["bucket"] = None
    return summary
