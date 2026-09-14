#!/usr/bin/env python3
"""
Safe cleanup for ``cache/`` only.

Deletes regenerable performance data. Never touches:
  data/asset_store, data/mod_backup, data/deploy_backup, DB,
  Identity, Deployment, .info/assets, .info/manifest.json

Usage:
  python tools/cleanup_cache.py --dry-run
  python tools/cleanup_cache.py --execute
  python tools/cleanup_cache.py --execute --offline-view-only
  python tools/cleanup_cache.py --execute --max-entries 100 --max-bytes-gb 10
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.paths import (  # noqa: E402
    asset_cache_dir,
    cache_temp_dir,
    get_cache_dir,
    headers_cache_dir,
    import_cache_dir,
    offline_view_cache_dir,
    project_root,
)
from services.offline_view_cache import (  # noqa: E402
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ENTRIES,
    enforce_offline_view_lru,
)

# Absolute hard deny — refuse if resolve lands outside cache/.
_FORBIDDEN_NAME_FRAGMENTS = (
    "asset_store",
    "mod_backup",
    "deploy_backup",
    "mod_manager.db",
    ".info",
)


def _assert_under_cache(path: Path, cache_root: Path) -> None:
    try:
        path.resolve().relative_to(cache_root.resolve())
    except ValueError as exc:
        raise SystemExit(f"refusing path outside cache/: {path}") from exc
    text = str(path).replace("\\", "/").lower()
    for frag in _FORBIDDEN_NAME_FRAGMENTS:
        if frag in text and "cache/" not in text.split(frag)[0][-20:]:
            # Still refuse if path somehow includes durable names outside cache.
            if "asset_store" in text or "mod_backup" in text or "deploy_backup" in text:
                raise SystemExit(f"refusing durable path: {path}")


def _wipe_tree(path: Path, *, dry_run: bool) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    files = 0
    nbytes = 0
    if path.is_file():
        try:
            nbytes = int(path.stat().st_size)
        except OSError:
            nbytes = 0
        if not dry_run:
            path.unlink(missing_ok=True)
        return 1, nbytes
    for f in path.rglob("*"):
        if f.is_file():
            files += 1
            try:
                nbytes += int(f.stat().st_size)
            except OSError:
                pass
    if not dry_run:
        shutil.rmtree(path, ignore_errors=False)
    return files, nbytes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cleanup regenerable cache/ only")
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--execute", action="store_true", default=False)
    parser.add_argument(
        "--offline-view-only",
        action="store_true",
        help="Only apply offline_view LRU / wipe, not entire cache/",
    )
    parser.add_argument(
        "--wipe-all",
        action="store_true",
        help="Delete entire cache/ tree (still under cache/ only)",
    )
    parser.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    parser.add_argument("--max-bytes-gb", type=float, default=10.0)
    parser.add_argument(
        "--root",
        type=str,
        default="",
        help="Override cache root (must resolve under project cache/)",
    )
    args = parser.parse_args(argv)

    if not args.execute and not args.dry_run:
        args.dry_run = True
    if args.execute and args.dry_run:
        # --execute wins when both passed
        args.dry_run = False

    cache_root = get_cache_dir()
    if args.root:
        candidate = Path(args.root)
        _assert_under_cache(candidate, cache_root)
        target_root = candidate
    else:
        target_root = cache_root

    print(f"cache_root={cache_root}")
    print(f"target={target_root} dry_run={args.dry_run}")

    # Safety: never accept project data/ as target
    data = project_root() / "data"
    try:
        if target_root.resolve() == data.resolve() or data.resolve() in target_root.resolve().parents:
            raise SystemExit("refusing to operate on data/")
    except OSError:
        pass

    max_bytes = int(float(args.max_bytes_gb) * 1024 * 1024 * 1024)
    if max_bytes <= 0:
        max_bytes = DEFAULT_MAX_BYTES

    if args.wipe_all and not args.offline_view_only:
        _assert_under_cache(target_root, cache_root)
        files, nbytes = _wipe_tree(target_root, dry_run=args.dry_run)
        # Recreate empty cache skeleton after wipe
        if not args.dry_run:
            get_cache_dir()
            offline_view_cache_dir()
            asset_cache_dir()
            import_cache_dir()
            headers_cache_dir()
            cache_temp_dir()
        print(f"wipe_all files~={files} bytes~={nbytes}")
        return 0

    ov = offline_view_cache_dir() if not args.root else target_root
    if args.root:
        ov = target_root
    _assert_under_cache(ov, cache_root)

    result = enforce_offline_view_lru(
        root=ov if ov.name == "offline_view" or args.offline_view_only else offline_view_cache_dir(),
        max_entries=int(args.max_entries),
        max_bytes=max_bytes,
        dry_run=bool(args.dry_run),
    )
    print(
        f"offline_view lru candidates={result['candidates']} "
        f"removed={result['removed']} freed_bytes={result['freed_bytes']} "
        f"dry_run={result['dry_run']}"
    )
    for p in list(result.get("paths") or [])[:20]:
        print(f"  {'would remove' if args.dry_run else 'removed'} {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
