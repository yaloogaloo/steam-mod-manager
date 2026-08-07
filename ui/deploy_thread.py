"""Background QThread that runs ModDeployer without blocking the UI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from PySide6.QtCore import QThread, Signal

from core.paths import default_mod_library
from services.deploy import ModDeployer

DeployAction = Literal["deploy", "undeploy", "redeploy"]


class DeployWorker(QThread):
    """
    Deploy / undeploy / redeploy one Mod on a worker thread.

    Signal names use a ``deploy_`` prefix because ``QThread`` already owns
    ``started`` / ``finished``.
    """

    deploy_started = Signal()
    deploy_finished = Signal(object)
    deploy_failed = Signal(str)

    def __init__(
        self,
        mod_id: int | str,
        library_root: str | Path | None = None,
        parent=None,
        *,
        action: DeployAction = "deploy",
        deployer: ModDeployer | None = None,
    ) -> None:
        super().__init__(parent)
        self.mod_id = str(mod_id).strip()
        self.library_root = Path(library_root) if library_root else default_mod_library()
        self.action: DeployAction = action
        self._deployer = deployer

    def run(self) -> None:
        self.deploy_started.emit()
        try:
            deployer = self._deployer or ModDeployer(library_root=self.library_root)
            if self.action == "undeploy":
                result: dict[str, Any] = deployer.undeploy_mod(self.mod_id)
            elif self.action == "redeploy":
                result = deployer.redeploy_mod(self.mod_id)
            else:
                result = deployer.deploy_mod(self.mod_id)
            if self.isInterruptionRequested():
                return
            self.deploy_finished.emit(result)
        except Exception as exc:  # noqa: BLE001 — surface to UI
            self.deploy_failed.emit(str(exc))
