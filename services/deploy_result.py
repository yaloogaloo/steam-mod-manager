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
    internal_id: str = ""
    app_id: int = 0
    source: str = ""
    target: str = ""
    strategy: str = ""
    stage: str = ""
    copied_files: int = 0
    copied_bytes: int = 0
    elapsed_ms: float = 0.0
    # FilePlan pipeline diagnostics (always meaningful when a plan existed)
    planned_files: int = 0
    backed_up_files: int = 0
    applied_files: int = 0
    verified_files: int = 0
    failed_files: int = 0
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
            "mod_id": self.internal_id,
            "planned_files": self.planned_files,
            "backed_up_files": self.backed_up_files,
            "applied_files": self.applied_files,
            "verified_files": self.verified_files,
            "failed_files": self.failed_files,
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
        # Prefer applied_files when present; keep copied_files for legacy callers.
        copied = self.copied_files or self.applied_files
        if copied:
            out["copied_files"] = copied
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
            "planned_files",
            "backed_up_files",
            "applied_files",
            "verified_files",
            "failed_files",
            "error",
            "error_code",
            "warnings",
        ):
            extra.pop(key, None)
        return cls(
            status=status,
            internal_id=str(data.get("internal_id") or data.get("mod_id") or ""),
            app_id=int(data.get("app_id") or 0),
            source=str(data.get("source") or ""),
            target=str(data.get("target") or ""),
            strategy=str(data.get("strategy") or data.get("deploy_type") or ""),
            stage=str(data.get("stage") or ""),
            copied_files=int(data.get("copied_files") or 0),
            copied_bytes=int(data.get("copied_bytes") or 0),
            elapsed_ms=float(data.get("elapsed_ms") or 0.0),
            planned_files=int(data.get("planned_files") or 0),
            backed_up_files=int(data.get("backed_up_files") or 0),
            applied_files=int(data.get("applied_files") or 0),
            verified_files=int(data.get("verified_files") or 0),
            failed_files=int(data.get("failed_files") or 0),
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
    internal_id: str = "",
    error_code: str = "deploy_failed",
    **extra: Any,
) -> dict[str, Any]:
    return DeployResult(
        status=DeployStatus.FAILED,
        internal_id=internal_id,
        error=error,
        error_code=error_code,
        extra=extra,
    ).to_dict()
