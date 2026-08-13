"""Load ``config/debug.json`` — production diagnostics stay off by default."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from core.paths import project_root

DEBUG_CONFIG_REL = Path("config") / "debug.json"

_CACHED: DebugSettings | None = None


@dataclass(frozen=True)
class DebugSettings:
    ui_trace: bool = False
    performance_log: bool = False


def debug_config_path() -> Path:
    return project_root() / DEBUG_CONFIG_REL


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def load_debug_settings(*, force_reload: bool = False) -> DebugSettings:
    """Read ``config/debug.json``. Missing / invalid file → all flags false."""
    global _CACHED
    if _CACHED is not None and not force_reload:
        return _CACHED
    settings = DebugSettings()
    path = debug_config_path()
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                settings = DebugSettings(
                    ui_trace=_parse_bool(raw.get("ui_trace", False)),
                    performance_log=_parse_bool(raw.get("performance_log", False)),
                )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        settings = DebugSettings()
    _CACHED = settings
    return settings


def reset_debug_settings_cache() -> None:
    global _CACHED
    _CACHED = None


def ui_trace_enabled() -> bool:
    if os.environ.get("SMM_UI_TRACE", "").strip() in {"1", "true", "yes"}:
        return True
    return load_debug_settings().ui_trace


def performance_log_enabled() -> bool:
    if os.environ.get("SMM_PERF_LOG", "").strip() in {"1", "true", "yes"}:
        return True
    return load_debug_settings().performance_log


def debug_mode_enabled() -> bool:
    """True when any diagnostic channel is on."""
    return ui_trace_enabled() or performance_log_enabled()
