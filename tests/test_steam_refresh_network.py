"""Manual Steam refresh_details network hardening."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from core.steam_api import (
    REFRESH_BACKOFF_SEC,
    REFRESH_MAX_ATTEMPTS,
    SteamWorkshopClient,
    _batch_needs_network_retry,
    _is_retryable_steam_fetch_error,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "refresh_net.db")
    yield manager
    DatabaseManager.reset_instance()


def test_retryable_error_detection() -> None:
    assert _is_retryable_steam_fetch_error(
        "HTTPSConnectionPool(host='api.steampowered.com'): ConnectTimeoutError"
    )
    assert _is_retryable_steam_fetch_error("Read timed out")
    assert not _is_retryable_steam_fetch_error("Unexpected Steam API payload shape")
    assert not _is_retryable_steam_fetch_error("")


def test_batch_needs_retry_only_when_all_transient() -> None:
    failed = [
        ModMetadata(published_file_id="1", fetch_error="ConnectTimeoutError"),
        ModMetadata(published_file_id="2", fetch_error="Read timed out"),
    ]
    assert _batch_needs_network_retry(failed) is True
    mixed = [
        ModMetadata(published_file_id="1", title="Ok", fetch_error=None),
        ModMetadata(published_file_id="2", fetch_error="ConnectTimeoutError"),
    ]
    assert _batch_needs_network_retry(mixed) is False


def test_refresh_details_retries_then_succeeds(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def flaky(self, ids, *, timeout=None, disable_adapter_retries=False):
        calls["n"] += 1
        assert timeout == (10.0, 30.0) or (
            isinstance(timeout, tuple) and len(timeout) == 2
        )
        assert disable_adapter_retries is True
        if calls["n"] < 3:
            return [
                ModMetadata(
                    published_file_id=str(ids[0]),
                    fetch_error="ConnectTimeoutError(api.steampowered.com timeout=10)",
                )
            ]
        return [
            ModMetadata(
                published_file_id=str(ids[0]),
                title="Recovered Mod",
                description="ok",
            )
        ]

    monkeypatch.setattr(
        SteamWorkshopClient, "_request_published_file_details", flaky
    )
    monkeypatch.setattr(
        "core.steam_api.time.sleep", lambda s: sleeps.append(float(s))
    )

    scrape = MagicMock()
    monkeypatch.setattr(
        SteamWorkshopClient, "_parallel_scrape_fallback", scrape
    )

    client = SteamWorkshopClient(db=db, enable_scrape_fallback=True, request_interval=0)
    try:
        out = client.refresh_details(["3413520661"])
    finally:
        client.close()

    assert calls["n"] == 3
    assert sleeps == [REFRESH_BACKOFF_SEC[0], REFRESH_BACKOFF_SEC[1]]
    assert len(out) == 1
    assert out[0].title == "Recovered Mod"
    assert not out[0].fetch_error
    scrape.assert_not_called()


def test_refresh_details_exhausts_retries_keeps_error(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def always_timeout(self, ids, *, timeout=None, disable_adapter_retries=False):
        calls["n"] += 1
        return [
            ModMetadata(
                published_file_id=str(ids[0]),
                fetch_error="ConnectTimeoutError(host='api.steampowered.com')",
            )
        ]

    monkeypatch.setattr(
        SteamWorkshopClient, "_request_published_file_details", always_timeout
    )
    monkeypatch.setattr(
        "core.steam_api.time.sleep", lambda s: sleeps.append(float(s))
    )

    client = SteamWorkshopClient(db=db, request_interval=0)
    try:
        out = client.refresh_details(["99"])
    finally:
        client.close()

    assert calls["n"] == REFRESH_MAX_ATTEMPTS
    assert len(sleeps) == REFRESH_MAX_ATTEMPTS - 1
    assert sleeps == list(REFRESH_BACKOFF_SEC[: REFRESH_MAX_ATTEMPTS - 1])
    assert out[0].fetch_error
    assert "ConnectTimeout" in out[0].fetch_error
    assert not out[0].title


def test_refresh_details_only_requests_given_ids(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def capture(self, ids, *, timeout=None, disable_adapter_retries=False):
        seen.append([str(i) for i in ids])
        return [
            ModMetadata(published_file_id=str(i), title=f"T{i}") for i in ids
        ]

    monkeypatch.setattr(
        SteamWorkshopClient, "_request_published_file_details", capture
    )
    scrape = MagicMock(side_effect=lambda batch, **k: batch)
    monkeypatch.setattr(SteamWorkshopClient, "_parallel_scrape_fallback", scrape)

    client = SteamWorkshopClient(db=db, request_interval=0)
    try:
        out = client.refresh_details(["111", "222"])
    finally:
        client.close()

    assert seen == [["111", "222"]]
    assert [m.published_file_id for m in out] == ["111", "222"]
    scrape.assert_not_called()


def test_get_details_batch_does_not_use_refresh_retries(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cached fetch path must stay on single-shot ``_request_published_file_details``."""
    calls = {"n": 0}

    def once(self, ids, *, timeout=None, disable_adapter_retries=False):
        calls["n"] += 1
        assert timeout is None
        assert disable_adapter_retries is False
        return [
            ModMetadata(
                published_file_id=str(ids[0]),
                fetch_error="ConnectTimeoutError",
            )
        ]

    monkeypatch.setattr(
        SteamWorkshopClient, "_request_published_file_details", once
    )
    monkeypatch.setattr(
        SteamWorkshopClient,
        "_parallel_scrape_fallback",
        lambda self, batch, **k: batch,
    )
    # Ensure refresh helper is not used by the cached path.
    monkeypatch.setattr(
        SteamWorkshopClient,
        "_request_published_file_details_refresh",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("refresh path used")),
    )

    client = SteamWorkshopClient(db=db, request_interval=0, enable_scrape_fallback=False)
    try:
        out = client.get_details_batch(["55"])
    finally:
        client.close()

    assert calls["n"] == 1
    assert out[0].fetch_error


