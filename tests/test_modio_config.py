"""Mod.io configuration loader (config/modio.json)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.modio_config import (
    DEFAULT_API_BASE_URL,
    ModioConfigError,
    load_modio_config,
    load_modio_config_dict,
)


def test_load_modio_config_success(tmp_path: Path) -> None:
    path = tmp_path / "modio.json"
    path.write_text(
        json.dumps(
            {
                "api_key": "test-key-for-unit-only",
                "api_base_url": "https://api.mod.io/v1",
            }
        ),
        encoding="utf-8",
    )
    cfg = load_modio_config(path)
    assert cfg.api_key == "test-key-for-unit-only"
    assert cfg.api_base_url == "https://api.mod.io/v1"
    as_dict = load_modio_config_dict(path)
    assert as_dict["api_key"] == "test-key-for-unit-only"
    assert as_dict["api_base_url"] == "https://api.mod.io/v1"


def test_missing_config_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(ModioConfigError, match="不存在"):
        load_modio_config(missing)


def test_empty_api_key_raises(tmp_path: Path) -> None:
    path = tmp_path / "modio.json"
    path.write_text(
        json.dumps({"api_key": "", "api_base_url": DEFAULT_API_BASE_URL}),
        encoding="utf-8",
    )
    with pytest.raises(ModioConfigError, match="api_key 为空"):
        load_modio_config(path, require_api_key=True)


def test_empty_api_key_allowed_when_not_required(tmp_path: Path) -> None:
    path = tmp_path / "modio.json"
    path.write_text(
        json.dumps({"api_key": "   ", "api_base_url": DEFAULT_API_BASE_URL}),
        encoding="utf-8",
    )
    cfg = load_modio_config(path, require_api_key=False)
    assert cfg.api_key == ""
    assert cfg.api_base_url == DEFAULT_API_BASE_URL


def test_invalid_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "modio.json"
    path.write_text("{not-valid-json", encoding="utf-8")
    with pytest.raises(ModioConfigError, match="JSON 无效"):
        load_modio_config(path)


def test_invalid_root_type_raises(tmp_path: Path) -> None:
    path = tmp_path / "modio.json"
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with pytest.raises(ModioConfigError, match="格式无效"):
        load_modio_config(path)


def test_example_template_shape() -> None:
    """Committed example must stay a safe empty-key template."""
    example = Path(__file__).resolve().parents[1] / "config" / "modio.json.example"
    assert example.is_file()
    data = json.loads(example.read_text(encoding="utf-8"))
    assert data.get("api_key", None) == ""
    assert "api.mod.io" in str(data.get("api_base_url") or "")
