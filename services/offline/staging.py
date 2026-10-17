"""Capture asset staging under ``cache/temp/offline_staging``.

LIVE ``.info`` keeps ``index.html`` + ``manifest.json`` + ``internal_id``.
Offline bytes go to Asset Store. Capture may write temporary files here;
finalize ingests them and deletes the staging tree.

HTML still references ``./assets/...``; those paths are relative names
in the manifest, not a durable ``.info/assets`` tree.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

STAGING_SUBDIR = "offline_staging"


def capture_staging_root(offline_root: Path | str) -> Path:
    """``cache/temp/offline_staging/<key>`` for one LIVE offline root."""
    from core.paths import cache_temp_dir

    resolved = Path(offline_root).resolve()
    key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    return cache_temp_dir() / STAGING_SUBDIR / key


def capture_assets_dir(offline_root: Path | str, *, create: bool = True) -> Path:
    """Staging ``assets/`` for capture writers. Never ``.info/assets``."""
    assets = capture_staging_root(offline_root) / "assets"
    if create:
        assets.mkdir(parents=True, exist_ok=True)
    return assets


def is_live_info_root(offline_root: Path | str) -> bool:
    """True when *offline_root* lives under a LIVE ``.info`` directory."""
    return ".info" in Path(offline_root).parts


def resolve_capture_assets_dir(
    offline_root: Path | str, *, create: bool = True
) -> Path:
    """Capture bytes for *offline_root*.

    LIVE ``.info`` (including ``.info/offline``) uses
    ``cache/temp/offline_staging``. Other output dirs keep a sibling
    ``assets/`` (unit tests / non-library writers).
    """
    root = Path(offline_root)
    if is_live_info_root(root):
        return capture_assets_dir(root, create=create)
    assets = root / "assets"
    if create:
        assets.mkdir(parents=True, exist_ok=True)
    return assets


def reset_capture_assets_dir(offline_root: Path | str) -> Path:
    """Empty capture assets dir. Removes leftover LIVE ``.info/assets``."""
    root = Path(offline_root)
    leftover = root / "assets"
    if is_live_info_root(root) and leftover.exists():
        shutil.rmtree(leftover, ignore_errors=True)
    assets = resolve_capture_assets_dir(root, create=False)
    if assets.exists():
        shutil.rmtree(assets, ignore_errors=True)
    return resolve_capture_assets_dir(root, create=True)


def cleanup_capture_staging(offline_root: Path | str) -> None:
    """Delete the staging tree for *offline_root*. Best-effort."""
    if not is_live_info_root(offline_root):
        # Non-.info outputs do not use cache/temp staging.
        return
    root = capture_staging_root(offline_root)
    if not root.exists():
        return
    try:
        shutil.rmtree(root, ignore_errors=True)
    except OSError as exc:
        logger.warning("capture staging cleanup failed for %s: %s", root, exc)
