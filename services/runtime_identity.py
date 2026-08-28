"""Runtime identity helpers for verifying loaded module paths."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple


class ArchiveModuleIdentity(NamedTuple):
    path: str
    mtime: str


def get_archive_module_identity() -> ArchiveModuleIdentity:
    """Return resolved path and UTC mtime for ``services.importers.archive``."""
    import services.importers.archive as archive_mod

    path = Path(archive_mod.__file__).resolve()
    try:
        mtime = datetime.fromtimestamp(
            path.stat().st_mtime,
            tz=timezone.utc,
        ).isoformat()
    except OSError:
        mtime = "unknown"
    return ArchiveModuleIdentity(str(path), mtime)


def log_archive_runtime_identity(
    logger: logging.Logger,
    *,
    prefix: str = "[RUNTIME]",
) -> ArchiveModuleIdentity:
    """Log archive module path + mtime for startup / deploy diagnostics."""
    ident = get_archive_module_identity()
    logger.info("%s archive_module_path=%s", prefix, ident.path)
    logger.info("%s archive_module_mtime=%s", prefix, ident.mtime)
    return ident
