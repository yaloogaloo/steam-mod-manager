"""Resolve an offline HTML resource reference against the current authority.

HTML keeps writing ``./assets/...``. After capture finalize, those bytes live
in the Asset Store and the manifest beside ``index.html``. Staging and a
sibling ``assets/`` tree are only pre-finalize / leftover sources.

This module does not scan the Asset Store directory and does not copy bytes
back into ``.info/offline/assets``.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


class CaptureResolveStatus(str, Enum):
    """What an HTML resource reference means against the current authority."""

    RESOLVED = "resolved"
    UNRESOLVED_REFERENCE = "unresolved_reference"
    REMOTE_OR_UNSUPPORTED = "remote_or_unsupported"


@dataclass(frozen=True)
class CapturedResource:
    status: CaptureResolveStatus
    reference: str
    path: Path | None = None


class CapturedResourceResolver:
    """Resolve many refs against one offline ``index.html`` (manifest loaded once)."""

    def __init__(self, index_html: Path) -> None:
        self.index = Path(index_html)
        self._manifest_paths: dict[str, str] | None = None

    def resolve(self, reference: str) -> CapturedResource:
        raw = str(reference or "").strip()
        key = captured_manifest_key(raw)
        if key is None:
            return CapturedResource(
                CaptureResolveStatus.REMOTE_OR_UNSUPPORTED, raw, None
            )

        via_store = self._resolve_manifest(key)
        if via_store is not None:
            return CapturedResource(
                CaptureResolveStatus.RESOLVED,
                key,
                _consumer_path(via_store, key),
            )

        staged = _staging_file(self.index.parent, key)
        if staged is not None:
            return CapturedResource(CaptureResolveStatus.RESOLVED, key, staged)

        sibling = _file_under(self.index.parent, key)
        if sibling is not None:
            return CapturedResource(CaptureResolveStatus.RESOLVED, key, sibling)

        return CapturedResource(
            CaptureResolveStatus.UNRESOLVED_REFERENCE, key, None
        )

    def _resolve_manifest(self, key: str) -> Path | None:
        paths = self._manifest_index()
        digest = paths.get(key)
        if not digest:
            return None
        from core.paths import asset_store_dir
        from services.asset_store import AssetNotFound, AssetStore

        try:
            return AssetStore(root=asset_store_dir()).get_path(digest)
        except AssetNotFound:
            return None

    def _manifest_index(self) -> dict[str, str]:
        if self._manifest_paths is not None:
            return self._manifest_paths
        from core.paths import asset_store_dir
        from services.asset_store import AssetStore
        from services.info_asset_runtime import load_usable_live_manifest

        manifest = load_usable_live_manifest(
            self.index, store=AssetStore(root=asset_store_dir())
        )
        found: dict[str, str] = {}
        if manifest is not None:
            for ref in manifest.assets:
                found[ref.path] = ref.sha256
        self._manifest_paths = found
        return found


def resolve_captured_offline_resource(
    reference: str, index_html: Path
) -> CapturedResource:
    """Resolve one HTML reference. See :class:`CapturedResourceResolver`."""
    return CapturedResourceResolver(index_html).resolve(reference)


def captured_manifest_key(reference: str) -> str | None:
    """Return ``assets/...`` when *reference* is a captured local href.

    ``https``, ``data``, ``javascript``, ``blob``, and any non-``assets``
    relative href return ``None`` (remote or unsupported).
    """
    text = str(reference or "").strip().replace("\\", "/")
    if not text:
        return None
    split = urlsplit(text)
    if split.scheme:
        return None
    path = split.path
    if path.startswith("./"):
        path = path[2:]
    if not path or path.startswith("/"):
        return None
    parts = PurePosixPath(path).parts
    if not parts or ".." in parts or parts[0] != "assets":
        return None
    from services.asset_manifest import ManifestError, validate_manifest_path

    try:
        return validate_manifest_path(PurePosixPath(*parts).as_posix())
    except ManifestError:
        logger.debug("rejected offline resource ref %r", reference)
        return None


def _file_under(root: Path, relative: str) -> Path | None:
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    try:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    except OSError:
        return None
    return None


def _staging_file(offline_root: Path, manifest_key: str) -> Path | None:
    if not manifest_key.startswith("assets/"):
        return None
    from services.offline.staging import capture_assets_dir

    staging = capture_assets_dir(offline_root, create=False)
    if not staging.exists():
        return None
    return _file_under(staging, manifest_key[len("assets/") :])


def _consumer_path(authority_path: Path, manifest_key: str) -> Path:
    """Return *authority_path*, or a suffix alias when the object has none.

    Asset Store objects are content-addressed and have no image suffix.
    Cover install rejects a path with an empty suffix. The alias is a
    cache/temp hardlink (copy if the link fails), not a second SoT and not
    ``.info/offline/assets``.
    """
    suffix = Path(manifest_key).suffix.lower()
    if not suffix or authority_path.suffix.lower() == suffix:
        return authority_path
    from core.paths import cache_temp_dir

    view = cache_temp_dir() / "offline_resource_view" / f"{authority_path.name}{suffix}"
    try:
        view.parent.mkdir(parents=True, exist_ok=True)
        if view.is_file() and view.stat().st_size == authority_path.stat().st_size:
            return view
        if view.exists():
            view.unlink()
        try:
            os.link(authority_path, view)
        except OSError:
            shutil.copy2(authority_path, view)
    except OSError as exc:
        logger.warning("offline resource view failed for %s: %s", manifest_key, exc)
        return authority_path
    return view
