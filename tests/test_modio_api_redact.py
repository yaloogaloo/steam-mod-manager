"""Sanity check: Mod.io error redaction never leaks api_key."""

from services.modio_api import _redact_secrets


def test_redact_api_key_from_timeout_message() -> None:
    key = "5c4eec6a6d6019a99b6c6c47cdd69bf7"
    raw = (
        "HTTPSConnectionPool(host='api.mod.io', port=443): "
        f"Max retries exceeded with url: /v1/games?api_key={key}"
    )
    out = _redact_secrets(raw, api_key=key)
    assert key not in out
    assert "api_key=***" in out
