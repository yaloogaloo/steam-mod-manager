"""DeploymentLifecycleState contract — unified status/phase dual-axis."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services.deployment_lifecycle import (
    DeploymentLifecycleError,
    DeploymentLifecycleState as S,
    persist_lifecycle_transaction,
    resolve_deployment_state,
    to_wire,
    transition,
)

ROOT = Path(__file__).resolve().parents[1]


def test_all_happy_path_transitions_legal() -> None:
    cur: S | None = None
    for nxt in (S.PREPARED, S.BACKUP_DONE, S.COPYING, S.VERIFYING, S.COMPLETED):
        cur = transition(cur, nxt)
    assert cur is S.COMPLETED
    # Idempotent complete
    assert transition(S.COMPLETED, S.COMPLETED) is S.COMPLETED


def test_illegal_jump_fails() -> None:
    with pytest.raises(DeploymentLifecycleError):
        transition(S.CREATED, S.COMPLETED)
    with pytest.raises(DeploymentLifecycleError):
        transition(S.PREPARED, S.VERIFYING)
    with pytest.raises(DeploymentLifecycleError):
        transition(S.COMPLETED, S.PREPARED)


def test_status_phase_resolve_unique() -> None:
    cases = [
        ("", "", S.CREATED),
        ("prepared", "BEGIN", S.PREPARED),
        ("backup_done", "BACKUP_DONE", S.BACKUP_DONE),
        ("backup_done", "COPY_DONE", S.COPYING),
        ("backup_done", "MANIFEST_DONE", S.VERIFYING),
        ("deployed", "COMMITTED", S.COMPLETED),
        ("failed", "", S.FAILED),
        ("failed", "ROLLBACK", S.ROLLED_BACK),
    ]
    for status, phase, expected in cases:
        assert resolve_deployment_state(status, phase) is expected
        # Round-trip wire for durable states (not CREATED / ROLLBACK_REQUIRED)
        if expected not in (S.CREATED, S.ROLLBACK_REQUIRED):
            w_status, w_phase = to_wire(expected)
            assert resolve_deployment_state(w_status, w_phase) is expected


def test_inactive_interruptible_resolves_rollback_required() -> None:
    assert (
        resolve_deployment_state(
            "backup_done", "COPY_DONE", active_transaction=False
        )
        is S.ROLLBACK_REQUIRED
    )
    assert (
        resolve_deployment_state("prepared", "BEGIN", active_transaction=False)
        is S.ROLLBACK_REQUIRED
    )


def test_writers_do_not_assign_raw_status_phase_strings() -> None:
    """Production txn writers must call persist_lifecycle_transaction."""
    files = [
        ROOT / "services" / "backup_manager.py",
        ROOT / "services" / "deploy.py",
    ]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = ""
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name != "write_transaction":
                continue
            # Allowed only inside deployment_lifecycle.persist via attribute
            # on self — still forbid keyword status= with string constants
            # at call sites in these modules except the low-level method body.
            for kw in node.keywords:
                if kw.arg in ("status", "phase") and isinstance(
                    kw.value, ast.Constant
                ):
                    # Direct string literals at call site are forbidden.
                    pytest.fail(
                        f"{path.name}:{node.lineno} write_transaction uses "
                        f"literal {kw.arg}={kw.value.value!r}"
                    )


def test_persist_uses_lifecycle_not_raw_strings(tmp_path: Path) -> None:
    class _W:
        def __init__(self) -> None:
            self.last: dict = {}

        def write_transaction(self, **kwargs):  # type: ignore[no-untyped-def]
            self.last = kwargs

    w = _W()
    persist_lifecycle_transaction(
        w,
        S.PREPARED,
        current=None,
        targets=["a"],
        backups=[],
        mod_id="1",
    )
    assert w.last["status"] == "prepared"
    assert w.last["phase"] == "BEGIN"
    persist_lifecycle_transaction(
        w,
        S.BACKUP_DONE,
        current=S.PREPARED,
        targets=["a"],
        backups=[],
        mod_id="1",
    )
    assert w.last["status"] == "backup_done"
    assert w.last["phase"] == "BACKUP_DONE"


def test_rollback_state_consistency() -> None:
    # Interruptible → ROLLBACK_REQUIRED → FAILED / ROLLED_BACK
    assert transition(S.ROLLBACK_REQUIRED, S.FAILED) is S.FAILED
    assert transition(S.ROLLBACK_REQUIRED, S.ROLLED_BACK) is S.ROLLED_BACK
    assert transition(S.FAILED, S.ROLLED_BACK) is S.ROLLED_BACK
    status, phase = to_wire(S.ROLLED_BACK)
    assert resolve_deployment_state(status, phase) is S.ROLLED_BACK
    # Recovery path: inactive prepared maps to ROLLBACK_REQUIRED
    assert (
        resolve_deployment_state("prepared", "", active_transaction=False)
        is S.ROLLBACK_REQUIRED
    )
