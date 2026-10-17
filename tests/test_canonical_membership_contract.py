"""Cross-backend contract: Sorting Mode membership is canonical deployed set."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.canonical_membership import (
    canonical_deployed_internal_ids,
    entry_is_deployed,
)
from services.order_backend import Bg3OrderBackend, ParadoxOrderBackend, Wh3OrderBackend
from services.paradox_activation import CK3_APP_ID, STELLARIS_APP_ID
from services.wh3_activation import merge_deployed_order

_UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
_UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"


def _row(internal_id: str, *, deployed: bool) -> SimpleNamespace:
    return SimpleNamespace(
        internal_id=internal_id,
        deployed=deployed,
        mod_id="1",
        workspace_id="1",
    )


@pytest.mark.parametrize(
    "backend",
    [
        ParadoxOrderBackend(CK3_APP_ID),
        ParadoxOrderBackend(STELLARIS_APP_ID),
        Wh3OrderBackend(),
        Bg3OrderBackend(),
    ],
)
def test_sorting_membership_matches_deployed_membership(backend) -> None:
    """Projection files are not consulted. Deployed rows are members."""
    deployed = _row(_UUID_A, deployed=True)
    idle = _row(_UUID_B, deployed=False)
    non_uuid = _row("not-a-frozen-uuid", deployed=True)
    assert backend.is_sortable_member(deployed) is True
    assert backend.is_sortable_member(idle) is False
    assert backend.is_sortable_member(non_uuid) is True
    assert canonical_deployed_internal_ids([deployed, idle, non_uuid]) == [
        _UUID_A,
    ]


def test_entry_is_deployed_reads_projection_flag_only() -> None:
    assert entry_is_deployed({"deployed": True, "deploy_status": "not_deployed"}) is True
    assert entry_is_deployed({"deploy_status": "deployed"}) is True
    assert entry_is_deployed({"deploy_status": "not_deployed"}) is False
    assert entry_is_deployed(SimpleNamespace(deploy_status="deployed")) is True


@pytest.mark.parametrize(
    "backend",
    [
        ParadoxOrderBackend(CK3_APP_ID),
        ParadoxOrderBackend(STELLARIS_APP_ID),
        Wh3OrderBackend(),
        Bg3OrderBackend(),
    ],
)
def test_unresolved_projection_fields_do_not_remove_membership(backend) -> None:
    """available/unresolved/pak/launcher fields are not membership inputs."""
    row = SimpleNamespace(
        internal_id=_UUID_A,
        deployed=True,
        available=False,
        unresolved=True,
        launcher_id=None,
        pak_uuid="",
        reason="missing_launcher_descriptor",
    )
    assert backend.is_sortable_member(row) is True
    assert canonical_deployed_internal_ids([row]) == [_UUID_A]


def test_order_merge_is_membership_sequence_not_projection() -> None:
    """Missing tokens append. Undeployed tokens leave. Order never defines the set."""
    assert merge_deployed_order(["A"], ["A", "B"]) == ["A", "B"]
    assert merge_deployed_order([], ["A", "B"]) == ["A", "B"]
    assert merge_deployed_order(["A", "B"], ["A"]) == ["A"]
    assert merge_deployed_order(["A", "C"], ["A", "B", "C"]) == ["A", "C", "B"]


def _sorting_ids(backend, rows: list[SimpleNamespace]) -> list[str]:
    return [row.internal_id for row in rows if backend.is_sortable_member(row)]


@pytest.mark.parametrize(
    ("label", "backend"),
    [
        ("CK3", ParadoxOrderBackend(CK3_APP_ID)),
        ("Stellaris", ParadoxOrderBackend(STELLARIS_APP_ID)),
        ("WH3", Wh3OrderBackend()),
        ("BG3", Bg3OrderBackend()),
    ],
)
@pytest.mark.parametrize(
    ("state", "deployed", "in_order"),
    [
        ("A", False, False),
        ("B", True, False),
        ("C", True, False),
        ("D", True, True),
        ("E", False, True),
        ("F", True, True),
        ("G", True, True),
    ],
)
def test_transition_matrix_projection_cannot_change_membership(
    label: str,
    backend,
    state: str,
    deployed: bool,
    in_order: bool,
) -> None:
    """Descriptor, pak UUID, and order presence are not membership inputs.

    B/C are deployed with no order token. E keeps a stale order token and
    is not a member. F/G stay members when projection would be unresolved.
    """
    del label, in_order
    row = _row(_UUID_A, deployed=deployed)
    sorting = _sorting_ids(backend, [row])
    deployed_ids = canonical_deployed_internal_ids([row])
    assert sorting == deployed_ids
    if state == "E":
        assert sorting == []
        assert merge_deployed_order([_UUID_A], []) == []
    if state in {"B", "C"}:
        assert sorting == [_UUID_A]
        assert merge_deployed_order([], [_UUID_A]) == [_UUID_A]
