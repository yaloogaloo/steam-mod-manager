"""Synthetic timing plans and opt-in Qt worker; no business service imports."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    at_ms: int
    action: str
    identity: str


PLANS = {
    "game_a_to_b": (Event(0, "request", "A"), Event(10, "select", "B"), Event(20, "success", "B"), Event(60, "success", "A")),
    "game_a_to_clear": (Event(0, "request", "A"), Event(10, "clear", ""), Event(60, "success", "A")),
    "late_failure": (Event(0, "request", "A"), Event(10, "select", "B"), Event(60, "failure", "A")),
    "metadata_a_to_b": (Event(0, "request_pk", "101"), Event(10, "select_pk", "102"), Event(60, "metadata_success_pk", "101")),
    "late_cover": (Event(0, "cover_request", "A"), Event(10, "select_no_cover", "B"), Event(60, "cover_success", "A")),
    "hidden_return": (Event(0, "request", "A"), Event(10, "hide", ""), Event(60, "success", "A")),
    "cancel_return": (Event(0, "request", "A"), Event(10, "cancel_requested", "A"), Event(60, "success", "A")),
    "close_running": (Event(0, "request", "A"), Event(10, "close", ""), Event(60, "success", "A")),
}


ALLOWED_TASK_KINDS = frozenset({"game", "metadata", "library"})


def validate_controlled_kind(kind):
    """Only synthetic result kinds. Does not intercept production dispatch."""
    if kind not in ALLOWED_TASK_KINDS:
        raise ValueError("Non-synthetic task request refused")


def worker_type():
    """Create only in an admitted child. Signal shape matches known UI workers.

    Connecting these signals to real production receivers is not implemented;
    returning a class does not establish UI admission or fix a race.
    """
    from PySide6.QtCore import QThread, Signal

    class ControlledWorker(QThread):
        finished_ok = Signal(object, object)
        finished_error = Signal(str)
        refresh_finished = Signal(object)
        refresh_failed = Signal(str)
        loaded = Signal(object)
        failed = Signal(str)

        def __init__(self, payload, *, kind="game", delay_ms=60, error=None, deliver_after_cancel=False, parent=None):
            validate_controlled_kind(kind)
            super().__init__(parent)
            self.payload = payload
            self.kind = kind
            self.delay_ms = max(0, int(delay_ms))
            self.error = error
            self.deliver_after_cancel = deliver_after_cancel

        def run(self):
            self.msleep(self.delay_ms)
            if self.isInterruptionRequested() and not self.deliver_after_cancel:
                return
            if self.kind == "game":
                if self.error:
                    self.finished_error.emit(str(self.error))
                else:
                    self.finished_ok.emit(self.payload, None)
            elif self.kind == "metadata":
                if self.error:
                    self.refresh_failed.emit(str(self.error))
                else:
                    self.refresh_finished.emit(self.payload)
            elif self.kind == "library":
                if self.error:
                    self.failed.emit(str(self.error))
                else:
                    self.loaded.emit(self.payload)
            else:
                self.failed.emit("Unsupported controlled worker kind")

    return ControlledWorker
