"""Sub-operation counters for one DeployTimingSession.

Minimal intrusion: callers record elapsed milliseconds. Path-hit maps stay
in-process and are summarized (not dumped per file) into timing JSON.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

_COPY_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("lt_4kb", 0, 4 * 1024),
    ("4kb_64kb", 4 * 1024, 64 * 1024),
    ("64kb_1mb", 64 * 1024, 1024 * 1024),
    ("1mb_10mb", 1024 * 1024, 10 * 1024 * 1024),
    ("gt_10mb", 10 * 1024 * 1024, None),
)


_VOLUME_CACHE: dict[str, str] = {}


def volume_id(path: Path | str) -> str:
    raw = str(path or "")
    if not raw:
        return ""
    key = raw[:3].upper() if len(raw) >= 2 and raw[1] == ":" else raw
    cached = _VOLUME_CACHE.get(key)
    if cached is not None:
        return cached
    value = ""
    if os.name == "nt":
        try:
            import ctypes

            GetVolumePathNameW = ctypes.windll.kernel32.GetVolumePathNameW
            buf = ctypes.create_unicode_buffer(520)
            if GetVolumePathNameW(raw, buf, len(buf)):
                value = buf.value.rstrip("\\/") or buf.value
        except Exception:  # noqa: BLE001
            value = ""
    if not value:
        try:
            p = Path(raw)
            drive = str(p.drive or p.anchor or "")
            value = drive.rstrip("\\/") if drive else drive
        except OSError:
            value = ""
    _VOLUME_CACHE[key] = value
    return value


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    idx = int(round((p / 100.0) * (len(sorted_vals) - 1)))
    idx = max(0, min(len(sorted_vals) - 1, idx))
    return float(sorted_vals[idx])


def _bucket_name(size: int) -> str:
    for name, lo, hi in _COPY_BUCKETS:
        if hi is None:
            if size >= lo:
                return name
        elif lo <= size < hi:
            return name
    return "gt_10mb"


@dataclass
class OpAgg:
    count: int = 0
    total_ms: float = 0.0
    largest_single_ms: float = 0.0
    bytes: int = 0


@dataclass
class TreeVisit:
    stage: str
    kind: str
    root: str
    file_count: int = 0
    directory_count: int = 0
    bytes: int = 0


@dataclass
class CopyFileTrace:
    relative: str
    size: int
    elapsed_ms: float
    read_write_ms: float = 0.0
    hash_ms: float = 0.0
    copystat_ms: float = 0.0
    mkdir_ms: float = 0.0
    stat_ms: float = 0.0
    source_volume: str = ""
    target_volume: str = ""
    source: str = ""
    target: str = ""
    copyfile: bool = False
    copy2: bool = False
    hashed: bool = False
    copystat: bool = False
    flush: bool = False
    fsync: bool = False
    lstat: bool = False
    chmod: bool = False


@dataclass
class DeployOpProfile:
    ops: dict[str, OpAgg] = field(default_factory=lambda: defaultdict(OpAgg))
    path_hits: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int))
    )
    trees: list[TreeVisit] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    last_stage_end: float = 0.0
    last_stage_name: str = ""
    resolve_cache: dict[str, str] = field(default_factory=dict)
    mkdir_cache: set[str] = field(default_factory=set)
    copy_files: list[CopyFileTrace] = field(default_factory=list)

    def add(
        self,
        op: str,
        ms: float,
        *,
        bytes_count: int = 0,
        path: str = "",
        count: int = 1,
    ) -> None:
        if not op or count <= 0:
            return
        agg = self.ops[op]
        agg.count += int(count)
        agg.total_ms += float(ms)
        if ms > agg.largest_single_ms:
            agg.largest_single_ms = float(ms)
        if bytes_count:
            agg.bytes += int(bytes_count)
        if path:
            hits = self.path_hits[op]
            if op not in {"copyfile", "hash", "stat", "copystat", "lstat"} and len(hits) < 20000:
                hits[path] += int(count)

    def note_tree(
        self,
        *,
        stage: str,
        kind: str,
        root: str,
        file_count: int = 0,
        directory_count: int = 0,
        bytes_count: int = 0,
    ) -> None:
        self.trees.append(
            TreeVisit(
                stage=str(stage or ""),
                kind=str(kind or ""),
                root=str(root or ""),
                file_count=int(file_count or 0),
                directory_count=int(directory_count or 0),
                bytes=int(bytes_count or 0),
            )
        )

    def note_gap(self, *, after: str, before: str, ms: float) -> None:
        self.gaps.append(
            {
                "after": after,
                "before": before,
                "ms": round(float(ms), 3),
            }
        )

    def note_copy_file(self, rec: CopyFileTrace) -> None:
        self.copy_files.append(rec)

    def copy_profile_dict(self) -> dict[str, Any]:
        rows = list(self.copy_files)
        n = len(rows)
        total_bytes = sum(int(r.size) for r in rows)
        times = sorted(float(r.elapsed_ms) for r in rows)
        total_ms = float(sum(times))
        buckets: dict[str, dict[str, float | int]] = {
            name: {"count": 0, "bytes": 0, "total_ms": 0.0}
            for name, _lo, _hi in _COPY_BUCKETS
        }
        volumes: dict[str, int] = defaultdict(int)
        for rec in rows:
            b = buckets[_bucket_name(int(rec.size))]
            b["count"] = int(b["count"]) + 1
            b["bytes"] = int(b["bytes"]) + int(rec.size)
            b["total_ms"] = float(b["total_ms"]) + float(rec.elapsed_ms)
            pair = f"{rec.source_volume}->{rec.target_volume}"
            volumes[pair] += 1
        top = sorted(rows, key=lambda r: r.elapsed_ms, reverse=True)[:20]
        return {
            "copy_count": n,
            "total_bytes": total_bytes,
            "total_ms": round(total_ms, 3),
            "avg_ms": round((total_ms / n) if n else 0.0, 3),
            "p50_ms": round(_percentile(times, 50), 3),
            "p95_ms": round(_percentile(times, 95), 3),
            "p99_ms": round(_percentile(times, 99), 3),
            "max_ms": round(times[-1] if times else 0.0, 3),
            "read_write_ms": round(sum(r.read_write_ms for r in rows), 3),
            "hash_ms": round(sum(r.hash_ms for r in rows), 3),
            "copystat_ms": round(sum(r.copystat_ms for r in rows), 3),
            "mkdir_ms": round(sum(r.mkdir_ms for r in rows), 3),
            "stat_ms": round(sum(r.stat_ms for r in rows), 3),
            "copyfile_count": sum(1 for r in rows if r.copyfile),
            "copy2_count": sum(1 for r in rows if r.copy2),
            "hashed_count": sum(1 for r in rows if r.hashed),
            "copystat_count": sum(1 for r in rows if r.copystat),
            "flush_count": sum(1 for r in rows if r.flush),
            "fsync_count": sum(1 for r in rows if r.fsync),
            "lstat_count": sum(1 for r in rows if r.lstat),
            "chmod_count": sum(1 for r in rows if r.chmod),
            "cross_volume": any(
                r.source_volume and r.target_volume and r.source_volume != r.target_volume
                for r in rows
            ),
            "volume_pairs": dict(volumes),
            "buckets": {
                name: {
                    "count": int(b["count"]),
                    "bytes": int(b["bytes"]),
                    "total_ms": round(float(b["total_ms"]), 3),
                }
                for name, b in buckets.items()
            },
            "top20": [
                {
                    "path": r.relative,
                    "size": int(r.size),
                    "copy_ms": round(float(r.elapsed_ms), 3),
                    "ms_per_MB": round(
                        (float(r.elapsed_ms) / (int(r.size) / (1024 * 1024)))
                        if r.size
                        else 0.0,
                        3,
                    ),
                    "read_write_ms": round(float(r.read_write_ms), 3),
                    "hash_ms": round(float(r.hash_ms), 3),
                    "copystat_ms": round(float(r.copystat_ms), 3),
                    "source_volume": r.source_volume,
                    "target_volume": r.target_volume,
                }
                for r in top
            ],
        }

    def _path_summary(self, op: str) -> dict[str, Any]:
        hits = self.path_hits.get(op) or {}
        if not hits:
            return {}
        max_hits = max(hits.values()) if hits else 0
        dup = sum(1 for n in hits.values() if n > 1)
        return {
            "unique_paths": len(hits),
            "max_hits_one_path": max_hits,
            "paths_hit_more_than_once": dup,
        }

    def as_dict(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for op in sorted(self.ops):
            agg = self.ops[op]
            avg = (agg.total_ms / agg.count) if agg.count else 0.0
            row: dict[str, Any] = {
                "operation": op,
                "count": agg.count,
                "total_ms": round(agg.total_ms, 3),
                "avg_ms": round(avg, 3),
                "largest_single_ms": round(agg.largest_single_ms, 3),
            }
            if agg.bytes:
                row["bytes"] = agg.bytes
            summary = self._path_summary(op)
            if summary:
                row.update(summary)
            rows.append(row)
        gap_total = sum(float(g.get("ms") or 0.0) for g in self.gaps)
        out: dict[str, Any] = {
            "operations": rows,
            "trees": [
                {
                    "stage": t.stage,
                    "kind": t.kind,
                    "root": t.root,
                    "file_count": t.file_count,
                    "directory_count": t.directory_count,
                    "bytes": t.bytes,
                }
                for t in self.trees
            ],
            "inter_stage_gaps": self.gaps,
            "inter_stage_gap_ms": round(gap_total, 3),
            "copy_profile": self.copy_profile_dict() if self.copy_files else {},
        }
        return out


def current_op_profile() -> DeployOpProfile | None:
    from services.deploy_stage_log import current_deploy_timing

    sess = current_deploy_timing()
    if sess is None:
        return None
    return sess.ops


def cached_resolve(path: Path | str) -> str:
    """Path.resolve with a per-deploy cache. Same input string is resolved once."""
    raw = str(path)
    prof = current_op_profile()
    if prof is not None:
        hit = prof.resolve_cache.get(raw)
        if hit is not None:
            return hit
    t0 = time.perf_counter()
    try:
        out = str(Path(path).expanduser().resolve())
    except OSError:
        out = str(Path(path))
    ms = (time.perf_counter() - t0) * 1000.0
    if prof is not None:
        prof.resolve_cache[raw] = out
        if out != raw:
            prof.resolve_cache.setdefault(out, out)
        prof.add("resolve", ms, path=raw)
    return out


def ensure_dir(path: Path | str) -> float:
    """mkdir(parents=True) once per directory during a deploy session.

    Returns elapsed milliseconds for this call (0 when cached).
    """
    dest = Path(path)
    key = str(dest)
    prof = current_op_profile()
    if prof is not None and key in prof.mkdir_cache:
        return 0.0
    t0 = time.perf_counter()
    dest.mkdir(parents=True, exist_ok=True)
    ms = (time.perf_counter() - t0) * 1000.0
    if prof is not None:
        prof.mkdir_cache.add(key)
        prof.add("mkdir", ms, path=key)
    return ms


def record_op(
    op: str,
    ms: float,
    *,
    bytes_count: int = 0,
    path: str = "",
    count: int = 1,
) -> None:
    prof = current_op_profile()
    if prof is None:
        return
    prof.add(op, ms, bytes_count=bytes_count, path=path, count=count)


def note_copy_file(rec: CopyFileTrace) -> None:
    prof = current_op_profile()
    if prof is None:
        return
    prof.note_copy_file(rec)


def note_tree(**kwargs: Any) -> None:
    prof = current_op_profile()
    if prof is None:
        return
    prof.note_tree(**kwargs)


@contextmanager
def timed_op(
    op: str,
    *,
    path: str = "",
    bytes_count: int = 0,
    count: int = 1,
) -> Iterator[None]:
    t0 = time.perf_counter()
    try:
        yield
    finally:
        record_op(
            op,
            (time.perf_counter() - t0) * 1000.0,
            bytes_count=bytes_count,
            path=path,
            count=count,
        )
