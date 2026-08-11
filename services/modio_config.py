"""Centralized Mod.io configuration loader (``config/modio.json``).

Does not log or embed API keys. Credentials stay in a gitignored local file
and/or environment variables consumed by callers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

DEFAULT_API_BASE_URL = "https://api.mod.io/v1"
CONFIG_FILENAME = "modio.json"


class ModioConfigError(Exception):
    """Raised when Mod.io configuration cannot be loaded or is incomplete."""


@dataclass(frozen=True)
class ModioConfig:
    """Validated Mod.io settings for API clients."""

    api_key: str
    api_base_url: str = DEFAULT_API_BASE_URL

    def to_dict(self) -> dict[str, str]:
        return {
            "api_key": self.api_key,
            "api_base_url": self.api_base_url,
        }


def project_root() -> Path:
    """Repository root (parent of ``services/``)."""
    return Path(__file__).resolve().parents[1]


def default_modio_config_path() -> Path:
    """Canonical path: ``<repo>/config/modio.json``."""
    return project_root() / "config" / CONFIG_FILENAME


def load_modio_config(
    path: str | Path | None = None,
    *,
    require_api_key: bool = True,
) -> ModioConfig:
    """
    Load Mod.io configuration from JSON.

    Parameters
    ----------
    path:
        Optional override (tests). Defaults to ``config/modio.json``.
    require_api_key:
        When True (default), an empty / missing ``api_key`` raises
        ``ModioConfigError``.

    Raises
    ------
    ModioConfigError
        File missing, invalid JSON, wrong shape, or empty API key (when required).
    """
    config_path = Path(path) if path is not None else default_modio_config_path()

    if not config_path.is_file():
        raise ModioConfigError(
            f"Mod.io 配置文件不存在: {config_path}. "
            f"请复制 config/modio.json.example 为 config/modio.json 并填写 api_key。"
        )

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ModioConfigError(f"无法读取 Mod.io 配置文件: {config_path}") from exc

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ModioConfigError(
            f"Mod.io 配置 JSON 无效: {config_path} ({exc.msg})"
        ) from exc

    if not isinstance(payload, Mapping):
        raise ModioConfigError(
            f"Mod.io 配置格式无效: {config_path}（根节点必须是对象）"
        )

    api_key = str(payload.get("api_key") or "").strip()
    api_base = str(
        payload.get("api_base_url") or DEFAULT_API_BASE_URL
    ).strip() or DEFAULT_API_BASE_URL

    if require_api_key and not api_key:
        raise ModioConfigError(
            f"Mod.io api_key 为空: {config_path}. "
            "请在 config/modio.json 中填写 API Key（不要提交到 Git）。"
        )

    if api_key:
        # Never log the key itself.
        logger.info("Mod.io API authentication configured")
    else:
        logger.warning("Mod.io config loaded but api_key is empty")

    return ModioConfig(api_key=api_key, api_base_url=api_base.rstrip("/"))


def load_modio_config_dict(
    path: str | Path | None = None,
    *,
    require_api_key: bool = True,
) -> dict[str, str]:
    """Same as ``load_modio_config`` but returns a plain dict."""
    return load_modio_config(path, require_api_key=require_api_key).to_dict()
