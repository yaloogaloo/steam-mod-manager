#!/usr/bin/env python3
"""
Readonly Offline Web Asset cost audit.

Scans LIVE ``.info`` trees and Backup ``offline/`` snapshots. Never deletes,
moves, renames, compresses, or mutates production files / DB / Backup / ``.info``.

Outputs under ``_tmp/`` only (repeatable; overwrite previous audit artifacts).

Usage:
  python tools/audit_offline_web_assets.py
  python tools/audit_offline_web_assets.py --limit 50   # smoke / subset
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import unquote, urlparse

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.offline.backup_closure import (  # noqa: E402
    collect_offline_closure_report,
    is_external_or_non_file_ref,
)
from services.offline.paths import resolve_offline_page  # noqa: E402

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

HASH_CHUNK = 1024 * 1024
OUT_DIR = _REPO / "_tmp"
OUT_JSON = OUT_DIR / "offline_web_asset_audit.json"
OUT_REPORT = OUT_DIR / "offline_web_asset_audit_report.md"
OUT_TOP_ASSETS = OUT_DIR / "offline_web_asset_audit_top_assets.json"
OUT_TOP_DUPES = OUT_DIR / "offline_web_asset_audit_top_dupes.json"
OUT_TOP_MODS = OUT_DIR / "offline_web_asset_audit_top_mods.json"

INFO_NAMES = (".info", "info")
ASSET_DIR_NAME = "assets"
ENTITY_KEY_NAME = "entity_key"

TYPE_BUCKETS = (
    "html",
    "css",
    "js",
    "png",
    "jpg",
    "webp",
    "avif",
    "gif",
    "svg",
    "ico",
    "woff",
    "woff2",
    "ttf",
    "otf",
    "json",
    "mp4",
    "webm",
    "other",
)

_CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)", re.I)
_CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*)?(['\"]?)([^'\"\s)]+)\1\s*\)?",
    re.I,
)

# Heuristic classification patterns (evidence-based; confidence tagged).
_CLASS_PATTERNS: list[tuple[str, str, re.Pattern[str], float, str]] = [
    (
        "E",
        "tracking/analytics",
        re.compile(
            r"(google[-_]?analytics|gtag|googletag|facebook\.net|hotjar|"
            r"segment\.|mixpanel|amplitude|sentry|newrelic|datadog|"
            r"steamstats|clientlog|eventlog|telemetry)",
            re.I,
        ),
        0.7,
        "filename/URL pattern matches known analytics/telemetry vendors",
    ),
    (
        "D",
        "online-only runtime",
        re.compile(
            r"(login|signin|oauth|openid|captcha|agecheck|agegate|"
            r"sharedfiles/filedetails|community/profiles|chat|"
            r"websocket|webrtc)",
            re.I,
        ),
        0.55,
        "filename/path suggests auth / community / online-only UI",
    ),
    (
        "C",
        "steam ui common",
        re.compile(
            r"(globalheader|global_nav|store_header|footer|"
            r"steamcommunity|shared_global|buttons_24|"
            r"ico_arrow|ico_close|default_avatar|avatarframe|"
            r"profile_header|badge_|emoticon|sprite)",
            re.I,
        ),
        0.65,
        "Steam chrome / shared UI / avatar / sprite naming",
    ),
    (
        "B",
        "offline render chrome",
        re.compile(
            r"(motiva[_-]?sans|fonts?/|stylesheet|\.css$)",
            re.I,
        ),
        0.45,
        "stylesheet / Motiva / font assets commonly needed for layout",
    ),
    (
        "A",
        "mod content",
        re.compile(
            r"(workshop_item|sharedfile|filedetails|preview|"
            r"publishedfile|ugc|screenshot|header_image|capsule)",
            re.I,
        ),
        0.5,
        "Workshop / UGC / preview naming often ties to Mod content",
    ),
]


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


def classify_extension(path: Path | str) -> str:
    """Map a path/filename to a TYPE_BUCKETS label."""
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
    if suffix == "avif":
        return "avif"
    if suffix == "gif":
        return "gif"
    if suffix == "svg":
        return "svg"
    if suffix == "ico":
        return "ico"
    if suffix == "woff":
        return "woff"
    if suffix == "woff2":
        return "woff2"
    if suffix == "ttf":
        return "ttf"
    if suffix == "otf":
        return "otf"
    if suffix == "json":
        return "json"
    if suffix == "mp4":
        return "mp4"
    if suffix == "webm":
        return "webm"
    return "other"


def fonts_bucket(type_name: str) -> bool:
    return type_name in {"woff", "woff2", "ttf", "otf"}


def sha256_file(path: Path, *, chunk: int = HASH_CHUNK) -> str | None:
    """Stream SHA-256 of a file. Returns None on OSError."""
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
    """Hash many files; keys are str(path)."""
    if not paths:
        return {}
    from concurrent.futures import ThreadPoolExecutor, as_completed

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


def file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


class _AuditHtmlRefParser(HTMLParser):
    """Extract local-capable resource refs from HTML (audit-facing)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[str] = []
        self.css_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {str(k).lower(): (v or "") for k, v in attrs}
        tag_l = tag.lower()
        if tag_l == "link" and ad.get("href"):
            self.refs.append(ad["href"])
        if tag_l in {
            "script",
            "img",
            "iframe",
            "source",
            "audio",
            "video",
            "embed",
            "track",
        } and ad.get("src"):
            self.refs.append(ad["src"])
        if tag_l == "object" and ad.get("data"):
            self.refs.append(ad["data"])
        if ad.get("srcset"):
            for part in str(ad["srcset"]).split(","):
                token = part.strip().split()
                if token:
                    self.refs.append(token[0])
        if ad.get("poster"):
            self.refs.append(ad["poster"])
        if ad.get("style"):
            self.css_text.append(ad["style"])

    def handle_data(self, data: str) -> None:
        lower = data.lower()
        if "url(" in lower or "@import" in lower:
            self.css_text.append(data)


