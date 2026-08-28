"""Backward-compatible re-exports — Local File vs Deployment domains."""

from __future__ import annotations

from services.local_file_index import (
    META_CONTENT_HASH,
    LocalReconcileResult as ArchiveReconcileResult,
    ReplacementCandidate,
    has_local_mod_payload,
    reconcile_local_files,
    validate_mod_files,
)

# Local File Domain (library refresh / reconcile)
reconcile_archive_source = reconcile_local_files

# Deployment Domain — library code must not import these for refresh paths.
from services.mod_source_integrity import (  # noqa: E402
    has_deployable_source,
    reconcile_source,
)

__all__ = [
    "META_CONTENT_HASH",
    "ArchiveReconcileResult",
    "ReplacementCandidate",
    "has_deployable_source",
    "has_local_mod_payload",
    "reconcile_archive_source",
    "reconcile_local_files",
    "reconcile_source",
    "validate_mod_files",
]
