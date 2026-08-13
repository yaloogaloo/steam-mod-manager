"""Production diagnostics stay off unless config/debug.json or env enables them."""

from __future__ import annotations

from core.debug_config import (
    load_debug_settings,
    performance_log_enabled,
    reset_debug_settings_cache,
    ui_trace_enabled,
)


def test_debug_flags_default_off(monkeypatch) -> None:
    monkeypatch.delenv("SMM_UI_TRACE", raising=False)
    monkeypatch.delenv("SMM_PERF_LOG", raising=False)
    reset_debug_settings_cache()
    settings = load_debug_settings(force_reload=True)
    assert settings.ui_trace is False
    assert settings.performance_log is False
    assert ui_trace_enabled() is False
    assert performance_log_enabled() is False


def test_env_overrides_enable_ui_trace(monkeypatch) -> None:
    monkeypatch.setenv("SMM_UI_TRACE", "1")
    reset_debug_settings_cache()
    assert ui_trace_enabled() is True