def extract_html_refs(html_text: str) -> list[str]:
    """Return raw refs from HTML (including external). Missing tags → []."""
    parser = _AuditHtmlRefParser()
    try:
        parser.feed(html_text)
    except Exception:  # noqa: BLE001 — audit must not crash on broken HTML
        return []
    refs = list(parser.refs)
    for block in parser.css_text:
        refs.extend(extract_css_urls(block))
        refs.extend(extract_css_imports(block))
    return refs


def extract_css_urls(css_text: str) -> list[str]:
    return [m.group(2) for m in _CSS_URL_RE.finditer(css_text or "")]


def extract_css_imports(css_text: str) -> list[str]:
    return [m.group(2) for m in _CSS_IMPORT_RE.finditer(css_text or "")]


def _strip_ref(ref: str) -> str:
    return unquote(str(ref or "").split("?", 1)[0].split("#", 1)[0].strip())


def resolve_local_ref(base_dir: Path, ref: str, *, root: Path) -> Path | None:
    """Resolve a relative ref under root; None if external/missing/escape."""
    if is_external_or_non_file_ref(ref):
        return None
    cleaned = _strip_ref(ref)
    if not cleaned or is_external_or_non_file_ref(cleaned):
        return None
    try:
        candidate = (base_dir / cleaned).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


def collect_dependency_closure_paths(index_path: Path) -> set[Path]:
    """
    Actual referenced local dependency closure for an offline index.

    Reuses production ``collect_offline_closure_report`` so audit matches
    Backup Offline Snapshot semantics. Missing assets are skipped (no crash).
    """
    report = collect_offline_closure_report(index_path)
    return {p.resolve() for p in report.files.values()}


def walk_files(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    try:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                yield Path(dirpath) / name
    except OSError:
        return


def find_info_dir(mod_folder: Path) -> Path | None:
    for name in INFO_NAMES:
        candidate = mod_folder / name
        if candidate.is_dir():
            return candidate
    return None


def classify_asset_heuristic(
    *,
    filename: str,
    rel_path: str = "",
    referenced: bool = False,
) -> dict[str, Any]:
    """
    Best-effort category A–F. Never claims certainty from filename alone.

    Evidence + confidence are always returned.
    """
    hay = f"{rel_path} {filename}"
    hits: list[dict[str, Any]] = []
    for code, label, pattern, confidence, why in _CLASS_PATTERNS:
        if pattern.search(hay):
            hits.append(
                {
                    "category": code,
                    "label": label,
                    "confidence": confidence,
                    "why": why,
                    "evidence": pattern.pattern[:120],
                    "filename": filename,
                    "path": rel_path,
                    "referenced_by_closure": referenced,
                }
            )
    if referenced and not hits:
        return {
            "category": "B",
            "label": "offline render (referenced)",
            "confidence": 0.6,
            "why": "present in HTML/CSS dependency closure",
            "evidence": "closure membership",
            "filename": filename,
            "path": rel_path,
            "referenced_by_closure": True,
        }
    if hits:
        # Prefer higher confidence; break ties by category order A..F preference
        # is not applied — highest confidence wins.
        hits.sort(key=lambda h: h["confidence"], reverse=True)
        return hits[0]
    return {
        "category": "F",
        "label": "uncertain",
        "confidence": 0.2,
        "why": "no strong filename/path evidence",
        "evidence": "none",
        "filename": filename,
        "path": rel_path,
        "referenced_by_closure": referenced,
    }


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def human_bytes(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} TB"


# ---------------------------------------------------------------------------
# DB (readonly)
# ---------------------------------------------------------------------------


@dataclass
class ModRow:
    mod_id: int
    internal_id: str
    workspace_id: str
    last_known_path: str
    platform: str
    title: str


def load_mods_readonly(db_path: Path) -> list[ModRow]:
    """Open SQLite read-only; never write."""
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(mods)").fetchall()
        }
        select = [
            "mod_id",
            "COALESCE(internal_id,'') AS internal_id",
            "COALESCE(workspace_id,'') AS workspace_id",
            "COALESCE(last_known_path,'') AS last_known_path",
            "COALESCE(platform,'') AS platform",
            "COALESCE(title,'') AS title",
        ]
        # Guard older schemas without internal_id (should not happen in prod).
        if "internal_id" not in cols:
            select[1] = "'' AS internal_id"
        rows = conn.execute(
            f"SELECT {', '.join(select)} FROM mods ORDER BY mod_id"
        ).fetchall()
        return [
            ModRow(
                mod_id=int(r["mod_id"]),
                internal_id=str(r["internal_id"] or ""),
                workspace_id=str(r["workspace_id"] or ""),
                last_known_path=str(r["last_known_path"] or ""),
                platform=str(r["platform"] or ""),
                title=str(r["title"] or ""),
            )
            for r in rows
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scan structures
# ---------------------------------------------------------------------------


@dataclass
class FileRec:
    path: Path
    size: int
    digest: str | None
    type_name: str
    scope: str  # info_assets | info_other | backup_assets | backup_other | info_index | backup_index
    mod_id: int
    internal_id: str
    workspace_id: str
    rel: str


@dataclass
class AuditState:
    mods_scanned: int = 0
    mods_with_info: int = 0
    mods_with_offline_snapshot: int = 0
    mods_with_live_offline_page: int = 0

    info_total_bytes: int = 0
    info_assets_bytes: int = 0
    info_index_bytes: int = 0
    info_other_bytes: int = 0

    backup_offline_total_bytes: int = 0
    backup_assets_bytes: int = 0
    backup_index_bytes: int = 0
    backup_other_bytes: int = 0

    # type -> {count, bytes}
    info_by_type: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: {"count": 0, "bytes": 0})
    )
    backup_by_type: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: {"count": 0, "bytes": 0})
    )

    # cross-mod content hashes for .info/assets only
    info_hash_groups: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    # within-mod duplicate tracking
    within_mod_dup_bytes: int = 0
    within_mod_dup_files: int = 0

    # per-mod rolls
    per_mod: list[dict[str, Any]] = field(default_factory=list)

    # .info ↔ backup identity
    backup_identical_to_info_bytes: int = 0
    backup_identical_to_info_files: int = 0
    backup_same_name_diff_content_files: int = 0
    info_only_asset_bytes: int = 0
    info_only_asset_files: int = 0
    backup_only_asset_bytes: int = 0
    backup_only_asset_files: int = 0
    same_filename_same_content_files: int = 0

    # dependency closure
    closure_bytes: int = 0
    closure_files: int = 0
    info_assets_in_closure_bytes: int = 0
    info_assets_in_closure_files: int = 0
    info_assets_unreferenced_bytes: int = 0
    info_assets_unreferenced_files: int = 0
    mods_closure_analyzed: int = 0

    # category samples
    category_bytes: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    category_files: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    category_samples: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list)
    )

    largest_info_assets: list[dict[str, Any]] = field(default_factory=list)
    largest_backup_assets: list[dict[str, Any]] = field(default_factory=list)

    errors: list[str] = field(default_factory=list)
    architecture_notes: dict[str, Any] = field(default_factory=dict)


