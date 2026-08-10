"""Shared helpers for Nexus / GitHub offline page providers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from core.db_manager import get_db
from core.mod_platform import OFFLINE_STATUS_ARCHIVED, OFFLINE_STATUS_FAILED
from core.paths import default_mod_library
from services.file_ops import ModFileManager
from services.importers.materialize import find_managed_mod_path
from services.offline.base import OfflineUpdateResult
from services.offline.browser_snapshot.manager import (
    BrowserSnapshotProvider,
    BrowserSnapshotResult,
)

OFFLINE_SUBDIR = "offline"

# Optional injection for tests.
_SNAPSHOT_FACTORY: Callable[[], BrowserSnapshotProvider] | None = None


def set_browser_snapshot_factory(
    factory: Callable[[], BrowserSnapshotProvider] | None,
) -> None:
    """Test helper: override ``BrowserSnapshotProvider`` construction."""
    global _SNAPSHOT_FACTORY
    _SNAPSHOT_FACTORY = factory


def _make_provider() -> BrowserSnapshotProvider:
    if _SNAPSHOT_FACTORY is not None:
        return _SNAPSHOT_FACTORY()
    return BrowserSnapshotProvider()


def run_browser_offline_snapshot(
    *,
    mod_id: str | int,
    provider_name: str,
    managed_path: str | Path | None = None,
    library_root: str | Path | None = None,
    source_url: str = "",
    require_source_url: bool = True,
) -> OfflineUpdateResult:
    """
    Resolve Mod folder, snapshot *source_url* into ``.info/offline/``, update DB.

    On total failure sets ``offline_status=failed`` and raises ``RuntimeError``.
    """
    mid = str(mod_id).strip()
    root = Path(library_root) if library_root else default_mod_library()
    path = Path(managed_path) if managed_path else find_managed_mod_path(root, mid)
    if path is None:
        raise FileNotFoundError(f"Managed Mod folder not found for mod_id={mid}")

    info = get_db().get_mod_display_info(mid)
    if info is None:
        raise ValueError(f"Mod not found in database: {mid}")

    url = (source_url or info.source_url or "").strip()
    if not url and require_source_url:
        get_db().update_mod_offline_status(
            mid,
            status=OFFLINE_STATUS_FAILED,
            provider=provider_name,
        )
        raise ValueError(f"Mod has no source_url: mod_id={mid}")

    mgr = ModFileManager(root)
    info_dir = mgr.ensure_info_dir(path)
    output_dir = info_dir / OFFLINE_SUBDIR

    result: BrowserSnapshotResult = _make_provider().snapshot(url, output_dir)

    if not result.success or not result.html_path.is_file():
        error = result.error or "Browser snapshot failed"
        get_db().update_mod_offline_status(
            mid,
            status=OFFLINE_STATUS_FAILED,
            provider=provider_name,
        )
        raise RuntimeError(error)

    get_db().update_mod_offline_status(
        mid,
        status=OFFLINE_STATUS_ARCHIVED,
        provider=provider_name,
    )
    return OfflineUpdateResult(
        mod_id=mid,
        index_path=result.html_path,
        status=OFFLINE_STATUS_ARCHIVED,
        provider=provider_name,
        error="",
    )
