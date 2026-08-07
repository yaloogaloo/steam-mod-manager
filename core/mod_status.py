"""Mod lifecycle status (invalid / conflict) — SQLite-backed, not .info."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

CONFLICT_STATUS_NONE = "none"
CONFLICT_STATUS_WARNING = "warning"
CONFLICT_STATUS_CONFLICT = "conflict"
SUPPORTED_CONFLICT_STATUSES = (
    CONFLICT_STATUS_NONE,
    CONFLICT_STATUS_WARNING,
    CONFLICT_STATUS_CONFLICT,
)


def normalize_conflict_status(value: str | None) -> str:
    key = str(value or "").strip().lower()
    if key in SUPPORTED_CONFLICT_STATUSES:
        return key
    return CONFLICT_STATUS_NONE


@dataclass
class ModStatus:
    """Lifecycle / health flags for one Mod."""

    invalid: bool = False
    invalid_reason: str = ""
    conflict_status: str = CONFLICT_STATUS_NONE
    conflict_note: str = ""
    last_check_time: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "invalid": bool(self.invalid),
            "invalid_reason": self.invalid_reason or "",
            "conflict_status": normalize_conflict_status(self.conflict_status),
            "conflict_note": self.conflict_note or "",
            "last_check_time": self.last_check_time or "",
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> ModStatus:
        if not data:
            return cls()
        invalid_raw = data.get("invalid", data.get("is_invalid", False))
        if invalid_raw in (True, 1, "1", "true", "True"):
            invalid = True
        else:
            invalid = False
        return cls(
            invalid=invalid,
            invalid_reason=str(data.get("invalid_reason") or ""),
            conflict_status=normalize_conflict_status(
                str(data.get("conflict_status") or CONFLICT_STATUS_NONE)
            ),
            conflict_note=str(data.get("conflict_note") or ""),
            last_check_time=str(data.get("last_check_time") or ""),
        )

    @property
    def is_conflict(self) -> bool:
        return self.conflict_status == CONFLICT_STATUS_CONFLICT

    @property
    def is_warning(self) -> bool:
        return self.conflict_status == CONFLICT_STATUS_WARNING

    @property
    def run_label(self) -> str:
        if self.invalid:
            return "失效"
        if self.is_conflict:
            return "冲突"
        if self.is_warning:
            return "警告"
        return "正常"
