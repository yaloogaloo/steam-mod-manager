"""Startup Identity Reconcile policy — default off to keep launch IO calm.

Override order for ``startup_reconcile_enabled``:
1. Env ``SMM_STARTUP_RECONCILE_ENABLED``
2. ``config/startup_runtime.json`` key ``startup_reconcile_enabled``
3. Default **False**

When disabled, GUI startup loads DB projection only and does not walk the
mod library for Identity Reconcile. Explicit callers of
``reconcile_library`` / ``start_reconcile_library_async`` are unchanged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from core.paths import project_root

_CONFIG_REL = Path("config") / "startup_runtime.json"
_ENV_ENABLED = "SMM_STARTUP_RECONCILE_ENABLED"

_CACHED: "StartupReconcilePolicy | None" = None


def _parse_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_int(value: object, *, default: int, minimum: int = 0) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, n)


@dataclass(frozen=True)
class StartupReconcilePolicy:
    """Pacing for optional deferred startup Identity Reconcile."""

    enabled: bool = False
    delay_ms: int = 15_000
    batch_size: int = 25
    batch_pause_ms: int = 50
    # 0 = unlimited (full library). >0 caps bind pass and skips missing/orphan.
    max_mods: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "startup_reconcile_enabled": self.enabled,
            "startup_reconcile_delay_ms": self.delay_ms,
            "startup_reconcile_batch_size": self.batch_size,
            "startup_reconcile_batch_pause_ms": self.batch_pause_ms,
            "startup_reconcile_max_mods": self.max_mods,
        }


def _from_mapping(raw: dict) -> StartupReconcilePolicy:
    return StartupReconcilePolicy(
        enabled=_parse_bool(raw.get("startup_reconcile_enabled"), default=False),
        delay_ms=_parse_int(
            raw.get("startup_reconcile_delay_ms"), default=15_000, minimum=0
        ),
        batch_size=_parse_int(
            raw.get("startup_reconcile_batch_size"), default=25, minimum=1
        ),
        batch_pause_ms=_parse_int(
            raw.get("startup_reconcile_batch_pause_ms"), default=50, minimum=0
        ),
        max_mods=_parse_int(
            raw.get("startup_reconcile_max_mods"), default=0, minimum=0
        ),
    )


def load_startup_reconcile_policy(*, force_reload: bool = False) -> StartupReconcilePolicy:
    global _CACHED
    if _CACHED is not None and not force_reload:
        return _CACHED

    file_policy = StartupReconcilePolicy()
    path = project_root() / _CONFIG_REL
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                file_policy = _from_mapping(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        file_policy = StartupReconcilePolicy()

    env = os.environ.get(_ENV_ENABLED)
    if env is not None and str(env).strip() != "":
        enabled = _parse_bool(env, default=False)
        policy = StartupReconcilePolicy(
            enabled=enabled,
            delay_ms=file_policy.delay_ms,
            batch_size=file_policy.batch_size,
            batch_pause_ms=file_policy.batch_pause_ms,
            max_mods=file_policy.max_mods,
        )
    else:
        policy = file_policy

    _CACHED = policy
    return _CACHED


def reset_startup_reconcile_policy_cache() -> None:
    global _CACHED
    _CACHED = None


def startup_reconcile_enabled(*, force_reload: bool = False) -> bool:
    return bool(load_startup_reconcile_policy(force_reload=force_reload).enabled)