def _bump_type(bucket: dict[str, dict[str, int]], type_name: str, size: int) -> None:
    bucket[type_name]["count"] += 1
    bucket[type_name]["bytes"] += size


def _keep_top(items: list[dict[str, Any]], item: dict[str, Any], *, n: int = 100) -> None:
    items.append(item)
    items.sort(key=lambda x: int(x.get("size") or 0), reverse=True)
    del items[n:]


def _iter_info_files(info_dir: Path) -> Iterator[tuple[Path, str]]:
    """Yield (path, scope) under .info. scope ∈ info_index|info_assets|info_other."""
    for path in walk_files(info_dir):
        try:
            rel = path.relative_to(info_dir).as_posix()
        except ValueError:
            continue
        lower = rel.lower()
        if lower in {"index.html", "offline/index.html"}:
            yield path, "info_index"
            continue
        if lower.startswith("assets/") or lower.startswith("offline/assets/"):
            yield path, "info_assets"
            continue
        yield path, "info_other"


def _iter_backup_offline_files(offline_dir: Path) -> Iterator[tuple[Path, str]]:
    for path in walk_files(offline_dir):
        try:
            rel = path.relative_to(offline_dir).as_posix()
        except ValueError:
            continue
        lower = rel.lower()
        if lower == "index.html":
            yield path, "backup_index"
        elif lower.startswith("assets/"):
            yield path, "backup_assets"
        else:
            yield path, "backup_other"


