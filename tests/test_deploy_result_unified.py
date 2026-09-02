"""Unified DeployResult / DeployStatus normalization."""

from __future__ import annotations

from services.deploy import _finalize_deploy_dict
from services.deploy_result import (
    DeployResult,
    DeployStatus,
    normalize_deploy_dict,
    terminal_failed,
)


def test_normalize_legacy_success_dict() -> None:
    out = normalize_deploy_dict({"success": True, "mod_id": "42", "target": "/x"})
    assert out["status"] == "SUCCESS"
    assert out["success"] is True


def test_normalize_legacy_failure_dict() -> None:
    out = normalize_deploy_dict({"success": False, "mod_id": "42", "error": "boom"})
    assert out["status"] == "FAILED"
    assert out["success"] is False


def test_terminal_failed_includes_error_code() -> None:
    out = terminal_failed("busy", mod_id="9", error_code="deploy_in_progress")
    assert out["status"] == "FAILED"
    assert out["error_code"] == "deploy_in_progress"


def test_deploy_result_roundtrip() -> None:
    dr = DeployResult(
        status=DeployStatus.CANCELLED,
        mod_id="1",
        error="部署已取消",
    )
    back = DeployResult.from_dict(dr.to_dict())
    assert back.status == DeployStatus.CANCELLED
    assert back.error == "部署已取消"


def test_finalize_deploy_dict_adds_status() -> None:
    out = _finalize_deploy_dict({"success": True, "mod_id": "7"})
    assert out["status"] == "SUCCESS"
