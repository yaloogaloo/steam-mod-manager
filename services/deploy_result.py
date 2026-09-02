"""Unified deploy terminal result — worker/UI/DB share one status vocabulary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DeployStatus(str, Enum):
    """Terminal deploy outcomes only — never leave the UI in perpetual RUNNING."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


@dataclass
class DeployResult:
    """Normalized deploy outcome (maps to legacy dict for Qt signals)."""

    status: DeployStatus
    mod_id: str = ""
    app_id: int = 0
    source: str = ""
    target: str = ""
    strategy: str = ""
    stage: str = ""
    copied_files: int = 0
    copied_bytes: int = 0
    elapsed_ms: float = 0.0
    error: str = ""
    error_code: str = ""
    warnings: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.status == DeployStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "success": self.success,
            "status": self.status.value,
            "mod_id": self.mod_id,
        }
        if self.app_id:
            out["app_id"] = self.app_id
        if self.source:
            out["source"] = self.source
        if self.target:
            out["target"] = self.target
        if self.strategy:
            out["strategy"] = self.strategy
            out["deploy_type"] = self.strategy
        if self.stage:
            out["stage"] = self.stage
        if self.copied_files:
            out["copied_files"] = self.copied_files
        if self.copied_bytes:
            out["copied_bytes"] = self.copied_bytes
        if self.elapsed_ms:
            out["elapsed_ms"] = self.elapsed_ms
        if self.error:
            out["error"] = self.error
        if self.error_code:
            out["error_code"] = self.error_code
        if self.warnings:
            out["warnings"] = list(self.warnings)
        out.update(self.extra)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> DeployResult:
        if not isinstance(data, dict):
            return cls(
                status=DeployStatus.FAILED,
                error="invalid deploy result",
                error_code="invalid_result",
            )
        raw_status = str(data.get("status") or "").strip().upper()
        if raw_status in DeployStatus.__members__:
            status = DeployStatus[raw_status]
        elif data.get("success"):
            status = DeployStatus.SUCCESS
        else:
            status = DeployStatus.FAILED
        extra = dict(data)
        for key in (
            "success",
            "status",
            "mod_id",
            "app_id",
            "source",
            "target",
            "strategy",
            "deploy_type",
            "stage",
            "copied_files",
            "copied_bytes",
            "elapsed_ms",
            "error",
            "error_code",
            "warnings",
        ):
            extra.pop(key, None)
        return cls(
            status=status,
            mod_id=str(data.get("mod_id") or ""),
            app_id=int(data.get("app_id") or 0),
            source=str(data.get("source") or ""),
            target=str(data.get("target") or ""),
            strategy=str(data.get("strategy") or data.get("deploy_type") or ""),
            stage=str(data.get("stage") or ""),
            copied_files=int(data.get("copied_files") or 0),
            copied_bytes=int(data.get("copied_bytes") or 0),
            elapsed_ms=float(data.get("elapsed_ms") or 0.0),
            error=str(data.get("error") or ""),
            error_code=str(data.get("error_code") or ""),
            warnings=list(data.get("warnings") or []),
            extra=extra,
        )


def normalize_deploy_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Ensure legacy deploy dicts include unified ``status`` field."""
    return DeployResult.from_dict(data).to_dict()


def terminal_failed(
    error: str,
    *,
    mod_id: str = "",
    error_code: str = "deploy_failed",
    **extra: Any,
) -> dict[str, Any]:
    return DeployResult(
        status=DeployStatus.FAILED,
        mod_id=mod_id,
        error=error,
        error_code=error_code,
        extra=extra,
    ).to_dict()