def scan_mod(
    state: AuditState,
    row: ModRow,
    *,
    library_root: Path,
    backup_root: Path,
    hash_assets: bool = True,
) -> None:
    state.mods_scanned += 1
    folder: Path | None = None
    if row.last_known_path:
        candidate = Path(row.last_known_path)
        if candidate.is_dir():
            folder = candidate
    if folder is None:
        # Best-effort discover under library by walking is too expensive;
        # rely on LKP for production audit.
        pass

    info_dir = find_info_dir(folder) if folder is not None else None
    if info_dir is not None:
        state.mods_with_info += 1

    live_index: Path | None = None
    if folder is not None:
        live_index = resolve_offline_page(folder)
        if live_index is not None:
            state.mods_with_live_offline_page += 1

    backup_offline = backup_root / str(row.mod_id) / "offline"
    has_backup_offline = (backup_offline / "index.html").is_file()
    if has_backup_offline:
        state.mods_with_offline_snapshot += 1

    info_asset_by_name: dict[str, tuple[int, str | None]] = {}
    info_asset_hashes: dict[str, list[str]] = defaultdict(list)  # hash -> names
    info_assets_total = 0
    info_assets_count = 0
    backup_assets_total = 0
    backup_assets_count = 0
    closure_set: set[Path] = set()
    closure_asset_rel: set[str] = set()

    # Collect asset paths first, then hash in parallel (SSD-friendly).
    info_asset_rows: list[tuple[Path, int, str, str]] = []  # path,size,rel,type
    backup_asset_rows: list[tuple[Path, int, str, str]] = []

    # --- LIVE .info ---
    if info_dir is not None:
        for path, scope in _iter_info_files(info_dir):
            size = file_size(path)
            state.info_total_bytes += size
            type_name = classify_extension(path)
            try:
                rel = path.relative_to(info_dir).as_posix()
            except ValueError:
                rel = path.name

            if scope == "info_index":
                state.info_index_bytes += size
            elif scope == "info_assets":
                state.info_assets_bytes += size
                info_assets_total += size
                info_assets_count += 1
                _bump_type(state.info_by_type, type_name, size)
                info_asset_rows.append((path, size, rel, type_name))
                _keep_top(
                    state.largest_info_assets,
                    {
                        "size": size,
                        "path": str(path),
                        "extension": path.suffix.lower(),
                        "mod_id": row.mod_id,
                        "workspace_id": row.workspace_id,
                        "internal_id": row.internal_id,
                        "type": type_name,
                    },
                )
            else:
                state.info_other_bytes += size

    # --- Backup offline (collect before hashing) ---
    backup_asset_by_name: dict[str, tuple[int, str | None]] = {}
    if has_backup_offline:
        for path, scope in _iter_backup_offline_files(backup_offline):
            size = file_size(path)
            state.backup_offline_total_bytes += size
            type_name = classify_extension(path)
            try:
                rel = path.relative_to(backup_offline).as_posix()
            except ValueError:
                rel = path.name

            if scope == "backup_index":
                state.backup_index_bytes += size
            elif scope == "backup_assets":
                state.backup_assets_bytes += size
                backup_assets_total += size
                backup_assets_count += 1
                _bump_type(state.backup_by_type, type_name, size)
                backup_asset_rows.append((path, size, rel, type_name))
                _keep_top(
                    state.largest_backup_assets,
                    {
                        "size": size,
                        "path": str(path),
                        "extension": path.suffix.lower(),
                        "mod_id": row.mod_id,
                        "workspace_id": row.workspace_id,
                        "internal_id": row.internal_id,
                        "type": type_name,
                    },
                )
            else:
                state.backup_other_bytes += size

    hash_map: dict[str, str | None] = {}
    if hash_assets:
        to_hash = [p for p, *_ in info_asset_rows] + [p for p, *_ in backup_asset_rows]
        hash_map = sha256_files_parallel(to_hash, workers=8)

    for path, size, rel, _type_name in info_asset_rows:
        digest = hash_map.get(str(path)) if hash_assets else None
        if digest:
            state.info_hash_groups[digest].append(
                {
                    "path": str(path),
                    "size": size,
                    "mod_id": row.mod_id,
                    "internal_id": row.internal_id,
                    "workspace_id": row.workspace_id,
                    "rel": rel,
                    "name": path.name,
                }
            )
            info_asset_hashes[digest].append(path.name)
        name_key = path.name
        if "/assets/" in f"/{rel}":
            name_key = rel.split("assets/", 1)[-1]
        info_asset_by_name[name_key] = (size, digest)

    # within-mod content duplicates among .info/assets
    for digest, names in info_asset_hashes.items():
        if len(names) < 2:
            continue
        sample = state.info_hash_groups.get(digest) or []
        size = int(sample[0]["size"]) if sample else 0
        copies = [c for c in sample if c["mod_id"] == row.mod_id]
        if len(copies) >= 2:
            state.within_mod_dup_files += len(copies) - 1
            state.within_mod_dup_bytes += size * (len(copies) - 1)

    for path, size, rel, _type_name in backup_asset_rows:
        digest = hash_map.get(str(path)) if hash_assets else None
        name_key = path.name
        if rel.startswith("assets/"):
            name_key = rel[len("assets/") :]
        backup_asset_by_name[name_key] = (size, digest)

    # --- Dependency closure from LIVE index ---
    if live_index is not None:
        try:
            closure_set = collect_dependency_closure_paths(live_index)
            state.mods_closure_analyzed += 1
            for p in closure_set:
                sz = file_size(p)
                state.closure_bytes += sz
                state.closure_files += 1
                try:
                    if info_dir is not None:
                        rel = p.resolve().relative_to(info_dir.resolve()).as_posix()
                        if "assets/" in rel:
                            closure_asset_rel.add(rel.split("assets/", 1)[-1])
                except (OSError, ValueError):
                    pass
        except Exception as exc:  # noqa: BLE001
            state.errors.append(f"closure mod_id={row.mod_id}: {exc}")

        if info_dir is not None:
            assets_roots = [
                info_dir / "assets",
                info_dir / "offline" / "assets",
            ]
            seen_asset: set[str] = set()
            for assets_root in assets_roots:
                if not assets_root.is_dir():
                    continue
                for path in walk_files(assets_root):
                    try:
                        resolved = path.resolve()
                        key = str(resolved)
                    except OSError:
                        continue
                    if key in seen_asset:
                        continue
                    seen_asset.add(key)
                    size = file_size(path)
                    in_closure = resolved in closure_set
                    try:
                        rel = path.relative_to(info_dir).as_posix()
                    except ValueError:
                        rel = path.name
                    if in_closure:
                        state.info_assets_in_closure_bytes += size
                        state.info_assets_in_closure_files += 1
                    else:
                        state.info_assets_unreferenced_bytes += size
                        state.info_assets_unreferenced_files += 1
                    cat = classify_asset_heuristic(
                        filename=path.name,
                        rel_path=rel,
                        referenced=in_closure,
                    )
                    code = str(cat["category"])
                    state.category_bytes[code] += size
                    state.category_files[code] += 1
                    samples = state.category_samples[code]
                    if len(samples) < 15:
                        samples.append(cat)

    # --- Per-mod .info <-> backup asset set compare ---
    info_names = set(info_asset_by_name)
    backup_names = set(backup_asset_by_name)
    for name in info_names & backup_names:
        isz, ih = info_asset_by_name[name]
        bsz, bh = backup_asset_by_name[name]
        if ih and bh and ih == bh:
            state.same_filename_same_content_files += 1
            state.backup_identical_to_info_bytes += bsz
            state.backup_identical_to_info_files += 1
        elif ih and bh and ih != bh:
            state.backup_same_name_diff_content_files += 1
        elif hash_assets and ih and bh is None:
            pass
        elif hash_assets and bh and ih is None:
            pass
        else:
            if isz == bsz and isz > 0:
                state.backup_identical_to_info_bytes += bsz
                state.backup_identical_to_info_files += 1

    if hash_assets:
        info_digest_sizes = {
            dig: sz
            for _name, (sz, dig) in info_asset_by_name.items()
            if dig
        }
        matched_backup: set[str] = set()
        for name, (bsz, bh) in backup_asset_by_name.items():
            if not bh:
                continue
            if name in info_names:
                continue
            if bh in info_digest_sizes and name not in matched_backup:
                state.backup_identical_to_info_bytes += bsz
                state.backup_identical_to_info_files += 1
                matched_backup.add(name)

    for name in info_names - backup_names:
        sz, _ = info_asset_by_name[name]
        state.info_only_asset_bytes += sz
        state.info_only_asset_files += 1
    for name in backup_names - info_names:
        sz, dig = backup_asset_by_name[name]
        state.backup_only_asset_bytes += sz
        state.backup_only_asset_files += 1

    state.per_mod.append(
        {
            "mod_id": row.mod_id,
            "internal_id": row.internal_id,
            "workspace_id": row.workspace_id,
            "platform": row.platform,
            "title": row.title[:120],
            "folder": str(folder) if folder else "",
            "info_assets_bytes": info_assets_total,
            "info_assets_files": info_assets_count,
            "backup_offline_assets_bytes": backup_assets_total,
            "backup_offline_assets_files": backup_assets_count,
            "combined_assets_bytes": info_assets_total + backup_assets_total,
            "has_info": info_dir is not None,
            "has_backup_offline": has_backup_offline,
            "live_index": str(live_index) if live_index else "",
            "closure_files": len(closure_set),
        }
    )


