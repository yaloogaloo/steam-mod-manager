"""Status Authority — reduced Mod status model.

ARCHITECTURE RULE
-----------------
User-visible Mod status (only)::

    conflict_status / invalid / abandoned   (user annotation)
    content_status ∈ {healthy, content_missing}  (content_status_eval only)

Not Mod status (separate subsystems)::

    identity_status     — Identity fact (never Conflict / never content)
    deploy_status       — Deploy outcome
    backup_status       — Backup subsystem (detail only)
    offline_status      — Offline subsystem
    Record overlays     — 记录缺失 / 额外部署 (memory-only)

Deleted from Mod status (must not persist on content_status)::

    folder_missing, metadata_missing, backup_invalid, identity_conflict,
    library_status=conflict, file_missing, …
"""

from __future__ import annotations

# --- User Annotation ---
USER_CONFLICT_STATUS = "conflict_status"

# --- System Fact (content) — ONLY these two ---
CONTENT_HEALTHY = "healthy"
CONTENT_CONTENT_MISSING = "content_missing"

# Historical tokens (deleted from Mod status model; kept for import compat /
# migration / diagnostics that still name the old values).
CONTENT_FOLDER_MISSING = "folder_missing"
CONTENT_METADATA_MISSING = "metadata_missing"
CONTENT_BACKUP_INVALID = "backup_invalid"

SUPPORTED_CONTENT_STATUSES = (
    CONTENT_HEALTHY,
    CONTENT_CONTENT_MISSING,
)

# --- Identity Fact (not Mod status) ---
IDENTITY_STATUS_OK = "ok"
IDENTITY_STATUS_CONFLICT = "identity_conflict"
IDENTITY_STATUS_UNRESOLVED = "unresolved"

SUPPORTED_IDENTITY_STATUSES = (
    IDENTITY_STATUS_OK,
    IDENTITY_STATUS_CONFLICT,
    IDENTITY_STATUS_UNRESOLVED,
)

# --- Deploy outcome (not Mod status) ---
DEPLOY_STATUS_NOT_DEPLOYED = "not_deployed"
DEPLOY_STATUS_DEPLOYED = "deployed"
DEPLOY_STATUS_FAILED = "failed"

SUPPORTED_DEPLOY_STATUSES = (
    DEPLOY_STATUS_NOT_DEPLOYED,
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
)

# Memory-only Deployment Record overlays
RECORD_OVERLAY_MISSING_LABEL = "记录缺失"
RECORD_OVERLAY_EXTRA_LABEL = "额外部署"
FORBIDDEN_RECORD_STATUS_COLUMNS = frozenset(
    {
        "extra_deployed",
        "record_missing",
        "relative_status",
        "record_status",
    }
)

# Deleted Mod-status tokens (historical pollution / old model)
DELETED_CONTENT_STATUS_TOKENS = frozenset(
    {
        "folder_missing",
        "metadata_missing",
        "backup_invalid",
        "file_missing",
        "identity_conflict",
        "conflict",
        "identity_unresolved",
        "IDENTITY_CONFLICT",
        "IDENTITY_UNRESOLVED",
        "unknown",
        "missing",
        "normal",
        "imported",
    }
)

# Back-compat names used by older imports — all map away via normalize.
POLLUTION_CONTENT_TOKENS = DELETED_CONTENT_STATUS_TOKENS
POLLUTION_LIBRARY_TOKENS = frozenset(
    {
        "conflict",
        "identity_conflict",
        "IDENTITY_CONFLICT",
        "IDENTITY_UNRESOLVED",
        "identity_unresolved",
        "unresolved",
        "backup_invalid",
    }
)

STATUS_RECOVERY_FLAG = "status_recovery_v1"
STATUS_MODEL_CLEANUP_V2_FLAG = "status_model_cleanup_v2"

CONTENT_STATUS_WRITERS = frozenset(
    {
        "services/content_status_eval.py",
        "core/db_manager.py",
    }
)
IDENTITY_STATUS_WRITERS = frozenset(
    {
        "services/status_recovery.py",
        "services/library_reconcile.py",
        "services/identity_repair.py",
        "services/identity_service.py",
        "core/db_manager.py",
    }
)
CONFLICT_STATUS_WRITERS = frozenset(
    {
        "services/user_annotation.py",
        "ui/mod_detail_panel.py",
        "core/db_manager.py",
    }
)
DEPLOY_STATUS_WRITERS = frozenset(
    {
        "services/deploy.py",
        "core/db_manager.py",
    }
)


def normalize_identity_status(value: str | None) -> str:
    key = str(value or "").strip().lower()
    if key in SUPPORTED_IDENTITY_STATUSES:
        return key
    if key in {"conflict", "identity_conflict", "identity_unresolved"}:
        if key == "identity_unresolved":
            return IDENTITY_STATUS_UNRESOLVED
        return IDENTITY_STATUS_CONFLICT
    return IDENTITY_STATUS_OK


def is_identity_conflict_status(value: str | None) -> bool:
    return normalize_identity_status(value) == IDENTITY_STATUS_CONFLICT


def normalize_deploy_status(value: str | None) -> str:
    key = str(value or "").strip().lower()
    if key in SUPPORTED_DEPLOY_STATUSES:
        return key
    return DEPLOY_STATUS_NOT_DEPLOYED


def normalize_content_axis(value: str | None) -> str:
    """
    Read-path normalize onto the reduced content axis.

    Only ``healthy`` / ``content_missing`` are legal. Illegal / deleted tokens
    collapse to ``healthy`` (never invent ``content_missing`` via mapping —
    Status Recovery re-evaluates from disk).
    """
    key = str(value or "").strip().lower()
    if key in SUPPORTED_CONTENT_STATUSES:
        return key
    return CONTENT_HEALTHY
