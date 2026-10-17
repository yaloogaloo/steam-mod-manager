"""Unified deployment transaction lifecycle (status + phase dual-axis).

Wire format on disk remains ``deploy_transaction.json`` fields ``status`` /
``phase``. Writers must go through :func:`transition` + :func:`to_wire` (or
:func:`persist_lifecycle_transaction`) — never invent ad-hoc status/phase
strings in business code.

DB ``mods.deploy_status`` and runtime UI ``deployment_status`` are separate
outcome axes and are intentionally not folded into this enum.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Protocol


class DeploymentLifecycleState(str, Enum):
    CREATED = "CREATED"
    PREPARED = "PREPARED"
    BACKUP_DONE = "BACKUP_DONE"
    COPYING = "COPYING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ROLLBACK_REQUIRED = "ROLLBACK_REQUIRED"
    ROLLED_BACK = "ROLLED_BACK"


# Disk wire values (unchanged for recovery compatibility)
TXN_PREPARED = "prepared"
TXN_BACKUP_DONE = "backup_done"
TXN_DEPLOYED = "deployed"
TXN_FAILED = "failed"

PHASE_BEGIN = "BEGIN"
PHASE_BACKUP_DONE = "BACKUP_DONE"
PHASE_COPY_DONE = "COPY_DONE"
PHASE_MANIFEST_DONE = "MANIFEST_DONE"
PHASE_COMMITTED = "COMMITTED"
PHASE_ROLLBACK = "ROLLBACK"


class DeploymentLifecycleError(ValueError):
    """Illegal lifecycle transition or unresolvable wire pair."""


# None = no prior state (treat as CREATED)
_ALLOWED: dict[DeploymentLifecycleState | None, frozenset[DeploymentLifecycleState]] = {
    None: frozenset(
        {
            DeploymentLifecycleState.CREATED,
            DeploymentLifecycleState.PREPARED,
            DeploymentLifecycleState.FAILED,
        }
    ),
    DeploymentLifecycleState.CREATED: frozenset(
        {
            DeploymentLifecycleState.PREPARED,
            DeploymentLifecycleState.FAILED,
            DeploymentLifecycleState.CREATED,
        }
    ),
    DeploymentLifecycleState.PREPARED: frozenset(
        {
            DeploymentLifecycleState.BACKUP_DONE,
            DeploymentLifecycleState.FAILED,
            DeploymentLifecycleState.ROLLBACK_REQUIRED,
            DeploymentLifecycleState.ROLLED_BACK,
            DeploymentLifecycleState.PREPARED,
        }
    ),
    DeploymentLifecycleState.BACKUP_DONE: frozenset(
        {
            DeploymentLifecycleState.COPYING,
            DeploymentLifecycleState.VERIFYING,
            DeploymentLifecycleState.COMPLETED,
            DeploymentLifecycleState.FAILED,
            DeploymentLifecycleState.ROLLBACK_REQUIRED,
            DeploymentLifecycleState.ROLLED_BACK,
            DeploymentLifecycleState.BACKUP_DONE,
        }
    ),
    DeploymentLifecycleState.COPYING: frozenset(
        {
            DeploymentLifecycleState.VERIFYING,
            DeploymentLifecycleState.COMPLETED,
            DeploymentLifecycleState.FAILED,
            DeploymentLifecycleState.ROLLBACK_REQUIRED,
            DeploymentLifecycleState.ROLLED_BACK,
            DeploymentLifecycleState.COPYING,
        }
    ),
    DeploymentLifecycleState.VERIFYING: frozenset(
        {
            DeploymentLifecycleState.COMPLETED,
            DeploymentLifecycleState.FAILED,
            DeploymentLifecycleState.ROLLBACK_REQUIRED,
            DeploymentLifecycleState.ROLLED_BACK,
            DeploymentLifecycleState.VERIFYING,
        }
    ),
    DeploymentLifecycleState.COMPLETED: frozenset(
        {DeploymentLifecycleState.COMPLETED}
    ),
    DeploymentLifecycleState.FAILED: frozenset(
        {
            DeploymentLifecycleState.FAILED,
            DeploymentLifecycleState.ROLLED_BACK,
            DeploymentLifecycleState.ROLLBACK_REQUIRED,
        }
    ),
    DeploymentLifecycleState.ROLLBACK_REQUIRED: frozenset(
        {
            DeploymentLifecycleState.ROLLED_BACK,
            DeploymentLifecycleState.FAILED,
            DeploymentLifecycleState.ROLLBACK_REQUIRED,
        }
    ),
    DeploymentLifecycleState.ROLLED_BACK: frozenset(
        {DeploymentLifecycleState.ROLLED_BACK}
    ),
}


def transition(
    current: DeploymentLifecycleState | None,
    target: DeploymentLifecycleState,
) -> DeploymentLifecycleState:
    """
    Validate and return ``target``.

    Idempotent same-state transitions are allowed. Illegal jumps raise
    :class:`DeploymentLifecycleError`.
    """
    if not isinstance(target, DeploymentLifecycleState):
        raise DeploymentLifecycleError(f"invalid target state: {target!r}")
    allowed = _ALLOWED.get(current)
    if allowed is None or target not in allowed:
        cur = current.value if isinstance(current, DeploymentLifecycleState) else current
        raise DeploymentLifecycleError(
            f"illegal deployment lifecycle transition: {cur} → {target.value}"
        )
    return target


def to_wire(state: DeploymentLifecycleState) -> tuple[str, str]:
    """Map lifecycle state → persistent ``(status, phase)`` wire pair."""
    if state is DeploymentLifecycleState.CREATED:
        return ("", "")
    if state is DeploymentLifecycleState.PREPARED:
        return (TXN_PREPARED, PHASE_BEGIN)
    if state is DeploymentLifecycleState.BACKUP_DONE:
        return (TXN_BACKUP_DONE, PHASE_BACKUP_DONE)
    if state is DeploymentLifecycleState.COPYING:
        return (TXN_BACKUP_DONE, PHASE_COPY_DONE)
    if state is DeploymentLifecycleState.VERIFYING:
        return (TXN_BACKUP_DONE, PHASE_MANIFEST_DONE)
    if state is DeploymentLifecycleState.COMPLETED:
        return (TXN_DEPLOYED, PHASE_COMMITTED)
    if state is DeploymentLifecycleState.FAILED:
        return (TXN_FAILED, "")
    if state is DeploymentLifecycleState.ROLLBACK_REQUIRED:
        # Durable marker still uses interruptible statuses; phase omitted.
        return (TXN_BACKUP_DONE, "")
    if state is DeploymentLifecycleState.ROLLED_BACK:
        return (TXN_FAILED, PHASE_ROLLBACK)
    raise DeploymentLifecycleError(f"unmapped state: {state!r}")


def resolve_deployment_state(
    status: str = "",
    phase: str = "",
    *,
    active_transaction: bool | None = None,
) -> DeploymentLifecycleState:
    """
    Unify legacy ``status`` + ``phase`` into a single lifecycle state.

    When ``active_transaction is False``, interruptible in-flight states resolve
    to :attr:`ROLLBACK_REQUIRED` (startup recovery judgment).
    """
    s = str(status or "").strip().lower()
    p = str(phase or "").strip().upper()

    if not s and not p:
        base = DeploymentLifecycleState.CREATED
    elif s == TXN_PREPARED:
        base = DeploymentLifecycleState.PREPARED
    elif s == TXN_BACKUP_DONE:
        if p == PHASE_COPY_DONE:
            base = DeploymentLifecycleState.COPYING
        elif p == PHASE_MANIFEST_DONE:
            base = DeploymentLifecycleState.VERIFYING
        else:
            base = DeploymentLifecycleState.BACKUP_DONE
    elif s == TXN_DEPLOYED:
        base = DeploymentLifecycleState.COMPLETED
    elif s == TXN_FAILED:
        if p == PHASE_ROLLBACK:
            base = DeploymentLifecycleState.ROLLED_BACK
        else:
            base = DeploymentLifecycleState.FAILED
    else:
        raise DeploymentLifecycleError(
            f"unresolvable deployment wire state status={status!r} phase={phase!r}"
        )

    if active_transaction is False and base in (
        DeploymentLifecycleState.PREPARED,
        DeploymentLifecycleState.BACKUP_DONE,
        DeploymentLifecycleState.COPYING,
        DeploymentLifecycleState.VERIFYING,
    ):
        return DeploymentLifecycleState.ROLLBACK_REQUIRED
    return base


def resolve_from_transaction(
    txn: Mapping[str, Any] | None,
    *,
    active_transaction: bool | None = None,
) -> DeploymentLifecycleState:
    if not txn:
        return DeploymentLifecycleState.CREATED
    return resolve_deployment_state(
        str(txn.get("status") or ""),
        str(txn.get("phase") or ""),
        active_transaction=active_transaction,
    )


class _TxnWriter(Protocol):
    def write_transaction(
        self,
        *,
        status: str,
        targets: list[str],
        backups: list[Mapping[str, Any]],
        mod_id: str = "",
        phase: str = "",
    ) -> Any: ...


def persist_lifecycle_transaction(
    writer: _TxnWriter,
    target: DeploymentLifecycleState,
    *,
    current: DeploymentLifecycleState | None = None,
    targets: list[str],
    backups: list[Mapping[str, Any]],
    mod_id: str = "",
) -> DeploymentLifecycleState:
    """
    Validate ``current → target`` then persist wire ``status``/``phase``.

    Business writers should call this instead of raw ``write_transaction``.
    """
    state = transition(current, target)
    status, phase = to_wire(state)
    writer.write_transaction(
        status=status,
        targets=targets,
        backups=backups,
        mod_id=mod_id,
        phase=phase,
    )
    return state


__all__ = [
    "DeploymentLifecycleError",
    "DeploymentLifecycleState",
    "PHASE_BACKUP_DONE",
    "PHASE_BEGIN",
    "PHASE_COMMITTED",
    "PHASE_COPY_DONE",
    "PHASE_MANIFEST_DONE",
    "PHASE_ROLLBACK",
    "TXN_BACKUP_DONE",
    "TXN_DEPLOYED",
    "TXN_FAILED",
    "TXN_PREPARED",
    "persist_lifecycle_transaction",
    "resolve_deployment_state",
    "resolve_from_transaction",
    "to_wire",
    "transition",
]
