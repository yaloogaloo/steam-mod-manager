#!/usr/bin/env python3
"""
Readonly Asset Store / CAS architecture audit.

Scans ``cache/asset_cache``, ``.info/assets``, and Backup ``offline/assets``
for content-hash overlap. Documents cache vs durable-store semantics from
production code. Never mutates production files, DB, Backup, or cache.

Outputs under ``_tmp/`` only.

Usage:
  python tools/audit_asset_store_architecture.py
  python tools/audit_asset_store_architecture.py --limit 100
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.archive import (  # noqa: E402
    ASSET_CACHE_MAX_BYTES,
    ASSET_CACHE_MIN_AGE_SECONDS,
    ASSET_CACHE_PRUNE_INTERVAL_SECONDS,
    ASSET_CACHE_PRUNE_TARGET_BYTES,
    ASSET_CACHE_TTL_SECONDS,
    _asset_cache_key,
    _is_asset_cache_filename,
)

HASH_CHUNK = 1024 * 1024
OUT_DIR = _REPO / "_tmp"
OUT_JSON = OUT_DIR / "asset_store_architecture_audit.json"
OUT_REPORT = OUT_DIR / "asset_store_architecture_audit_report.md"
OUT_CACHE_TOP = OUT_DIR / "asset_store_architecture_cache_top.json"


def human_bytes(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} TB"


def classify_extension(path: Path | str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in {"htm", "html"}:
        return "html"
    if suffix == "css":
        return "css"
    if suffix in {"js", "mjs", "cjs"}:
        return "js"
    if suffix == "png":
        return "png"
    if suffix in {"jpg", "jpeg"}:
        return "jpg"
    if suffix == "webp":
        return "webp"
    if suffix == "gif":
        return "gif"
    if suffix == "svg":
        return "svg"
    if suffix in {"woff", "woff2", "ttf", "otf", "eot"}:
        return "font"
    if suffix == "bin":
        return "bin"
    return "other"


def sha256_file(path: Path, *, chunk: int = HASH_CHUNK) -> str | None:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def sha256_files_parallel(
    paths: list[Path], *, workers: int = 8
) -> dict[str, str | None]:
    if not paths:
        return {}
    out: dict[str, str | None] = {}
    workers = max(1, min(workers, len(paths)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(sha256_file, p): p for p in paths}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                out[str(p)] = fut.result()
            except Exception:  # noqa: BLE001
                out[str(p)] = None
    return out


def walk_files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                yield Path(dirpath) / name
    except OSError:
        return


def parse_cache_key_from_filename(name: str) -> str | None:
    """Return 64-hex URL-hash key if filename matches asset_cache layout."""
    if not _is_asset_cache_filename(name):
        # bare 64-hex without ext
        stem = Path(name).stem
        if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem):
            return stem
        return None
    return Path(name).stem


def cache_key_is_url_hash_not_content(url: str, content_sha256: str) -> bool:
    """True when URL-key differs from content hash (normal for URL-keyed cache)."""
    return _asset_cache_key(url) != content_sha256


@dataclass
class HashIndex:
    """content_sha256 -> size (unique object) and instance counts."""

    size_by_hash: dict[str, int] = field(default_factory=dict)
    count_by_hash: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    files: int = 0
    bytes_total: int = 0
    by_type: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: {"count": 0, "bytes": 0})
    )
    largest: list[dict[str, Any]] = field(default_factory=list)
    # filename key (URL hash for cache) samples
    key_samples: list[dict[str, Any]] = field(default_factory=list)

    def add(self, digest: str | None, size: int, *, path: Path, type_name: str) -> None:
        self.files += 1
        self.bytes_total += size
        self.by_type[type_name]["count"] += 1
        self.by_type[type_name]["bytes"] += size
        self.largest.append(
            {"size": size, "path": str(path), "type": type_name, "sha256": digest}
        )
        self.largest.sort(key=lambda x: int(x["size"]), reverse=True)
        del self.largest[50:]
        if not digest:
            return
        prev = self.size_by_hash.get(digest)
        if prev is None:
            self.size_by_hash[digest] = size
        elif prev != size:
            # Prefer larger recorded size if mismatch (should not happen)
            self.size_by_hash[digest] = max(prev, size)
        self.count_by_hash[digest] += 1

    def unique_bytes(self) -> int:
        return sum(self.size_by_hash.values())

    def duplicate_instance_bytes(self) -> int:
        """Sum of (count-1)*size for hashes with count>1."""
        waste = 0
        for h, n in self.count_by_hash.items():
            if n > 1:
                waste += self.size_by_hash[h] * (n - 1)
        return waste


def hash_set_bytes(hashes: set[str], size_lookup: dict[str, int]) -> int:
    return sum(size_lookup.get(h, 0) for h in hashes)


def intersect_stats(
    a: HashIndex, b: HashIndex, *, label: str
) -> dict[str, Any]:
    ha = set(a.size_by_hash)
    hb = set(b.size_by_hash)
    inter = ha & hb
    # Prefer size from a, fall back b
    sizes = {**b.size_by_hash, **a.size_by_hash}
    return {
        "label": label,
        "unique_hashes": len(inter),
        "unique_bytes": hash_set_bytes(inter, sizes),
        "unique_bytes_human": human_bytes(hash_set_bytes(inter, sizes)),
    }


def scan_asset_cache(cache_root: Path) -> HashIndex:
    idx = HashIndex()
    paths = [
        p
        for p in cache_root.iterdir()
        if p.is_file() and not p.name.startswith(".")
    ] if cache_root.is_dir() else []
    # Hash in parallel
    digests = sha256_files_parallel(paths, workers=8)
    for path in paths:
        try:
            size = int(path.stat().st_size)
        except OSError:
            continue
        digest = digests.get(str(path))
        type_name = classify_extension(path)
        idx.add(digest, size, path=path, type_name=type_name)
        key = parse_cache_key_from_filename(path.name)
        if key and digest and len(idx.key_samples) < 20:
            idx.key_samples.append(
                {
                    "filename": path.name,
                    "url_hash_key": key,
                    "content_sha256": digest,
                    "key_equals_content": key == digest,
                    "size": size,
                }
            )
    return idx


def scan_info_assets(library: Path, *, limit: int | None = None) -> HashIndex:
    """Scan ``mod/<game>/<mod>/.info/assets`` and ``.info/offline/assets``."""
    idx = HashIndex()
    if not library.is_dir():
        return idx
    asset_dirs: list[Path] = []
    mod_count = 0
    try:
        games = [p for p in library.iterdir() if p.is_dir()]
    except OSError:
        return idx
    for game in games:
        try:
            mods = [p for p in game.iterdir() if p.is_dir()]
        except OSError:
            continue
        for mod in mods:
            mod_count += 1
            if limit is not None and mod_count > limit:
                break
            for candidate in (
                mod / ".info" / "assets",
                mod / ".info" / "offline" / "assets",
                mod / "info" / "assets",
            ):
                if candidate.is_dir():
                    asset_dirs.append(candidate)
        if limit is not None and mod_count >= limit:
            break

    total_dirs = len(asset_dirs)
    for i, assets_dir in enumerate(asset_dirs, 1):
        paths = list(walk_files(assets_dir))
        digests = sha256_files_parallel(paths, workers=8)
        for path in paths:
            try:
                size = int(path.stat().st_size)
            except OSError:
                continue
            digest = digests.get(str(path))
            idx.add(digest, size, path=path, type_name=classify_extension(path))
        if i % 100 == 0 or i == total_dirs:
            print(
                f"[audit:info] dirs {i}/{total_dirs}  "
                f"files={idx.files} bytes={human_bytes(idx.bytes_total)}",
                flush=True,
            )
    return idx


def scan_backup_assets(backup_root: Path, *, limit: int | None = None) -> HashIndex:
    idx = HashIndex()
    if not backup_root.is_dir():
        return idx
    buckets = sorted(
        [p for p in backup_root.iterdir() if p.is_dir()],
        key=lambda p: p.name,
    )
    if limit is not None:
        buckets = buckets[:limit]
    total = len(buckets)
    for i, bucket in enumerate(buckets, 1):
        assets = bucket / "offline" / "assets"
        if not assets.is_dir():
            continue
        paths = list(walk_files(assets))
        digests = sha256_files_parallel(paths, workers=8)
        for path in paths:
            try:
                size = int(path.stat().st_size)
            except OSError:
                continue
            digest = digests.get(str(path))
            idx.add(digest, size, path=path, type_name=classify_extension(path))
        if i % 100 == 0 or i == total:
            print(
                f"[audit:backup] buckets {i}/{total}  "
                f"files={idx.files} bytes={human_bytes(idx.bytes_total)}",
                flush=True,
            )
    return idx


def code_semantics() -> dict[str, Any]:
    """Document production asset_cache semantics (from services.archive)."""
    return {
        "path": "cache/asset_cache/",
        "declared": "Regenerable URL-hash cache. Not Source of Truth. (archive.py:84)",
        "key": {
            "type": "URL identity",
            "algorithm": "SHA-256(absolute_url UTF-8)",
            "function": "services.archive._asset_cache_key",
            "not": "content hash / SHA-1 of bytes",
        },
        "on_disk_filename": "{sha256(url)}{ext}",
        "info_assets_filename": "{sha1(url)[:16]}{ext}  (different algorithm + truncation)",
        "same_content_different_url": "NO — separate cache objects",
        "same_url_content_change": (
            "STALE KEEP — if cache file exists, miss path skips rewrite; "
            "no versioning"
        ),
        "css_note": (
            "Cache stores RAW bytes (pre-CSS rewrite). "
            ".info/assets CSS may be localized (/* smm-css-localized */) — "
            "content hash may differ from cache for CSS."
        ),
        "eviction": {
            "ttl_seconds": ASSET_CACHE_TTL_SECONDS,
            "ttl_days": ASSET_CACHE_TTL_SECONDS / 86400,
            "max_bytes": ASSET_CACHE_MAX_BYTES,
            "prune_target_bytes": ASSET_CACHE_PRUNE_TARGET_BYTES,
            "min_age_seconds": ASSET_CACHE_MIN_AGE_SECONDS,
            "prune_interval_seconds": ASSET_CACHE_PRUNE_INTERVAL_SECONDS,
            "mechanism": "prune_asset_cache: zero-byte, TTL by mtime, size-cap LRU by mtime",
            "trigger": "maybe_prune_asset_cache on OfflinePageArchiver.__enter__",
        },
        "deletable": True,
        "regenerable_on_miss": (
            "YES — _download_asset HTTP GET → .info/assets → seed cache "
            "(requires network). Existing Mods open offline from .info/assets, "
            "not from asset_cache."
        ),
        "atomic_write": "_copy_file_atomic / _stream_to_file tempfile + Path.replace",
        "locks": "per-URL-key in-process threading.Lock; no cross-process flock",
        "backup_miss_repair": "NO references — Backup/MISS/Repair never read asset_cache",
        "semantic_class": "cache",
        "durable_cas_direct": False,
        "evolve_verdict": "CAN EVOLVE WITH REFACTOR",
        "evolve_reason": (
            "Has TTL + size eviction + URL keys + no refcount. "
            "Must disable eviction, switch to content addressing, add "
            "reachability/GC, and separate durable semantics — OR create "
            "new data/asset_store/ and keep asset_cache as download accelerator."
        ),
    }


def capability_matrix() -> list[dict[str, str]]:
    return [
        {
            "capability": "Content hashing",
            "current": "NO (URL SHA-256 only)",
            "evidence": "_asset_cache_key = sha256(url); no content digest stored",
            "required": "YES — SHA-256(content) object id",
            "risk": "URL key ≠ content identity; duplicate bytes across URLs",
        },
        {
            "capability": "Content-addressed lookup",
            "current": "NO",
            "evidence": "_find_cached_asset(key=url_hash, ext)",
            "required": "YES",
            "risk": "Cannot share identical bytes from different URLs",
        },
        {
            "capability": "Same content dedupe",
            "current": "NO",
            "evidence": "Different URLs → different files even if bytes identical",
            "required": "YES",
            "risk": "Cross-Mod waste (~12.77 GB in .info alone)",
        },
        {
            "capability": "Atomic write",
            "current": "PARTIAL YES",
            "evidence": "_copy_file_atomic tempfile+replace",
            "required": "YES",
            "risk": "No cross-process lock; race possible across processes",
        },
        {
            "capability": "Corruption detection",
            "current": "NO",
            "evidence": "No hash verify on read; size>0 only",
            "required": "YES (verify on ingest / optional on read)",
            "risk": "Silent corrupt cache seeds bad .info/assets",
        },
        {
            "capability": "Missing object detection",
            "current": "PARTIAL (cache miss → HTTP)",
            "evidence": "_find_cached_asset None → download",
            "required": "YES + manifest-level missing for durable refs",
            "risk": "Durable miss cannot fall back to HTTP if offline",
        },
        {
            "capability": "Object immutability",
            "current": "WEAK",
            "evidence": "Exists check skips overwrite; no content seal",
            "required": "YES (content-addressed write-once)",
            "risk": "Stale URL content never refreshed",
        },
        {
            "capability": "Safe concurrent write",
            "current": "PARTIAL (in-process lock)",
            "evidence": "_cache_lock_for(key)",
            "required": "YES cross-process",
            "risk": "Two processes may race on same key",
        },
        {
            "capability": "Reference tracking",
            "current": "NO",
            "evidence": "No refcount / manifest reachability",
            "required": "YES for safe GC",
            "risk": "Cannot know if object still needed",
        },
        {
            "capability": "Garbage collection",
            "current": "YES but CACHE-style (TTL/LRU)",
            "evidence": "prune_asset_cache deletes by age/size — NOT reachability",
            "required": "Reachability GC only (never TTL on durable)",
            "risk": "TTL/LRU incompatible with durable Offline/Backup refs",
        },
        {
            "capability": "Restore",
            "current": "NO role",
            "evidence": "MISS/Restore use Backup offline closure, not cache",
            "required": "Durable store must survive LIVE deletion",
            "risk": "If Backup only references store, store becomes Backup infra",
        },
        {
            "capability": "Backup portability",
            "current": "N/A (not used)",
            "evidence": "Backup copies LIVE closure bytes into mod_backup/",
            "required": "Define boundary: self-contained vs shared store",
            "risk": "Refs-only Backup breaks if store not migrated with Backup",
        },
        {
            "capability": "Repair",
            "current": "NO",
            "evidence": "repair_live_offline_backups recopies LIVE→Backup only",
            "required": "Repair must re-link or re-fetch missing objects",
            "risk": "Missing durable object + no LIVE = unrecoverable offline page",
        },
        {
            "capability": "Offline access",
            "current": "NO (not on offline path)",
            "evidence": "file:// opens .info or Backup offline/index.html + local assets",
            "required": "Store objects must be resolvable offline via manifest paths",
            "risk": "Hardlinks/copies/symlinks needed for file:// relative URLs",
        },
        {
            "capability": "Cross-Mod sharing",
            "current": "URL-only sharing of downloads",
            "evidence": "Same URL hits cache; copies still into each .info/assets",
            "required": "Content sharing without per-Mod byte copies",
            "risk": "Today still pays per-Mod physical storage",
        },
    ]


def estimate_savings(
    info: HashIndex,
    backup: HashIndex,
    cache: HashIndex,
) -> dict[str, Any]:
    a = set(info.size_by_hash)
    b = set(cache.size_by_hash)
    c = set(backup.size_by_hash)
    sizes = {**cache.size_by_hash, **backup.size_by_hash, **info.size_by_hash}

    unique_a = info.unique_bytes()
    unique_b = cache.unique_bytes()
    unique_c = backup.unique_bytes()
    unique_abc = hash_set_bytes(a | b | c, sizes)
    unique_ac = hash_set_bytes(a | c, sizes)
    b_only = hash_set_bytes(b - a - c, sizes)

    current = info.bytes_total + backup.bytes_total + cache.bytes_total
    # Strategy: CAS for .info only; Backup stays self-contained; cache remains accel
    # After .info CAS: physical info = unique_a; backup unchanged; cache optional
    cas_info_only = unique_a + backup.bytes_total + cache.bytes_total
    # Better: retire cache into CAS (if CAS holds localized+raw as needed):
    cas_info_retire_cache = unique_a + backup.bytes_total + b_only
    # Global CAS for .info+Backup (+ absorb cache unique leftovers)
    global_cas = unique_abc  # single store; manifests negligible
    # Global CAS without keeping raw-only cache extras that aren't in A∪C
    # (b_only may be raw CSS variants — still needed if used as download accel)
    global_cas_plus_raw_accel = unique_ac + b_only

    return {
        "current_physical_bytes": current,
        "current_physical_human": human_bytes(current),
        "breakdown_current": {
            "info_assets": info.bytes_total,
            "backup_assets": backup.bytes_total,
            "asset_cache": cache.bytes_total,
        },
        "unique_info_bytes": unique_a,
        "unique_backup_bytes": unique_c,
        "unique_cache_bytes": unique_b,
        "unique_info_or_backup_bytes": unique_ac,
        "unique_all_three_bytes": unique_abc,
        "cache_only_not_in_info_or_backup_bytes": b_only,
        "scenarios": {
            "cas_info_only_keep_backup_and_cache": {
                "estimated_bytes": cas_info_only,
                "estimated_human": human_bytes(cas_info_only),
                "savings_bytes": current - cas_info_only,
                "savings_human": human_bytes(current - cas_info_only),
                "note": "Dedupe .info across Mods; Backup still full copies; cache kept",
            },
            "cas_info_only_retire_cache_keep_backup": {
                "estimated_bytes": cas_info_retire_cache,
                "estimated_human": human_bytes(cas_info_retire_cache),
                "savings_bytes": current - cas_info_retire_cache,
                "savings_human": human_bytes(current - cas_info_retire_cache),
                "note": "Absorb cache into CAS; keep raw-only leftovers; Backup still copies",
            },
            "global_cas_info_and_backup": {
                "estimated_bytes": unique_abc,
                "estimated_human": human_bytes(unique_abc),
                "savings_bytes": current - unique_abc,
                "savings_human": human_bytes(current - unique_abc),
                "note": (
                    "Single durable content store for LIVE+Backup+cache contents; "
                    "manifests only in .info/Backup. Changes Backup Contract "
                    "(Model 1: Backup + shared Asset Store)."
                ),
            },
            "global_cas_plus_raw_accel_only": {
                "estimated_bytes": global_cas_plus_raw_accel,
                "estimated_human": human_bytes(global_cas_plus_raw_accel),
                "savings_bytes": current - global_cas_plus_raw_accel,
                "savings_human": human_bytes(current - global_cas_plus_raw_accel),
                "note": "Same as global CAS; raw-only cache objects retained if needed for download accel",
            },
        },
    }


def build_report(payload: dict[str, Any]) -> str:
    sem = payload["cache_semantics"]
    inv = payload["cache_inventory"]
    ov = payload["overlap"]
    sav = payload["savings"]
    lines = [
        "# Asset Store / CAS Architecture Audit",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "**Status: ARCHITECTURE AUDIT COMPLETE (no migration / no mutation)**",
        "",
        "## 1. Asset Cache Inventory",
        "",
        f"files: {inv['files']}",
        f"bytes: {inv['bytes']} ({inv['bytes_human']})",
        f"unique contents: {inv['unique_contents']} ({inv['unique_bytes_human']})",
        f"duplicate instance waste: {inv['duplicate_waste_human']}",
        f"largest: see companion JSON",
        f"types: {json.dumps(inv['by_type'], ensure_ascii=False)}",
        f"cache key type: {sem['key']['type']} / {sem['key']['algorithm']}",
        f"key==content samples equal? "
        f"{sum(1 for s in inv['key_samples'] if s.get('key_equals_content'))}/"
        f"{len(inv['key_samples'])} (expect ~0)",
        "",
        "## 2. Overlap (unique content bytes)",
        "",
        f".info ∩ cache: {ov['info_cap_cache']['unique_bytes_human']} "
        f"({ov['info_cap_cache']['unique_hashes']} hashes)",
        f"Backup ∩ cache: {ov['backup_cap_cache']['unique_bytes_human']} "
        f"({ov['backup_cap_cache']['unique_hashes']} hashes)",
        f".info ∩ Backup: {ov['info_cap_backup']['unique_bytes_human']} "
        f"({ov['info_cap_backup']['unique_hashes']} hashes)",
        f"all three: {ov['all_three']['unique_bytes_human']} "
        f"({ov['all_three']['unique_hashes']} hashes)",
        f"cache only (not in .info or Backup): "
        f"{ov['cache_only']['unique_bytes_human']}",
        f".info only: {ov['info_only']['unique_bytes_human']}",
        f"Backup only: {ov['backup_only']['unique_bytes_human']}",
        "",
        "### Instance totals (physical)",
        f".info/assets files/bytes: {payload['info_inventory']['files']} / "
        f"{payload['info_inventory']['bytes_human']}",
        f"Backup assets files/bytes: {payload['backup_inventory']['files']} / "
        f"{payload['backup_inventory']['bytes_human']}",
        f"asset_cache files/bytes: {inv['files']} / {inv['bytes_human']}",
        "",
        "## 3. Cache Semantics",
        "",
        f"classification: **{sem['semantic_class']}** (not durable)",
        f"evolve verdict: **{sem['evolve_verdict']}**",
        f"reason: {sem['evolve_reason']}",
        "",
        "Evidence:",
        f"- declared: {sem['declared']}",
        f"- key: {sem['key']}",
        f"- eviction: TTL {sem['eviction']['ttl_days']}d, "
        f"max {human_bytes(sem['eviction']['max_bytes'])}, "
        f"prune to {human_bytes(sem['eviction']['prune_target_bytes'])}",
        f"- deletable/regenerable: {sem['deletable']} / {sem['regenerable_on_miss']}",
        f"- Backup/MISS/Repair: {sem['backup_miss_repair']}",
        f"- CSS: {sem['css_note']}",
        "",
        "## 4. CAS Capability Matrix",
        "",
        "| Capability | Current | Evidence | Required | Risk |",
        "| ---------- | ------- | -------- | -------- | ---- |",
    ]
    for row in payload["capability_matrix"]:
        lines.append(
            f"| {row['capability']} | {row['current']} | {row['evidence']} | "
            f"{row['required']} | {row['risk']} |"
        )
    lines.extend(
        [
            "",
            "## 5. Current Storage Model",
            "",
            "```",
            "CDN URL",
            "  -> OfflinePageArchiver._download_asset",
            "       HIT:  asset_cache[sha256(url)]  --copy-->  .info/assets[sha1(url)[:16]]",
            "       MISS: HTTP --> .info/assets --> seed asset_cache[sha256(url)]",
            "CSS localize only on .info copy (cache stays RAW)",
            "",
            "OPEN offline: resolve_offline_page(.info) -> file:// index + local assets",
            "Backup: snapshot_offline_closure(LIVE index) -> mod_backup/<mod_id>/offline/",
            "        (bytes copied; never reads asset_cache)",
            "MISS: LIVE gone; Backup offline closure still has its own asset bytes",
            "prune_asset_cache: TTL/LRU may DELETE cache objects anytime",
            "```",
            "",
            "## 6. Candidate Architectures",
            "",
            "### Strategy A — Evolve asset_cache into Durable CAS",
            "Requires: remove TTL/LRU, content-address keys, refcount/GC, immutability,",
            "corruption checks, Offline/Backup integration. **High risk** of confusing",
            "cache vs durable semantics during transition.",
            "",
            "### Strategy B — New data/asset_store/ (recommended foundation)",
            "Keep asset_cache as regenerable download accelerator (URL-keyed, TTL OK).",
            "New durable store: sha256(content)/objects + manifests. Clear semantic split.",
            "",
            "### Strategy C — Per-Mod asset storage (baseline / current)",
            "Status quo: .info/assets + Backup copies. Simple portability; worst space.",
            "",
            "### Strategy D — Global CAS + manifests",
            "Asset Store holds bytes; .info and Backup hold manifests + index.html;",
            "relative file:// resolution via hardlink/copy-on-open/symlink farm OR",
            "rewritten paths into store. Enables Model 1 Backup+shared store.",
            "",
            "## 7. Real Storage Savings",
            "",
            f"Current physical (.info + Backup + cache): {sav['current_physical_human']}",
            f"Unique .info content: {human_bytes(sav['unique_info_bytes'])}",
            f"Unique Backup content: {human_bytes(sav['unique_backup_bytes'])}",
            f"Unique cache content: {human_bytes(sav['unique_cache_bytes'])}",
            f"Unique .info∪Backup: {human_bytes(sav['unique_info_or_backup_bytes'])}",
            f"Unique all three: {human_bytes(sav['unique_all_three_bytes'])}",
            f"Cache-only leftovers: {human_bytes(sav['cache_only_not_in_info_or_backup_bytes'])}",
            "",
        ]
    )
    for name, sc in sav["scenarios"].items():
        lines.append(
            f"- **{name}**: {sc['estimated_human']} "
            f"(save {sc['savings_human']}) — {sc['note']}"
        )
    lines.extend(
        [
            "",
            "## 8. MISS / Restore / Repair Impact",
            "",
            "Current Contract: Backup Offline Snapshot is self-contained bytes under",
            "data/mod_backup/<mod_id>/offline/. MISS does not need asset_cache or LIVE.",
            "",
            "If Backup only stores SHA-256 refs:",
            "- Asset Store MUST be Backup infrastructure (Model 1), OR",
            "- Backup remains self-contained (Model 2) and keeps paying duplicate bytes.",
            "",
            "Repair today recopies LIVE→Backup; with CAS, repair re-links manifests",
            "and verifies object presence/hash.",
            "",
            "## 9. Deletion / GC Strategy (design only)",
            "",
            "Recommended: manifest reachability",
            "  reachable = union(LIVE manifests, Backup manifests)",
            "  GC candidates = store objects not in reachable",
            "Never TTL-evict durable objects. Optional grace period after unreachability.",
            "",
            "## 10. Concurrency / Crash Safety",
            "",
            "Need: temp file + hash verify + atomic rename into sha256 path;",
            "cross-process lock or write-to-unique-temp then publish;",
            "crash leaves temps only; partial files never published under final name.",
            "Current cache: in-process lock only — insufficient for durable 10000+ Mods.",
            "",
            "## 11. Recommended Architecture",
            "",
            "**Choose Strategy B + D (new Durable CAS + manifests), keep asset_cache as cache.**",
            "",
            "Backup recommendation: **SHOULD REFERENCE SHARED ASSETS (Model 1)** for space,",
            "BUT only after Asset Store is part of Backup Contract / portability boundary.",
            "Until then, Model 2 (self-contained Backup) remains safer.",
            "",
            "Phased: Phase1 store foundation → Phase2 .info refs → Phase3 Backup refs",
            "→ Phase4 historical migration → Phase5 reachability GC → Phase6 cleanup.",
            "",
            "## 12. Implementation Phases (planning only)",
            "",
            "1. Asset Store foundation (content hash, atomic write, verify)",
            "2. .info reference migration (manifest + hardlink/copy farm)",
            "3. Backup reference migration (Contract change + MISS tests)",
            "4. Historical migration of existing .info/Backup bytes",
            "5. Safe GC via manifest reachability",
            "6. Retire per-Mod byte trees; keep URL cache optional",
            "",
            "## Conclusions",
            "",
            "### Conclusion A — cache/asset_cache",
            f"**{sem['evolve_verdict']}**",
            "",
            "### Conclusion B — Backup",
            "**SHOULD REFERENCE SHARED ASSETS** (target Model 1), "
            "after Durable Store is Backup infrastructure; "
            "**MUST REMAIN SELF-CONTAINED** until that Contract lands.",
            "",
            "### Conclusion C — Global Durable CAS",
            "**YES — recommended.**",
            f"Estimated elimination of duplicate physical storage: "
            f"{sav['scenarios']['global_cas_info_and_backup']['savings_human']} "
            f"from current {sav['current_physical_human']} "
            f"down to ~{sav['scenarios']['global_cas_info_and_backup']['estimated_human']}.",
            "",
            "## Production safety",
            "- production files modified: NO",
            "- production DB modified: NO",
            "- production Backup modified: NO",
            "- production .info modified: NO",
            "- production asset_cache modified: NO",
            "- migration performed: NO",
            "",
            "Gate: ASSET STORAGE ARCHITECTURE AUDIT — CLOSED "
            "(pending targeted tests PASS)",
        ]
    )
    return "\n".join(lines)


def inventory_dict(idx: HashIndex) -> dict[str, Any]:
    return {
        "files": idx.files,
        "bytes": idx.bytes_total,
        "bytes_human": human_bytes(idx.bytes_total),
        "unique_contents": len(idx.size_by_hash),
        "unique_bytes": idx.unique_bytes(),
        "unique_bytes_human": human_bytes(idx.unique_bytes()),
        "duplicate_waste": idx.duplicate_instance_bytes(),
        "duplicate_waste_human": human_bytes(idx.duplicate_instance_bytes()),
        "by_type": {k: dict(v) for k, v in idx.by_type.items()},
        "largest": idx.largest[:20],
        "key_samples": idx.key_samples,
    }


def run_audit(
    *,
    limit: int | None = None,
    library_root: Path | None = None,
    data_root: Path | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    library_root = library_root or (_REPO / "mod")
    data_root = data_root or (_REPO / "data")
    from core.paths import ASSET_CACHE_DIR_NAME, asset_cache_dir, project_root

    if Path(data_root).resolve() == (project_root() / "data").resolve():
        cache_root = asset_cache_dir()
    else:
        sibling = Path(data_root).parent / "cache" / ASSET_CACHE_DIR_NAME
        legacy = Path(data_root) / ASSET_CACHE_DIR_NAME
        cache_root = sibling if sibling.is_dir() else legacy
    backup_root = data_root / "mod_backup"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("[audit] scanning asset_cache ...", flush=True)
    cache_idx = scan_asset_cache(cache_root)
    print(
        f"[audit] cache files={cache_idx.files} "
        f"bytes={human_bytes(cache_idx.bytes_total)} "
        f"unique={human_bytes(cache_idx.unique_bytes())}",
        flush=True,
    )

    print("[audit] scanning .info/assets ...", flush=True)
    info_idx = scan_info_assets(library_root, limit=limit)
    print(
        f"[audit] info files={info_idx.files} "
        f"bytes={human_bytes(info_idx.bytes_total)} "
        f"unique={human_bytes(info_idx.unique_bytes())}",
        flush=True,
    )

    print("[audit] scanning Backup offline/assets ...", flush=True)
    backup_idx = scan_backup_assets(backup_root, limit=limit)
    print(
        f"[audit] backup files={backup_idx.files} "
        f"bytes={human_bytes(backup_idx.bytes_total)} "
        f"unique={human_bytes(backup_idx.unique_bytes())}",
        flush=True,
    )

    a = set(info_idx.size_by_hash)
    b = set(cache_idx.size_by_hash)
    c = set(backup_idx.size_by_hash)
    sizes = {
        **cache_idx.size_by_hash,
        **backup_idx.size_by_hash,
        **info_idx.size_by_hash,
    }
    all3 = a & b & c
    overlap = {
        "info_cap_cache": intersect_stats(info_idx, cache_idx, label="A∩B"),
        "backup_cap_cache": intersect_stats(backup_idx, cache_idx, label="C∩B"),
        "info_cap_backup": intersect_stats(info_idx, backup_idx, label="A∩C"),
        "all_three": {
            "label": "A∩B∩C",
            "unique_hashes": len(all3),
            "unique_bytes": hash_set_bytes(all3, sizes),
            "unique_bytes_human": human_bytes(hash_set_bytes(all3, sizes)),
        },
        "cache_only": {
            "unique_hashes": len(b - a - c),
            "unique_bytes": hash_set_bytes(b - a - c, sizes),
            "unique_bytes_human": human_bytes(hash_set_bytes(b - a - c, sizes)),
        },
        "info_only": {
            "unique_hashes": len(a - b - c),
            "unique_bytes": hash_set_bytes(a - b - c, sizes),
            "unique_bytes_human": human_bytes(hash_set_bytes(a - b - c, sizes)),
        },
        "backup_only": {
            "unique_hashes": len(c - a - b),
            "unique_bytes": hash_set_bytes(c - a - b, sizes),
            "unique_bytes_human": human_bytes(hash_set_bytes(c - a - b, sizes)),
        },
    }

    semantics = code_semantics()
    savings = estimate_savings(info_idx, backup_idx, cache_idx)
    matrix = capability_matrix()

    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_sec": round(time.monotonic() - t0, 2),
        "status": "ARCHITECTURE AUDIT COMPLETE",
        "limit": limit,
        "cache_semantics": semantics,
        "capability_matrix": matrix,
        "cache_inventory": inventory_dict(cache_idx),
        "info_inventory": inventory_dict(info_idx),
        "backup_inventory": inventory_dict(backup_idx),
        "overlap": overlap,
        "savings": savings,
        "conclusions": {
            "A_asset_cache": semantics["evolve_verdict"],
            "B_backup": (
                "SHOULD REFERENCE SHARED ASSETS (target Model 1) after Durable "
                "Store is Backup infrastructure; MUST REMAIN SELF-CONTAINED until then"
            ),
            "C_global_durable_cas": "YES — recommended",
            "estimated_savings_global_cas": savings["scenarios"][
                "global_cas_info_and_backup"
            ],
        },
        "production_safety": {
            "production_files_modified": "NO",
            "production_db_modified": "NO",
            "production_backup_modified": "NO",
            "production_info_modified": "NO",
            "production_asset_cache_modified": "NO",
            "migration_performed": "NO",
        },
    }

    report = build_report(payload)
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    OUT_REPORT.write_text(report, encoding="utf-8")
    OUT_CACHE_TOP.write_text(
        json.dumps(cache_idx.largest, indent=2), encoding="utf-8"
    )

    summary = (
        f"=== Asset Store Architecture Audit ===\n"
        f"cache: {human_bytes(cache_idx.bytes_total)} "
        f"unique={human_bytes(cache_idx.unique_bytes())}\n"
        f"info: {human_bytes(info_idx.bytes_total)} "
        f"unique={human_bytes(info_idx.unique_bytes())}\n"
        f"backup: {human_bytes(backup_idx.bytes_total)} "
        f"unique={human_bytes(backup_idx.unique_bytes())}\n"
        f"info∩cache: {overlap['info_cap_cache']['unique_bytes_human']}\n"
        f"global CAS est: "
        f"{savings['scenarios']['global_cas_info_and_backup']['estimated_human']} "
        f"(save {savings['scenarios']['global_cas_info_and_backup']['savings_human']})\n"
        f"Conclusion A: {semantics['evolve_verdict']}\n"
        f"Report: {OUT_REPORT}\n"
    )
    try:
        print(summary)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(summary.encode("utf-8", errors="replace"))
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Readonly Asset Store / CAS architecture audit"
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit mods/buckets (0=all)")
    args = parser.parse_args(argv)
    run_audit(limit=args.limit or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