def test_refresh_steam_mod_metadata_single_id_no_scraper(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.file_ops import INFO_DIR_NAME
    from services.metadata_refresh import refresh_steam_mod_metadata

    mid = "3413520661"
    folder = tmp_path / "library" / "Game" / f"Unknown_Mod_{mid}"
    (folder / INFO_DIR_NAME).mkdir(parents=True)
    (folder / INFO_DIR_NAME / "metadata.json").write_text(
        '{"published_file_id":"%s","title":"Unknown_Mod_%s","fetch_error":"timeout"}'
        % (mid, mid),
        encoding="utf-8",
    )

    refresh_ids: list[list[str]] = []
    scrape_calls = {"n": 0}

    def fake_refresh(self, ids, **kwargs):
        refresh_ids.append([str(i) for i in ids])
        assert kwargs.get("enable_scrape_fallback") is False
        return [
            ModMetadata(
                published_file_id=mid,
                title="Bigger Harbour",
                preview_url="https://example.com/c.jpg",
            )
        ]

    def boom_scrape(self, batch, **kwargs):
        scrape_calls["n"] += 1
        raise AssertionError("workshop scrape must not run during manual refresh")

    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", fake_refresh)
    monkeypatch.setattr(SteamWorkshopClient, "_parallel_scrape_fallback", boom_scrape)
    monkeypatch.setattr(
        SteamWorkshopClient,
        "fetch_and_save_cover",
        lambda self, metadata, dest_dir, *, filename="preview": None,
    )

    result = refresh_steam_mod_metadata(
        mid, folder, library_root=tmp_path / "library", download_cover=False
    )
    assert result.success
    assert refresh_ids == [[mid]]
    assert scrape_calls["n"] == 0
    assert result.renamed is True
    assert result.managed_path is not None
    assert result.managed_path.name == "Bigger Harbour"


def test_refresh_uses_separated_timeouts_on_real_request_path(
    db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_timeout: dict[str, object] = {}

    class FakeResp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "response": {
                    "publishedfiledetails": [
                        {
                            "publishedfileid": "7",
                            "result": 1,
                            "title": "T",
                            "description": "",
                            "file_size": 1,
                            "time_created": 1,
                            "time_updated": 1,
                        }
                    ]
                }
            }

    def fake_no_retry(self, method, url, *, timeout, **kwargs):
        seen_timeout["timeout"] = timeout
        return FakeResp()

    monkeypatch.setattr(
        SteamWorkshopClient, "_request_without_adapter_retries", fake_no_retry
    )
    monkeypatch.setattr("core.steam_api.time.sleep", lambda *_a, **_k: None)

    client = SteamWorkshopClient(db=db, request_interval=0)
    try:
        out = client.refresh_details(["7"])
    finally:
        client.close()

    assert seen_timeout["timeout"] == (10.0, 30.0)
    assert out[0].title == "T"
