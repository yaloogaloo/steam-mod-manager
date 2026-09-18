"""Background QThread that runs ModDeployer without blocking the UI."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

from PySide6.QtCore import QThread, Signal

from core.paths import default_mod_library
from services.deploy import ModDeployer
from services.deploy_result import DeployResult, DeployStatus, normalize_deploy_dict

logger = logging.getLogger(__name__)

DeployAction = Literal["deploy", "undeploy", "redeploy"]


class DeployWorker(QThread):
    """
    Deploy / undeploy / redeploy one Mod on a worker thread.

    Signal names use a ``deploy_`` prefix because ``QThread`` already owns
    ``started`` / ``finished``.

    Terminal routing (exactly one terminal signal per run):
      SUCCESS / CANCELLED / TIMEOUT → ``deploy_finished``
      FAILED                        → ``deploy_failed`` only
    """

    deploy_started = Signal()
    deploy_finished = Signal(object)
    # Full normalized result dict (not a bare error string) so UI can paint once.
    deploy_failed = Signal(object)

    def __init__(
        self,
        internal_id: int | str,
        library_root: str | Path | None = None,
        parent=None,
        *,
        action: DeployAction = "deploy",
        deployer: ModDeployer | None = None,
    ) -> None:
        super().__init__(parent)
        self.internal_id = str(internal_id).strip()
        self.library_root = Path(library_root) if library_root else default_mod_library()
        self.action: DeployAction = action
        self._deployer = deployer
        self._terminal_emitted = False

    def _emit_terminal(self, payload: dict[str, Any]) -> None:
        if self._terminal_emitted:
            return
        self._terminal_emitted = True
        normalized = normalize_deploy_dict(payload)
        status = str(normalized.get("status") or "").upper()
        logger.info(
            "[DEPLOY] internal_id=%s mod_pk=%s action=%s status=%s",
            normalized.get("internal_id") or self.internal_id,
            normalized.get("mod_pk") or "",
            self.action,
            status or normalized.get("status"),
        )
        if status == DeployStatus.SUCCESS.value:
            self.deploy_finished.emit(normalized)
            return
        if status == DeployStatus.CANCELLED.value:
            self.deploy_finished.emit(normalized)
            return
        if status == DeployStatus.TIMEOUT.value:
            self.deploy_finished.emit(normalized)
            return
        # FAILED (and legacy non-success): single terminal via deploy_failed only.
        # Do NOT also emit deploy_finished — that caused dual projection refresh.
        self.deploy_failed.emit(normalized)

    def run(self) -> None:
        self.deploy_started.emit()
        result: dict[str, Any] | None = None
        try:
            deployer = self._deployer or ModDeployer(library_root=self.library_root)
            if self.action == "undeploy":
                result = deployer.undeploy_mod(self.internal_id)
            elif self.action == "redeploy":
                result = deployer.redeploy_mod(self.internal_id)
            else:
                result = deployer.deploy_mod(self.internal_id)
            if self.isInterruptionRequested():
                result = DeployResult(
                    status=DeployStatus.CANCELLED,
                    internal_id=self.internal_id,
                    error="部署已取消",
                ).to_dict()
        except Exception as exc:  # noqa: BLE001 — surface to UI
            logger.exception(
                "[DEPLOY_FAILED] internal_id=%s action=%s unhandled error_code=worker_exception",
                self.internal_id,
                self.action,
            )
            result = DeployResult(
                status=DeployStatus.FAILED,
                internal_id=self.internal_id,
                error=str(exc),
                error_code="worker_exception",
            ).to_dict()
        finally:
            if self.isInterruptionRequested() and result is None:
                result = DeployResult(
                    status=DeployStatus.CANCELLED,
                    internal_id=self.internal_id,
                    error="部署已取消",
                ).to_dict()
            if result is None:
                result = DeployResult(
                    status=DeployStatus.FAILED,
                    internal_id=self.internal_id,
                    error="部署失败：未知错误（无结果）",
                    error_code="empty_result",
                ).to_dict()
            self._emit_terminal(result)
