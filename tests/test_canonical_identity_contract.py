"""Canonical business identity is a Frozen UUID. PK and projection ids are not."""

from __future__ import annotations

from types import SimpleNamespace

from services.canonical_membership import (
    canonical_deployed_internal_ids,
    entry_is_deployed,
)
from services.deploy_identity import is_frozen_internal_uuid
from services.order_backend import Bg3OrderBackend, ParadoxOrderBackend, Wh3OrderBackend
from services.paradox_activation import CK3_APP_ID, _row_to_ref

_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
_WORKSPACE = "123456789"


def _deployed(**fields: object) -> SimpleNamespace:
    base = {"deployed": True, "mod_id": "123", "workspace_id": _WORKSPACE}
    base.update(fields)
    return SimpleNamespace(**base)


def test_valid_uuid_is_the_canonical_identity() -> None:
    row = _deployed(internal_id=_UUID, mod_id="123")
    assert canonical_deployed_internal_ids([row]) == [_UUID]
    assert entry_is_deployed(row) is True


def test_missing_internal_id_does_not_become_pk() -> None:
    row = _deployed(internal_id="", mod_id="123")
    assert canonical_deployed_internal_ids([row]) == []
    assert "123" not in canonical_deployed_internal_ids([row])
    assert entry_is_deployed(row) is True


def test_invalid_internal_id_does_not_fall_back_to_pk() -> None:
    row = _deployed(internal_id="not-a-uuid", mod_id="123")
    assert canonical_deployed_internal_ids([row]) == []
    assert entry_is_deployed(row) is True


def test_workspace_id_is_not_internal_id() -> None:
    row = _deployed(internal_id=_UUID, workspace_id=_WORKSPACE, mod_id="123")
    found = canonical_deployed_internal_ids([row])
    assert found == [_UUID]
    assert _WORKSPACE not in found


def test_canonical_ids_reject_digit_pk() -> None:
    rows = [
        _deployed(internal_id=_UUID, mod_id="1"),
        _deployed(internal_id="123", mod_id="123"),
        _deployed(internal_id="", token="456", mod_id="456"),
        {"deployed": True, "internal_id": _UUID, "token": "789", "mod_id": "789"},
    ]
    found = canonical_deployed_internal_ids(rows)
    assert found == [_UUID]
    assert all(is_frozen_internal_uuid(item) for item in found)
    assert all(not item.isdigit() for item in found)


def test_paradox_ref_does_not_smuggle_pk_into_token() -> None:
    missing = _row_to_ref(
        {
            "internal_id": "",
            "mod_id": 123,
            "workspace_id": _WORKSPACE,
            "deployed": True,
        }
    )
    assert missing is not None
    assert missing.internal_id == ""
    assert missing.token == ""
    assert missing.token != "123"
    assert entry_is_deployed(missing) is True
    assert canonical_deployed_internal_ids([missing]) == []

    invalid = _row_to_ref(
        {
            "internal_id": "workshop-token",
            "mod_id": 123,
            "workspace_id": _WORKSPACE,
            "deployed": True,
        }
    )
    assert invalid is not None
    assert invalid.internal_id == ""
    assert invalid.token == ""

    valid = _row_to_ref(
        {
            "internal_id": _UUID,
            "mod_id": 123,
            "workspace_id": _WORKSPACE,
            "deployed": True,
        }
    )
    assert valid is not None
    assert valid.internal_id == _UUID
    assert valid.token == _UUID
    assert canonical_deployed_internal_ids([valid]) == [_UUID]


def test_sort_tokens_do_not_use_pk() -> None:
    bare = SimpleNamespace(internal_id="", mod_id="123", id="123", deployed=True)
    valid = SimpleNamespace(internal_id=_UUID, mod_id="123", id="123", deployed=True)
    for backend in (
        ParadoxOrderBackend(CK3_APP_ID),
        Wh3OrderBackend(),
        Bg3OrderBackend(),
    ):
        assert backend.sort_token(bare) == ""
        assert backend.sort_token(valid) == _UUID
        assert backend.is_sortable_member(bare) is True
        assert backend.is_sortable_member(valid) is True
