"""Canonical offline-page path resolution for OPEN operations.

Preferred layout (all platforms)::

    1. ``<mod>/.info/offline/index.html``   (mod.io / GitHub / Nexus / modern)
    2. ``<mod>/.info/index.html``           (legacy Steam archives)
    3. None

Legacy ``info/`` (without the leading dot) is checked the same way for
old libraries. Optional metadata / sidecar paths are last-resort only and
must never outrank the preferred filesystem layouts.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

OFFLINE_INDEX_NAME = "index.html"
OFFLINE_SNAPSHOT_DIR = "offline"

CANONICAL_OFFLINE_REL = f"{INFO_DIR_NAME}/{OFFLINE_SNAPSHOT_DIR}/{OFFLINE_INDEX_NAME}"
LEGACY_STEAM_OFFLINE_REL = f"{INFO_DIR_NAME}/{OFFLINE_INDEX_NAME}"


def _usable_file(path: Path) -> Path | None:
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path.resolve()
    except OSError:
        return None
    return None


def _info_dir_names() -> tuple[str, ...]:
    return (INFO_DIR_NAME, LEGACY_INFO_DIR_NAME)


def resolve_offline_page(managed_path: str | Path) -> Path | None:
    """
    Canonical offline-page resolver used by every UI \"open offline\" action.

    Order (hard contract)::

        1. ``.info/offline/index.html`` if present and non-empty
        2. ``.info/index.html`` if present and non-empty
        3. ``None``
    """
    root = Path(managed_path)

    for info_name in _info_dir_names():
        found = _usable_file(
            root / info_name / OFFLINE_SNAPSHOT_DIR / OFFLINE_INDEX_NAME
        )
        if found is not None:
            return found

    for info_name in _info_dir_names():
        found = _usable_file(root / info_name / OFFLINE_INDEX_NAME)
        if found is not None:
            return found

    return None


def iter_offline_sidecar_candidates(
    managed_path: str | Path,
    *,
    offline_page_path: str | None = None,
) -> Iterator[Path]:
    """
    Extra candidates for existence / import detection (not for preferred OPEN).

    Includes metadata paths and non-index html/mhtml under ``.info/``.
    Never yields ahead of :func:`resolve_offline_page` results.
    """
    root = Path(managed_path)
    seen: set[str] = set()

    def emit(path: Path) -> Iterator[Path]:
        key = str(path)
        if key not in seen:
            seen.add(key)
            yield path

    raw = str(offline_page_path or "").strip()
    if raw:
        path = Path(raw).expanduser()
        yield from emit(path)
        if not path.is_absolute():
            yield from emit(root / path)

    for info_name in _info_dir_names():
        info_dir = root / info_name
        offline_dir = info_dir / OFFLINE_SNAPSHOT_DIR
        for suffix in (".mhtml", ".mht", ".html", ".htm"):
            yield from emit(offline_dir / f"index{suffix}")
        try:
            if offline_dir.is_dir():
                for path in sorted(offline_dir.iterdir()):
                    if path.suffix.lower() in {".mhtml", ".mht", ".html", ".htm"}:
                        yield from emit(path)
        except OSError:
            pass
        try:
            if info_dir.is_dir():
                for path in sorted(info_dir.iterdir()):
                    if path.suffix.lower() in {".mhtml", ".mht", ".html", ".htm"}:
                        yield from emit(path)
        except OSError:
            continue


def iter_offline_page_candidates(
    managed_path: str | Path,
    *,
    offline_page_path: str | None = None,
) -> Iterator[Path]:
    """Yield candidates in the same preference order as resolution."""
    root = Path(managed_path)
    for info_name in _info_dir_names():
        yield root / info_name / OFFLINE_SNAPSHOT_DIR / OFFLINE_INDEX_NAME
    for info_name in _info_dir_names():
        yield root / info_name / OFFLINE_INDEX_NAME
    yield from iter_offline_sidecar_candidates(
        root, offline_page_path=offline_page_path
    )


def resolve_offline_page_path(
    managed_path: str | Path,
    *,
    offline_page_path: str | None = None,
) -> Path | None:
    """
    Resolve an existing offline page for open / existence checks.

    Always applies the canonical preference first. Metadata / sidecar paths
    are consulted only when neither canonical index exists (Nexus oddballs).
    """
    preferred = resolve_offline_page(managed_path)
    if preferred is not None:
        return preferred

    root = Path(managed_path)
    for candidate in iter_offline_sidecar_candidates(
        root, offline_page_path=offline_page_path
    ):
        text = str(candidate or "").strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = root / path
        found = _usable_file(path)
        if found is not None:
            return found
    return None


def offline_page_file_exists(
    managed_path: str | Path,
    *,
    offline_page_path: str | None = None,
) -> bool:
    """True when an offline page file exists (existence only)."""
    return (
        resolve_offline_page_path(
            managed_path, offline_page_path=offline_page_path
        )
        is not None
    )