def compute_cross_mod_dupes(state: AuditState) -> dict[str, Any]:
    unique_files = 0
    duplicate_files = 0
    duplicate_groups = 0
    duplicate_bytes = 0  # sum of all copies in groups with count>1
    reclaimable = 0  # (count-1)*size across groups
    cross_mod_groups = 0
    cross_mod_reclaimable = 0
    top: list[dict[str, Any]] = []

    for digest, copies in state.info_hash_groups.items():
        n = len(copies)
        if n == 0:
            continue
        size = int(copies[0]["size"])
        unique_files += 1
        if n == 1:
            continue
        duplicate_groups += 1
        duplicate_files += n
        duplicate_bytes += size * n
        waste = size * (n - 1)
        reclaimable += waste
        mod_ids = {c["mod_id"] for c in copies}
        is_cross = len(mod_ids) > 1
        if is_cross:
            cross_mod_groups += 1
            cross_mod_reclaimable += waste
        top.append(
            {
                "hash": digest,
                "size": size,
                "copy_count": n,
                "total_bytes": size * n,
                "waste_bytes": waste,
                "cross_mod": is_cross,
                "mod_count": len(mod_ids),
                "sample_paths": [c["path"] for c in copies[:5]],
            }
        )

    top.sort(key=lambda x: int(x["waste_bytes"]), reverse=True)
    return {
        "unique_content_hashes": unique_files,
        "duplicate_files": duplicate_files,
        "duplicate_groups": duplicate_groups,
        "duplicate_bytes": duplicate_bytes,
        "potentially_reclaimable_bytes": reclaimable,
        "cross_mod_duplicate_groups": cross_mod_groups,
        "cross_mod_reclaimable_bytes": cross_mod_reclaimable,
        "within_mod_dup_files": state.within_mod_dup_files,
        "within_mod_dup_bytes": state.within_mod_dup_bytes,
        "top_100": top[:100],
    }


def growth_projection(per_mod_asset_bytes: list[int], targets: list[int]) -> dict[str, Any]:
    """Linear baseline from current mean combined (.info + backup) per mod."""
    if not per_mod_asset_bytes:
        return {str(t): {"note": "no data"} for t in targets}
    mean = sum(per_mod_asset_bytes) / len(per_mod_asset_bytes)
    # Also split: we need separate means — caller passes combined; also compute from state later
    return {
        "method": "linear baseline from current production mean per Mod",
        "disclaimer": (
            "Linear baseline from current production data — NOT a post-optimization forecast."
        ),
        "current_n": len(per_mod_asset_bytes),
        "mean_combined_assets_bytes": mean,
        "projections": {
            str(t): {
                "combined_assets_bytes": int(mean * t),
                "combined_assets_human": human_bytes(mean * t),
            }
            for t in targets
        },
    }


def architecture_notes() -> dict[str, Any]:
    return {
        "info_producer": (
            "Steam: services.archive.OfflinePageArchiver.archive → "
            ".info/index.html + .info/assets/ (CSS/fonts/images; scripts stripped). "
            "Other platforms: providers write .info/offline/index.html + assets/."
        ),
        "what_is_downloaded": (
            "CSS (max 60), images (max 120), fonts (max 40), plus nested CSS url() "
            "up to 3 localization rounds. Scripts/iframes stripped before download. "
            "Filenames are sha1(url)[:16]+ext. Global cache/asset_cache seeds copies."
        ),
        "refresh_behavior": (
            "ensure_offline_page skips valid Workshop page unless force_refresh; "
            "UI OfflineArchiveWorker defaults force_refresh=True; Sync skips if "
            "valid page exists unless overwrite_files."
        ),
        "backup_contract": (
            "Offline Snapshot = minimum viable local dependency closure "
            "(services.offline.backup_closure.snapshot_offline_closure). "
            "Never copytree full .info/assets."
        ),
        "resolve_offline_page": (
            ".info/offline/index.html preferred, else .info/index.html "
            "(services.offline.paths.resolve_offline_page)."
        ),
        "repair": (
            "repair_live_offline_backups re-copies LIVE closure into backup; "
            "does not re-scrape Steam."
        ),
    }


