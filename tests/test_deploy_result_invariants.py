"""DeployResult / normalize_deploy_dict invariants."""

from __future__ import annotations

from services.deploy_result import DeployResult, DeployStatus, normalize_deploy_dict


def test_success_status_aligned() -> None:
    out = normalize_deploy_dict({"success": True, "mod_id": "1", "status": "SUCCESS"})
    assert out["success"] is True
    assert out["status"] == "SUCCESS"


def test_failure_status_aligned() -> None:
    out = normalize_deploy_dict({"success": False, "mod_id": "1", "error": "x"})
    assert out["success"] is False
    assert out["status"] == "FAILED"


def test_legacy_success_without_status() -> None:
    out = normalize_deploy_dict({"success": True, "mod_id": "2"})
    assert out["status"] == "SUCCESS"


def test_cancelled_and_timeout_terminal() -> None:
    for status in (DeployStatus.CANCELLED, DeployStatus.TIMEOUT):
        dr = DeployResult(status=status, mod_id="3", error="e")
        out = dr.to_dict()
        assert out["success"] is False
        assert out["status"] == status.value


def test_conflicting_success_flag_resolves_to_status() -> None:
    """status wins over legacy success flag when both present."""
    out = normalize_deploy_dict(
        {"success": True, "status": "FAILED", "mod_id": "4", "error": "boom"}
    )
    assert out["status"] == "FAILED"
    assert out["success"] is False
