"""Deploy-only identity resolver: Frozen UUID in, SQL PK out.

Business entry for Deploy is Frozen ``mods.internal_id`` (UUID).
Digit SQLite ``mods.mod_id`` is not a legal deploy token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

INVALID_INTERNAL_UUID = "Invalid internal UUID"

_FROZEN_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class DeployIdentityError(ValueError):
    """Deploy entry token is not a Frozen Internal UUID."""

    def __init__(self, message: str = INVALID_INTERNAL_UUID) -> None:
        super().__init__(message)


@dataclass(frozen=True)
class DeployEntity:
    """Resolved Deploy identity. ``internal_id`` is never ``str(mod_pk)``."""

    internal_id: str
    mod_pk: int
    workspace_id: str = ""


def is_frozen_internal_uuid(token: str | int | None) -> bool:
    """True when *token* is a hyphenated Frozen UUID (not a digit PK)."""
    text = str(token or "").strip()
    if not text or text.isdigit():
        return False
    return bool(_FROZEN_UUID_RE.fullmatch(text))


def resolve_deploy_entity(internal_id: str | int, *, db: Any) -> DeployEntity:
    """
    UUID → ``{internal_id, mod_pk, workspace_id}``.

    Rejects empty tokens, digit PK handles (``\"296\"``), and mixed UUID/PK
    tokens. Does not call ``resolve_mod_pk`` (that helper still accepts PK).
    """
    token = str(internal_id or "").strip()
    if not is_frozen_internal_uuid(token):
        raise DeployIdentityError(INVALID_INTERNAL_UUID)
    try:
        found = db.find_mod_by_internal_id(token)
    except Exception as exc:  # noqa: BLE001
        raise DeployIdentityError(INVALID_INTERNAL_UUID) from exc
    pk_text = str(found or "").strip()
    if not pk_text.isdigit():
        raise DeployIdentityError(INVALID_INTERNAL_UUID)
    mod_pk = int(pk_text)
    workspace_id = ""
    try:
        display = db.get_mod_display_info(mod_pk)
        if display is not None:
            workspace_id = str(display.workspace_id or "").strip()
    except Exception:  # noqa: BLE001
        workspace_id = ""
    return DeployEntity(
        internal_id=token,
        mod_pk=mod_pk,
        workspace_id=workspace_id,
    )


def frozen_internal_id_for_pk(mod_pk: int | str, *, db: Any) -> str:
    """DAL: SQLite PK → Frozen UUID. Empty when missing or not a UUID."""
    pk = str(mod_pk or "").strip()
    if not pk.isdigit():
        return ""
    try:
        row = db.get_mod_backup_row(pk) or {}
    except Exception:  # noqa: BLE001
        return ""
    frozen = str(row.get("internal_id") or "").strip()
    if not is_frozen_internal_uuid(frozen):
        return ""
    return frozen
