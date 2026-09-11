"""Deploy transaction lifecycle helpers — active guard + phase logging.

Phases (log-only / optional txn.phase field) are owned by
``services.deployment_lifecycle``. This module keeps the in-memory active-txn
guard and structured phase logging.

Active deploy transactions must not be recovered by startup reconcile.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from services.deployment_lifecycle import (
    PHASE_BACKUP_DONE,
    PHASE_BEGIN,
    PHASE_COMMITTED,
    PHASE_COPY_DONE,
    PHASE_MANIFEST_DONE,
    PHASE_ROLLBACK,
)

logger = logging.getLogger(__name__)

_ROLLBACK_NOTE = "rollback completed"
_INTERRUPTED_MSG = "interrupted deploy rolled back from transaction"

_lock = threading.RLock()
# normalized managed path → internal_id
_active_managed: dict[str, str] = {}


def _norm_managed(managed: str | Path) -> str:
    try:
        return str(Path(managed).expanduser().resolve()).replace("\\", "/").lower()
    except OSError:
        return str(managed).replace("\\", "/").lower()


def register_active_deploy_transaction(
    managed: str | Path, *, internal_id: str = ""
) -> None:
    key = _norm_managed(managed)
    mid = str(internal_id or "").strip()
    with _lock:
        _active_managed[key] = mid
    logger.info(
        "[DEPLOY_TXN] register active managed=%s internal_id=%s", key, mid or "-"
    )


def unregister_active_deploy_transaction(managed: str | Path) -> None:
    key = _norm_managed(managed)
    with _lock:
        _active_managed.pop(key, None)
    logger.info("[DEPLOY_TXN] unregister active managed=%s", key)


def is_active_deploy_transaction(managed: str | Path) -> bool:
    key = _norm_managed(managed)
    with _lock:
        return key in _active_managed


def log_txn_phase(
    phase: str,
    *,
    internal_id: str = "",
    managed: str | Path | None = None,
    extra: str = "",
) -> None:
    parts = [
        f"phase={phase}",
        f"internal_id={internal_id}" if internal_id else "",
        f"managed={managed}" if managed is not None else "",
        extra,
    ]
    logger.info("[DEPLOY_TXN] %s", " ".join(p for p in parts if p))


def compose_recover_deploy_error(existing: str | None) -> str:
    """
    Preserve original deploy_error; only append rollback completion note.

    Never replace a concrete failure (manifest / OSError / validation) with the
    generic interrupted-transaction string.
    """
    prev = str(existing or "").strip()
    note = _ROLLBACK_NOTE
    if not prev:
        return f"{_INTERRUPTED_MSG} | {note}"
    if note in prev:
        return prev
    if prev == _INTERRUPTED_MSG:
        return f"{prev} | {note}"
    return f"{prev} | {note}"


__all__ = [
    "PHASE_BACKUP_DONE",
    "PHASE_BEGIN",
    "PHASE_COMMITTED",
    "PHASE_COPY_DONE",
    "PHASE_MANIFEST_DONE",
    "PHASE_ROLLBACK",
    "compose_recover_deploy_error",
    "is_active_deploy_transaction",
    "log_txn_phase",
    "register_active_deploy_transaction",
    "unregister_active_deploy_transaction",
]