def build_report(state: AuditState, dupes: dict[str, Any], projections: dict[str, Any]) -> str:
    info_assets = state.info_assets_bytes
    backup_assets = state.backup_assets_bytes
    identical = state.backup_identical_to_info_bytes
    ratio = (identical / backup_assets * 100.0) if backup_assets else 0.0

    closure_vs_info = (
        state.info_assets_in_closure_bytes / info_assets * 100.0 if info_assets else 0.0
    )
    unref_pct = (
        state.info_assets_unreferenced_bytes / info_assets * 100.0 if info_assets else 0.0
    )

    def type_table(bucket: dict[str, dict[str, int]], total: int) -> str:
        lines = [
            "| Type | File Count | Total Bytes | % |",
            "| ----- | ---------: | ----------: | -: |",
        ]
        # roll fonts
        rows: list[tuple[str, int, int]] = []
        font_c = font_b = 0
        for t in TYPE_BUCKETS:
            c = int(bucket.get(t, {}).get("count") or 0)
            b = int(bucket.get(t, {}).get("bytes") or 0)
            if fonts_bucket(t):
                font_c += c
                font_b += b
                continue
            if t in {"woff", "woff2", "ttf", "otf"}:
                continue
            rows.append((t.upper() if t != "other" else "Other", c, b))
        rows.append(("Fonts", font_c, font_b))
        rows.sort(key=lambda r: r[2], reverse=True)
        for name, c, b in rows:
            if c == 0 and b == 0:
                continue
            pct = (b / total * 100.0) if total else 0.0
            lines.append(f"| {name} | {c} | {b} ({human_bytes(b)}) | {pct:.1f}% |")
        return "\n".join(lines)

    per_sorted = sorted(
        state.per_mod, key=lambda m: int(m["combined_assets_bytes"]), reverse=True
    )
    combined_vals = sorted(float(m["combined_assets_bytes"]) for m in state.per_mod)
    info_vals = sorted(float(m["info_assets_bytes"]) for m in state.per_mod)

    def stats_block(vals: list[float], label: str) -> str:
        if not vals:
            return f"{label}: (no data)"
        mean = sum(vals) / len(vals)
        return (
            f"{label}: median={human_bytes(percentile(vals, 50))} "
            f"mean={human_bytes(mean)} "
            f"P90={human_bytes(percentile(vals, 90))} "
            f"P95={human_bytes(percentile(vals, 95))} "
            f"P99={human_bytes(percentile(vals, 99))} "
            f"max={human_bytes(vals[-1])}"
        )

    top_mods_lines = []
    for m in per_sorted[:20]:
        top_mods_lines.append(
            f"- mod_id={m['mod_id']} wid={m['workspace_id']} "
            f"info={human_bytes(m['info_assets_bytes'])} "
            f"backup={human_bytes(m['backup_offline_assets_bytes'])} "
            f"combined={human_bytes(m['combined_assets_bytes'])} "
            f"title={m['title']!r}"
        )

    # Strategy savings estimates (evidence-based from this scan)
    cas_save = int(dupes.get("cross_mod_reclaimable_bytes") or 0)
    unref_save = state.info_assets_unreferenced_bytes
    # Backup that is identical to .info — Strategy A/C might avoid re-copy
    # but Backup contract needs portability — note carefully
    backup_dup_save = identical

    lines = [
        "# Offline Web Asset Audit Report",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "**Status: AUDIT COMPLETE (no optimization applied)**",
        "",
        "=== Offline Web Asset Audit ===",
        "",
        f"Mods scanned: {state.mods_scanned}",
        f"Mods with .info: {state.mods_with_info}",
        f"Mods with Offline Snapshot: {state.mods_with_offline_snapshot}",
        f"Mods with LIVE offline page: {state.mods_with_live_offline_page}",
        f"Mods closure-analyzed: {state.mods_closure_analyzed}",
        "",
        f".info total: {human_bytes(state.info_total_bytes)} ({state.info_total_bytes} bytes)",
        f".info/assets total: {human_bytes(info_assets)} ({info_assets} bytes)",
        f".info/index.html total: {human_bytes(state.info_index_bytes)}",
        f"other .info: {human_bytes(state.info_other_bytes)}",
        "",
        f"Backup Offline total: {human_bytes(state.backup_offline_total_bytes)}",
        f"Backup offline/assets total: {human_bytes(backup_assets)} ({backup_assets} bytes)",
        f"Backup offline/index.html total: {human_bytes(state.backup_index_bytes)}",
        f"other offline: {human_bytes(state.backup_other_bytes)}",
        "",
        f"Exact duplicate .info <-> Backup (content-identical asset bytes in Backup): "
        f"{human_bytes(identical)} ({identical} bytes)",
        f"Backup duplication ratio (identical / backup assets): {ratio:.2f}%",
        f"Same filename + same content files: {state.same_filename_same_content_files}",
        f"Same filename / different content files: {state.backup_same_name_diff_content_files}",
        f".info-only asset bytes: {human_bytes(state.info_only_asset_bytes)} "
        f"({state.info_only_asset_files} files)",
        f"Backup-only asset bytes: {human_bytes(state.backup_only_asset_bytes)} "
        f"({state.backup_only_asset_files} files)",
        "",
        "Cross-Mod duplicate (.info/assets content-hash):",
        f"  unique hashes: {dupes.get('unique_content_hashes')}",
        f"  duplicate groups: {dupes.get('duplicate_groups')}",
        f"  potentially reclaimable (all dups): {human_bytes(dupes.get('potentially_reclaimable_bytes') or 0)}",
        f"  cross-Mod reclaimable: {human_bytes(cas_save)}",
        f"  within-Mod reclaimable: {human_bytes(dupes.get('within_mod_dup_bytes') or 0)}",
        "",
        "Actual HTML/CSS dependency closure:",
        f"  closure files: {state.closure_files}",
        f"  closure bytes (all closure members): {human_bytes(state.closure_bytes)}",
        f"  .info/assets in closure: {human_bytes(state.info_assets_in_closure_bytes)} "
        f"({state.info_assets_in_closure_files} files) = {closure_vs_info:.1f}% of .info/assets",
        f"  Unreferenced .info/assets: {human_bytes(state.info_assets_unreferenced_bytes)} "
        f"({state.info_assets_unreferenced_files} files) = {unref_pct:.1f}%",
        "  NOTE: unreferenced ≠ safe to delete (audit fact only).",
        "",
        "## .info/assets by type",
        type_table(state.info_by_type, info_assets),
        "",
        "## Backup offline/assets by type",
        type_table(state.backup_by_type, backup_assets),
        "",
        "## Heuristic categories (A–F) over .info/assets",
    ]
    for code in ("A", "B", "C", "D", "E", "F"):
        lines.append(
            f"- {code}: files={state.category_files.get(code, 0)} "
            f"bytes={human_bytes(state.category_bytes.get(code, 0))}"
        )
    lines.extend(
        [
            "",
            stats_block(info_vals, ".info/assets per Mod"),
            stats_block(combined_vals, "combined .info+backup assets per Mod"),
            "",
            "## Top Mods (combined assets)",
            *top_mods_lines,
            "",
            "## Growth (linear baseline — NOT optimized)",
            projections.get("disclaimer", ""),
            f"current_n={projections.get('current_n')} "
            f"mean_combined={human_bytes(projections.get('mean_combined_assets_bytes') or 0)}",
        ]
    )
    for t, row in (projections.get("projections") or {}).items():
        lines.append(f"  {t} Mods → {row.get('combined_assets_human')} combined assets")

    # Q&A
    lines.extend(
        [
            "",
            "## Answers (Q1–Q8)",
            "",
            "### Q1 — What is ~.info/assets composed of?",
            f"Measured .info/assets = {human_bytes(info_assets)}. "
            "See type table above (images/CSS/fonts dominate Steam archives). "
            "Produced by OfflinePageArchiver (CSS≤60, images≤120, fonts≤40 + nested CSS url()).",
            "",
            "### Q2 — How much did Backup copy?",
            f"Backup offline/assets = {human_bytes(backup_assets)}; "
            f"Backup offline total = {human_bytes(state.backup_offline_total_bytes)}.",
            "",
            "### Q3 — How much of Backup is .info duplicate content?",
            f"{human_bytes(identical)} ({ratio:.2f}% of Backup assets) are content-identical "
            "to this Mod's .info assets (by SHA-256).",
            "",
            "### Q4 — How much of .info/assets is true Offline dependency?",
            f"{human_bytes(state.info_assets_in_closure_bytes)} "
            f"({closure_vs_info:.1f}%) of .info/assets bytes appear in the LIVE "
            "HTML/CSS/JS dependency closure used by Backup Offline Snapshot.",
            f"Unreferenced (fact only): {human_bytes(state.info_assets_unreferenced_bytes)} "
            f"({unref_pct:.1f}%).",
            "",
            "### Q5 — What can leave Offline Snapshot?",
            "Anything outside the minimum viable dependency closure is already "
            "supposed to be excluded by Contract. Remaining Backup cost is largely "
            "closure-required Steam chrome (CSS/fonts/shared UI) + Mod preview images. "
            "Candidates to tighten closure classification (optional CSS url fonts that "
            "are not required for validity) need a separate Optimization Gate.",
            "",
            "### Q6 — What can be blocked at .info scrape time?",
            "Steam shared UI sprites/avatars, excess nested CSS url() decoration, "
            "and any residual analytics (scripts already stripped). "
            f"Cross-Mod identical content reclaimable ≈ {human_bytes(cas_save)} via CAS "
            "or scrape-time shared store.",
            "",
            "### Q7 — Cross-Mod dedup theoretical savings?",
            f"CAS / content-addressed shared store: ≈ {human_bytes(cas_save)} "
            f"across {dupes.get('cross_mod_duplicate_groups')} cross-Mod duplicate groups "
            f"(plus within-Mod {human_bytes(dupes.get('within_mod_dup_bytes') or 0)}).",
            "",
            "### Q8 — Recommended architecture for 3000+/10000+?",
            "See P0/P1/P2 below. Baseline growth without change is linear with Mod count "
            "(see projections).",
            "",
            "## Optimization candidates (NOT implemented)",
            "",
            "### P0 — Stop paying twice for closure-identical bytes in Backup portability redesign",
            f"- Strategy A/C evidence: Backup assets ≈ {human_bytes(backup_assets)}; "
            f"{ratio:.1f}% content-identical to .info.",
            f"- Estimated savings if Backup stores refs/CAS instead of copies: up to ~{human_bytes(backup_dup_save)} "
            "(portability/MISS restore risk — Contract change).",
            "- Complexity: High. Risk: High (Backup must survive LIVE deletion).",
            "- Affects Contract: YES. Offline open: must keep file:// usable. Repair/Refresh: redesign.",
            "",
            "### P0 — Scrape-time filter of Steam chrome / unreferenced download set (Strategy B)",
            f"- Unreferenced .info/assets today: {human_bytes(unref_save)} ({unref_pct:.1f}%).",
            "- Estimated savings on future archives: similar fraction of new scrapes.",
            "- Complexity: Medium. Risk: Medium (layout fidelity). Contract Offline Snapshot: no change if LIVE still has needed closure.",
            "",
            "### P1 — Global CAS for .info assets (Strategy G)",
            f"- Cross-Mod reclaimable ≈ {human_bytes(cas_save)}.",
            "- Complexity: High (refcount, orphan, restore, corruption, migration).",
            "- Verdict: worth Phase-2 after scrape filters; not first if P0 filters remove most waste.",
            "",
            "### P1 — True min closure tighten (Strategy C) + JS online strip (Strategy F)",
            "- Scripts already stripped at Steam archive; residual may be CSS/fonts.",
            "- Complexity: Medium. Risk: visual regression.",
            "",
            "### P2 — Image compress / WebP (Strategy D), font subset (Strategy E)",
            "- Does not remove duplicate copies; reduces per-copy size.",
            "- Complexity: Medium. Risk: quality / CPU on archive.",
            "",
            "### P3 — .info evidence lifecycle (Strategy H)",
            "- Keep short-lived full mirror; promote only closure to durable store.",
            "- Complexity: High. Affects Refresh/Repair history.",
            "",
            "## CAS risk checklist",
            "- refcount / orphan / delete / restore / backup portability /",
            "  atomic write / corruption / migration / repair / mod deletion",
            "",
            "## Architecture (current)",
            json.dumps(state.architecture_notes, indent=2),
            "",
            "## Production safety",
            "- production files modified: NO",
            "- production DB modified: NO",
            "- production Backup modified: NO",
            "- production .info modified: NO",
            "",
            "Gate: AUDIT CLOSED (pending test PASS in CI/local run)",
        ]
    )
    return "\n".join(lines)


