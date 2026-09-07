"""Authority for ``mods.updated_at`` — Library 「最近修改」 sort field.

ARCHITECTURE RULE
-----------------
``updated_at`` means: a **user / business object** change time.

Legal::

    user_edit     — metadata, tags, flags, enable/disable, conflict annotation
    user_import   — Import lifecycle creates or first-binds a Mod
    user_create   — IdentityService create under Import (new row)

Illegal (must never bump ``updated_at``)::

    recovery, reconcile, identity_repair, content_scan, content_eval,
    backup, sync, migration, deploy, offline

Any ``DatabaseManager`` write that sets ``mods.updated_at`` must call
:func:`validate_updated_at_reason` with an explicit legal reason.
"""

from __future__ import annotations

LEGAL_UPDATED_AT_REASONS: frozenset[str] = frozenset(
    {
        "user_edit",
        "user_import",
        "user_create",
    }
)

FORBIDDEN_UPDATED_AT_REASONS: frozenset[str] = frozenset(
    {
        "recovery",
        "reconcile",
        "identity_repair",
        "content_scan",
        "content_eval",
        "backup",
        "sync",
        "migration",
        "deploy",
        "offline",
        "status_scan",
        "folder_presence",
    }
)


class UpdatedAtAuthorityError(ValueError):
    """Raised when a caller attempts an illegal or missing ``updated_at`` reason."""


def validate_updated_at_reason(reason: str | None) -> str:
    """
    Return a normalized legal reason or raise :class:`UpdatedAtAuthorityError`.

    Empty / unknown / forbidden reasons all fail — no silent defaults.
    """
    key = str(reason or "").strip()
    if not key:
        raise UpdatedAtAuthorityError(
            "mods.updated_at touch requires an explicit reason"
        )
    if key in FORBIDDEN_UPDATED_AT_REASONS:
        raise UpdatedAtAuthorityError(
            f"illegal mods.updated_at reason={key!r} "
            f"(forbidden system maintenance path)"
        )
    if key not in LEGAL_UPDATED_AT_REASONS:
        raise UpdatedAtAuthorityError(
            f"unknown mods.updated_at reason={key!r}; "
            f"allowed={sorted(LEGAL_UPDATED_AT_REASONS)}"
        )
    return key


def assert_reason_not_forbidden(reason: str | None) -> None:
    """Fail fast if *reason* is explicitly forbidden (even before legality)."""
    key = str(reason or "").strip()
    if key in FORBIDDEN_UPDATED_AT_REASONS:
        raise UpdatedAtAuthorityError(
            f"illegal mods.updated_at reason={key!r}"
        )
