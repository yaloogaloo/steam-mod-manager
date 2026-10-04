"""Canonical deployed membership.

One authority: ``mods.deploy_status = deployed``, already projected onto
library rows as ``deployed``. Order files and external projections
(launcher descriptors, playsets, pak UUIDs, ``used_mods.txt``) do not
decide who belongs to this set.

Identity is a separate axis. A deployed row with a missing or non-UUID
``internal_id`` stays a member. It is omitted from
``canonical_deployed_internal_ids`` and logged. That function never
substitutes ``mod_id``, ``workspace_id``, ``token``, a launcher id, or a
pak UUID.
"""

from __future__ import annotations

import logging
from typing import Iterable

from services.deploy_identity import is_frozen_internal_uuid

logger = logging.getLogger(__name__)

_DEPLOYED = "deployed"


def entry_is_deployed(entry: object) -> bool:
    """True when this row's deployment flag is deployed.

    Reads the flag the library projection already computed from
    ``mods.deploy_status``. Does not open launcher files, pak metadata,
    or load-order JSON.
    """
    if isinstance(entry, dict):
        if "deployed" in entry and entry.get("deployed") is not None:
            return bool(entry.get("deployed"))
        return str(entry.get("deploy_status") or "").strip().lower() == _DEPLOYED
    deployed = getattr(entry, "deployed", None)
    if deployed is not None:
        return bool(deployed)
    status = getattr(entry, "deploy_status", None)
    if status is None:
        return False
    return str(status).strip().lower() == _DEPLOYED


def _frozen_internal_id(entry: object) -> str:
    """Frozen UUID from ``internal_id`` only.

    Does not read ``token``, ``mod_id``, ``workspace_id``, launcher ids,
    or pak UUIDs. Empty when the field is missing or not a Frozen UUID.
    """
    if isinstance(entry, dict):
        raw = entry.get("internal_id")
    else:
        raw = getattr(entry, "internal_id", None)
    text = str(raw or "").strip()
    if is_frozen_internal_uuid(text):
        return text
    return ""


def canonical_deployed_internal_ids(entries: Iterable[object]) -> list[str]:
    """Deployed Frozen UUIDs, stable and unique, in *entries* order.

    *entries* is the current game projection (or the same rows a backend
    already loaded for that game). This function does not query order
    files or external projections, and it does not invent a second
    deployed set.

    A deployed row whose ``internal_id`` is missing or not a Frozen UUID
    is skipped and logged. It remains a member via :func:`entry_is_deployed`.
    """
    out: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not entry_is_deployed(entry):
            continue
        frozen = _frozen_internal_id(entry)
        if not frozen:
            logger.warning(
                "canonical identity skipped deployed row: "
                "internal_id is missing or not a Frozen UUID"
            )
            continue
        if frozen in seen:
            continue
        seen.add(frozen)
        out.append(frozen)
    return out