def run_audit(
    *,
    limit: int | None = None,
    hash_assets: bool = True,
    library_root: Path | None = None,
    data_root: Path | None = None,
) -> dict[str, Any]:
    t0 = time.monotonic()
    library_root = library_root or (_REPO / "mod")
    data_root = data_root or (_REPO / "data")
    db_path = data_root / "mod_manager.db"
    backup_root = data_root / "mod_backup"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    state = AuditState()
    state.architecture_notes = architecture_notes()

    mods = load_mods_readonly(db_path)
    if limit is not None and limit > 0:
        mods = mods[:limit]

    total = len(mods)
    for i, row in enumerate(mods, 1):
        scan_mod(
            state,
            row,
            library_root=library_root,
            backup_root=backup_root,
            hash_assets=hash_assets,
        )
        if i % 100 == 0 or i == total:
            elapsed = time.monotonic() - t0
            print(
                f"[audit] {i}/{total} mods  "
                f"info_assets={human_bytes(state.info_assets_bytes)}  "
                f"backup_assets={human_bytes(state.backup_assets_bytes)}  "
                f"elapsed={elapsed:.1f}s",
                flush=True,
            )

    dupes = compute_cross_mod_dupes(state)
    combined = [int(m["combined_assets_bytes"]) for m in state.per_mod]
    # Separate projections for info / backup / combined
    info_mean = (
        sum(int(m["info_assets_bytes"]) for m in state.per_mod) / len(state.per_mod)
        if state.per_mod
        else 0.0
    )
    backup_mean = (
        sum(int(m["backup_offline_assets_bytes"]) for m in state.per_mod) / len(state.per_mod)
        if state.per_mod
        else 0.0
    )
    projections = growth_projection(combined, [3000, 5000, 10000, 20000])
    projections["info_mean_bytes"] = info_mean
    projections["backup_mean_bytes"] = backup_mean
    projections["projections_detail"] = {
        str(t): {
            "info_storage": human_bytes(info_mean * t),
            "backup_offline_storage": human_bytes(backup_mean * t),
            "combined_storage": human_bytes((info_mean + backup_mean) * t),
        }
        for t in (3000, 5000, 10000, 20000)
    }

    report_md = build_report(state, dupes, projections)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_sec": round(time.monotonic() - t0, 2),
        "status": "AUDIT COMPLETE",
        "mods_scanned": state.mods_scanned,
        "mods_with_info": state.mods_with_info,
        "mods_with_offline_snapshot": state.mods_with_offline_snapshot,
        "mods_with_live_offline_page": state.mods_with_live_offline_page,
        "mods_closure_analyzed": state.mods_closure_analyzed,
        "info_total_bytes": state.info_total_bytes,
        "info_assets_bytes": state.info_assets_bytes,
        "info_index_bytes": state.info_index_bytes,
        "info_other_bytes": state.info_other_bytes,
        "backup_offline_total_bytes": state.backup_offline_total_bytes,
        "backup_assets_bytes": state.backup_assets_bytes,
        "backup_index_bytes": state.backup_index_bytes,
        "backup_other_bytes": state.backup_other_bytes,
        "backup_identical_to_info_bytes": state.backup_identical_to_info_bytes,
        "backup_identical_to_info_files": state.backup_identical_to_info_files,
        "backup_duplication_ratio": (
            state.backup_identical_to_info_bytes / state.backup_assets_bytes
            if state.backup_assets_bytes
            else 0.0
        ),
        "info_only_asset_bytes": state.info_only_asset_bytes,
        "backup_only_asset_bytes": state.backup_only_asset_bytes,
        "info_by_type": {k: dict(v) for k, v in state.info_by_type.items()},
        "backup_by_type": {k: dict(v) for k, v in state.backup_by_type.items()},
        "closure_bytes": state.closure_bytes,
        "closure_files": state.closure_files,
        "info_assets_in_closure_bytes": state.info_assets_in_closure_bytes,
        "info_assets_unreferenced_bytes": state.info_assets_unreferenced_bytes,
        "info_assets_unreferenced_files": state.info_assets_unreferenced_files,
        "category_bytes": dict(state.category_bytes),
        "category_files": dict(state.category_files),
        "category_samples": {k: v for k, v in state.category_samples.items()},
        "cross_mod_dupes": {
            k: v for k, v in dupes.items() if k != "top_100"
        },
        "projections": projections,
        "architecture_notes": state.architecture_notes,
        "errors": state.errors[:50],
        "production_safety": {
            "production_files_modified": "NO",
            "production_db_modified": "NO",
            "production_backup_modified": "NO",
            "production_info_modified": "NO",
        },
    }

    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    OUT_REPORT.write_text(report_md, encoding="utf-8")
    OUT_TOP_ASSETS.write_text(
        json.dumps(
            {
                "info_assets_top_100": state.largest_info_assets,
                "backup_assets_top_100": state.largest_backup_assets,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    OUT_TOP_DUPES.write_text(json.dumps(dupes.get("top_100") or [], indent=2), encoding="utf-8")
    top_mods = sorted(
        state.per_mod, key=lambda m: int(m["combined_assets_bytes"]), reverse=True
    )[:100]
    OUT_TOP_MODS.write_text(json.dumps(top_mods, indent=2), encoding="utf-8")

    # Windows consoles may be GBK; never fail the audit on print encoding.
    summary = (
        f"=== Offline Web Asset Audit ===\n"
        f"Mods scanned: {state.mods_scanned}\n"
        f".info/assets: {human_bytes(state.info_assets_bytes)}\n"
        f"Backup offline/assets: {human_bytes(state.backup_assets_bytes)}\n"
        f"Identical .info<->Backup: {human_bytes(state.backup_identical_to_info_bytes)}\n"
        f"Cross-Mod reclaimable: {human_bytes(dupes.get('cross_mod_reclaimable_bytes') or 0)}\n"
        f"Unreferenced .info/assets: {human_bytes(state.info_assets_unreferenced_bytes)}\n"
        f"Full report: {OUT_REPORT}\n"
    )
    try:
        print(summary)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(summary.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_REPORT}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Readonly Offline Web Asset audit")
    parser.add_argument("--limit", type=int, default=0, help="Scan only first N mods (0=all)")
    parser.add_argument(
        "--no-hash",
        action="store_true",
        help="Skip SHA-256 (faster smoke; weakens duplicate metrics)",
    )
    args = parser.parse_args(argv)
    run_audit(
        limit=args.limit or None,
        hash_assets=not args.no_hash,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
