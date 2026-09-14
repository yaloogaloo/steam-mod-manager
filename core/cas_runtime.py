"""CAS-only LIVE ``.info`` asset runtime feature gate (Phase 6).

When enabled (default), durable LIVE offline assets live in Asset Store +
``.info[/offline]/manifest.json``. Capture stages under ``cache/temp``.
OPEN uses ``cache/offline_view``. Physical ``.info/.../assets`` is leftover
GC only — not SoT, not capture staging, not recreated by Repair.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from core.paths import project_root

_CONFIG_REL = Path("config") / "asset_runtime.json"
_ENV = "SMM_CAS_ONLY_INFO_ASSET_RUNTIME"

_CACHED: bool | None = None


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


def cas_only_info_asset_runtime(*, force_reload: bool = False) -> bool:
    """
    ``CAS_ONLY_INFO_ASSET_RUNTIME`` — default **True**.

    Override order:
    1. Env ``SMM_CAS_ONLY_INFO_ASSET_RUNTIME``
    2. ``config/asset_runtime.json`` key ``cas_only_info_asset_runtime``
    3. Default True
    """
    global _CACHED
    if _CACHED is not None and not force_reload:
        return _CACHED

    env = os.environ.get(_ENV)
    if env is not None and str(env).strip() != "":
        _CACHED = _parse_bool(env, default=True)
        return _CACHED

    enabled = True
    path = project_root() / _CONFIG_REL
    try:
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "cas_only_info_asset_runtime" in raw:
                enabled = _parse_bool(
                    raw.get("cas_only_info_asset_runtime"), default=True
                )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        enabled = True

    _CACHED = enabled
    return _CACHED


def reset_cas_runtime_cache() -> None:
    global _CACHED
    _CACHED = None


# Public alias matching the Phase 6 gate name.
CAS_ONLY_INFO_ASSET_RUNTIME = cas_only_info_asset_runtime
