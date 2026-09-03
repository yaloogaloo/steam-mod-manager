#!/usr/bin/env python3
"""
Readonly inventory + reference scan of ``data/``.

Does not delete, move, or mutate any file under ``data/``.
Writes machine-readable output to ``docs/p0_data_hygiene_inventory.json``.

Usage:
  python tools/p0_data_hygiene_audit.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

DATA = _REPO / "data"
DOCS_OUT = _REPO / "docs" / "p0_data_hygiene_inventory.json"

CODE_GLOBS = ("*.py", "*.md", "*.json", "*.toml", "*.ini", "*.ps1", "*.bat", "*.yml", "*.yaml")
SKIP_SCAN_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "data",
    "mod",
    "node_modules",
}

# Hash files that share a size with at least one other file, up to this cap.
# Do not full-hash production backup / cache trees (too large; treated separately).
HASH_MAX_BYTES = 8 * 1024 * 1024
HASH_CHUNK = 1024 * 1024
SKIP_HASH_PREFIXES = (
    "mod_backup/",
    "asset_cache/",
    "identity_repair_production_backup/",
    "identity_repair_quarantine/",
    "import_cache/",
    "headers/",
    "browser_profile/",
    "_smoke_ws/",
    "_smoke_ws2/",
)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, OverflowError, ValueError):
        return None


def _stat_times(path: Path) -> tuple[int, float | None, float | None, float | None]:
    try:
        st = path.stat()
    except OSError:
        return 0, None, None, None
    size = int(st.st_size) if stat.S_ISREG(st.st_mode) else 0
    mtime = float(st.st_mtime)
    ctime = float(st.st_ctime)
    atime = float(st.st_atime)
    return size, mtime, ctime, atime


def walk_inventory(root: Path) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    dirs: list[dict[str, Any]] = []
    dir_totals: dict[str, dict[str, int]] = defaultdict(lambda: {"file_count": 0, "total_size": 0})
    errors: list[str] = []

    def add_dir_rollups(rel: Path, size: int) -> None:
        parts = rel.parts
        acc = Path()
        for part in parts[:-1]:
            acc = acc / part
            key = acc.as_posix()
            dir_totals[key]["file_count"] += 1
            dir_totals[key]["total_size"] += size
        dir_totals[""]["file_count"] += 1
        dir_totals[""]["total_size"] += size

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        dpath = Path(dirpath)
        rel_dir = dpath.relative_to(root)
        rel_dir_s = "." if rel_dir.as_posix() == "." else rel_dir.as_posix()
        size, mtime, ctime, _atime = _stat_times(dpath)
        dirs.append(
            {
                "relative_path": rel_dir_s if rel_dir_s != "." else ".",
                "file_or_directory": "directory",
                "size_bytes": 0,
                "modified_time": _iso(mtime),
                "created_time": _iso(ctime),
                "extension": "",
                "empty": not dirnames and not filenames,
            }
        )
        for name in filenames:
            fpath = dpath / name
            try:
                fsize, fmtime, fctime, _ = _stat_times(fpath)
            except OSError as exc:
                errors.append(f"{fpath}: {exc}")
                continue
            rel = fpath.relative_to(root)
            files.append(
                {
                    "relative_path": rel.as_posix(),
                    "file_or_directory": "file",
                    "size_bytes": fsize,
                    "modified_time": _iso(fmtime),
                    "created_time": _iso(fctime),
                    "extension": fpath.suffix.lower(),
                }
            )
            add_dir_rollups(rel, fsize)

    for d in dirs:
        key = "" if d["relative_path"] in {".", ""} else d["relative_path"]
        totals = dir_totals.get(key, {"file_count": 0, "total_size": 0})
        d["file_count"] = totals["file_count"]
        d["total_size"] = totals["total_size"]
        if d["relative_path"] == ".":
            d["file_count"] = dir_totals[""]["file_count"]
            d["total_size"] = dir_totals[""]["total_size"]

    return {
        "files": files,
        "directories": dirs,
        "dir_totals": {k: dict(v) for k, v in dir_totals.items()},
        "errors": errors,
    }


def hash_file(path: Path, limit: int = HASH_MAX_BYTES) -> str | None:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > limit:
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(HASH_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _skip_hash(rel: str) -> bool:
    posix = rel.replace("\\", "/")
    return any(posix.startswith(prefix) for prefix in SKIP_HASH_PREFIXES)


def find_size_duplicate_candidates(
    files: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    by_size: dict[int, list[str]] = defaultdict(list)
    skipped_tree = 0
    for item in files:
        size = int(item["size_bytes"])
        if size <= 0:
            continue
        rel = item["relative_path"]
        if _skip_hash(rel):
            skipped_tree += 1
            continue
        by_size[size].append(rel)
    groups: list[dict[str, Any]] = []
    for size, paths in sorted(by_size.items(), key=lambda kv: -kv[0]):
        if len(paths) < 2:
            continue
        hashes: dict[str, list[str]] = defaultdict(list)
        skipped: list[str] = []
        for rel in paths:
            full = DATA / rel
            digest = hash_file(full)
            if digest is None:
                skipped.append(rel)
                continue
            hashes[digest].append(rel)
        for digest, members in hashes.items():
            if len(members) < 2:
                continue
            groups.append(
                {
                    "sha256": digest,
                    "size_bytes": size,
                    "count": len(members),
                    "paths": members,
                }
            )
        if skipped and size < HASH_MAX_BYTES:
            groups.append(
                {
                    "sha256": None,
                    "size_bytes": size,
                    "count": len(skipped),
                    "paths": skipped,
                    "note": "hash_failed_or_unreadable",
                }
            )
    groups.sort(key=lambda g: (-int(g["size_bytes"]), -int(g["count"])))
    return groups, skipped_tree


def collect_code_files() -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(_REPO):
        dirnames[:] = [d for d in dirnames if d not in SKIP_SCAN_DIRS]
        base = Path(dirpath)
        for name in filenames:
            path = base / name
            if path.suffix.lower() in {
                ".py",
                ".md",
                ".toml",
                ".ini",
                ".ps1",
                ".bat",
                ".yml",
                ".yaml",
            } or name in {".gitignore", "AGENTS.md"}:
                out.append(path)
    return out


_DYNAMIC_PATTERNS = [
    r'data_dir\(\)\s*/',
    r'asset_cache_dir\(\)',
    r'database_path\(\)',
    r'default_mod_library\(\)',
    r'BACKUP_DIR_NAME',
    r'IMPORT_CACHE_DIR_NAME',
    r'ASSET_CACHE_DIR_NAME',
    r'Path\(\s*["\']data["\']\s*\)',
    r'BASE_DIR\s*/\s*["\']data["\']',
    r'os\.path\.join\([^)]*["\']data["\']',
]


def scan_references(top_names: list[str]) -> dict[str, Any]:
    files = collect_code_files()
    name_hits: dict[str, list[str]] = {name: [] for name in top_names}
    dynamic_hits: list[dict[str, str]] = []
    io_hits: list[dict[str, str]] = []
    io_re = re.compile(
        r"(open\(|read_text\(|write_text\(|json\.load|json\.dump|"
        r"sqlite3\.connect|DatabaseManager\.instance|shutil\.(copy|copytree|rmtree)|"
        r"os\.walk|Path\.glob|Path\.rglob|QSettings)",
        re.IGNORECASE,
    )
    dyn_res = [(pat, re.compile(pat)) for pat in _DYNAMIC_PATTERNS]

    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(_REPO).as_posix()
        for name in top_names:
            needle_variants = (
                name,
                f"data/{name}",
                f"data\\\\{name}",
                f'"{name}"',
                f"'{name}'",
            )
            if any(v in text for v in needle_variants):
                # Count line numbers of first few hits
                lines: list[int] = []
                for i, line in enumerate(text.splitlines(), 1):
                    if name in line and (
                        "data" in line.lower()
                        or name.startswith("p0_")
                        or name.startswith("identity_")
                        or name.startswith("_")
                        or name in {"mod_manager.db", "headers", "asset_cache", "import_cache", "mod_backup", "browser_profile"}
                    ):
                        lines.append(i)
                        if len(lines) >= 8:
                            break
                name_hits[name].append(f"{rel}:{',' .join(str(n) for n in lines) if lines else '?'}")
        for pat, compiled in dyn_res:
            if compiled.search(text):
                dynamic_hits.append({"file": rel, "pattern": pat})
        if io_re.search(text) and ("data_dir" in text or "DATA_DIR" in text or ' / "data"' in text or "/ 'data'" in text or 'Path("data")' in text):
            io_hits.append({"file": rel, "kind": "data_io_heuristic"})

    # Dedup dynamic
    seen = set()
    dyn_unique = []
    for hit in dynamic_hits:
        key = (hit["file"], hit["pattern"])
        if key in seen:
            continue
        seen.add(key)
        dyn_unique.append(hit)

    return {
        "name_hits": {k: v for k, v in name_hits.items() if v},
        "name_no_hits": sorted(k for k, v in name_hits.items() if not v),
        "dynamic_hits": dyn_unique,
        "data_io_heuristic": io_hits,
        "scanned_file_count": len(files),
    }


def json_semantic_duplicates(top_files: list[Path]) -> list[dict[str, Any]]:
    payloads: dict[str, list[str]] = defaultdict(list)
    skipped: list[str] = []
    for path in top_files:
        if path.suffix.lower() != ".json":
            continue
        try:
            if path.stat().st_size > HASH_MAX_BYTES:
                skipped.append(path.name)
                continue
            obj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            key = hashlib.sha256(
                json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()
            payloads[key].append(path.name)
        except (OSError, json.JSONDecodeError, ValueError):
            skipped.append(path.name)
    groups = [
        {"canonical_json_sha256": digest, "files": names}
        for digest, names in payloads.items()
        if len(names) > 1
    ]
    return groups


def top_level_names(root: Path) -> list[str]:
    try:
        return sorted(p.name for p in root.iterdir())
    except OSError:
        return []


def summarize_backup(root: Path) -> dict[str, Any]:
    base = root / "mod_backup"
    if not base.is_dir():
        return {"exists": False}
    children = []
    try:
        entries = [p for p in base.iterdir() if p.is_dir()]
    except OSError as exc:
        return {"exists": True, "error": str(exc)}
    for child in entries:
        file_count = 0
        total = 0
        has_meta = False
        has_cover = False
        has_offline = False
        for dirpath, _dns, fns in os.walk(child):
            for name in fns:
                fp = Path(dirpath) / name
                try:
                    total += fp.stat().st_size
                except OSError:
                    continue
                file_count += 1
                low = name.lower()
                if low == "metadata.json":
                    has_meta = True
                if low.startswith("cover"):
                    has_cover = True
                if low == "index.html" and "offline" in Path(dirpath).as_posix().replace("\\", "/"):
                    has_offline = True
        meta_hash = None
        meta_path = child / "metadata.json"
        if meta_path.is_file() and meta_path.stat().st_size <= HASH_MAX_BYTES:
            meta_hash = hash_file(meta_path)
        children.append(
            {
                "mod_id": child.name,
                "file_count": file_count,
                "total_size": total,
                "has_metadata": has_meta,
                "has_cover": has_cover,
                "has_offline": has_offline,
                "metadata_sha256": meta_hash,
            }
        )
    children.sort(key=lambda x: -int(x["total_size"]))
    meta_groups: dict[str, list[str]] = defaultdict(list)
    for child in children:
        digest = child.get("metadata_sha256")
        if digest:
            meta_groups[digest].append(child["mod_id"])
    duplicate_metadata = [
        {"sha256": digest, "mod_ids": ids}
        for digest, ids in meta_groups.items()
        if len(ids) > 1
    ]
    return {
        "exists": True,
        "mod_count": len(children),
        "largest": children[:15],
        "empty_or_tiny": [c for c in children if c["file_count"] == 0 or c["total_size"] == 0][:20],
        "total_size": sum(c["total_size"] for c in children),
        "total_files": sum(c["file_count"] for c in children),
        "duplicate_metadata_json": duplicate_metadata[:40],
        "note": "Identical metadata.json across mod_ids is semantic duplicate of sidecar, not proof the backups are disposable. Historical snapshots KEEP.",
    }


def main() -> int:
    if not DATA.is_dir():
        print("data/ missing", file=sys.stderr)
        return 1

    inventory = walk_inventory(DATA)
    files = inventory["files"]
    directories = inventory["directories"]
    names = top_level_names(DATA)
    refs = scan_references(names)
    dupes, skipped_hash_tree_files = find_size_duplicate_candidates(files)
    json_dupes = json_semantic_duplicates([DATA / n for n in names if (DATA / n).is_file()])
    backup = summarize_backup(DATA)

    top_dirs = sorted(
        [d for d in directories if d["relative_path"] != "." and "/" not in d["relative_path"]],
        key=lambda d: -int(d.get("total_size") or 0),
    )
    largest_files = sorted(files, key=lambda f: -int(f["size_bytes"]))[:25]

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "readonly": True,
        "deleted_nothing": True,
        "root": str(DATA),
        "summary": {
            "total_files": len(files),
            "total_directories": len(directories),
            "total_bytes": inventory["dir_totals"].get("", {}).get("total_size", 0),
            "top_level_count": len(names),
            "top_level_names": names,
            "largest_directories": [
                {
                    "relative_path": d["relative_path"],
                    "file_count": d.get("file_count"),
                    "total_size": d.get("total_size"),
                    "empty": d.get("empty"),
                    "modified_time": d.get("modified_time"),
                    "created_time": d.get("created_time"),
                }
                for d in top_dirs
            ],
            "largest_files": largest_files,
        },
        "top_level_entries": [],
        "references": refs,
        "duplicate_hash_groups": dupes[:80],
        "skipped_hash_tree_files": skipped_hash_tree_files,
        "json_semantic_duplicates_top_level": json_dupes,
        "mod_backup": backup,
        "walk_errors": inventory["errors"],
        "note": "Full per-file listing is not written into git. Summary + top_level_entries are the durable artifact.",
        "directories_top_and_nested_count": len(directories),
    }

    for name in names:
        path = DATA / name
        is_dir = path.is_dir()
        size, mtime, ctime, _ = _stat_times(path)
        entry: dict[str, Any] = {
            "relative_path": name,
            "file_or_directory": "directory" if is_dir else "file",
            "size_bytes": 0 if is_dir else size,
            "modified_time": _iso(mtime),
            "created_time": _iso(ctime),
            "extension": "" if is_dir else path.suffix.lower(),
            "code_references": refs["name_hits"].get(name, []),
            "code_reference_count": len(refs["name_hits"].get(name, [])),
        }
        if is_dir:
            match = next((d for d in directories if d["relative_path"] == name), None)
            if match:
                entry["file_count"] = match.get("file_count")
                entry["total_size"] = match.get("total_size")
                entry["empty"] = match.get("empty")
            children = []
            try:
                for child in sorted(path.iterdir(), key=lambda p: p.name.lower())[:40]:
                    csize, cmtime, cctime, _ = _stat_times(child)
                    children.append(
                        {
                            "name": child.name,
                            "file_or_directory": "directory" if child.is_dir() else "file",
                            "size_bytes": 0 if child.is_dir() else csize,
                            "modified_time": _iso(cmtime),
                            "created_time": _iso(cctime),
                        }
                    )
            except OSError as exc:
                entry["list_error"] = str(exc)
            entry["children_preview"] = children
        payload["top_level_entries"].append(entry)

    DOCS_OUT.parent.mkdir(parents=True, exist_ok=True)
    DOCS_OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    summary = payload["summary"]
    print("DATA_HYGIENE_AUDIT readonly=true deleted_nothing=true")
    print(f"total_files={summary['total_files']}")
    print(f"total_directories={summary['total_directories']}")
    print(f"total_bytes={summary['total_bytes']}")
    print("largest_directories:")
    for d in summary["largest_directories"][:12]:
        print(f"  {d['relative_path']}\tfiles={d['file_count']}\tbytes={d['total_size']}")
    print("largest_files:")
    for f in summary["largest_files"][:12]:
        print(f"  {f['relative_path']}\tbytes={f['size_bytes']}")
    print("no_code_hits:")
    for name in refs["name_no_hits"]:
        print(f"  {name}")
    print(f"duplicate_hash_groups={len(dupes)}")
    print(f"wrote={DOCS_OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
