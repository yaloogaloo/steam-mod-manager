"""Tests: BeautifulSoup parser selection / lxml fallback for archive."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from bs4 import BeautifulSoup, FeatureNotFound

from services import archive as archive_mod
from services.archive import (
    OfflinePageArchiver,
    SteamArchiveRateLimiter,
    _parse_workshop_html,
    is_stub_offline_page,
)


SAMPLE_HTML = (
    "<!DOCTYPE html><html><body>"
    "<div class='workshopItemTitle'>Test Mod</div>"
    + ("x" * 200)
    + "</body></html>"
)


@pytest.fixture(autouse=True)
def _fast_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive_mod, "_get_archive_proxy", lambda: None)
    lim = SteamArchiveRateLimiter(0.0)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_LIMITER", lim)
    monkeypatch.setattr(archive_mod, "STEAM_ARCHIVE_RATE_LIMITER", lim)


def test_parse_workshop_html_prefers_lxml(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str | None] = []
    real_bs = BeautifulSoup

    def spy(markup: str, features: str | None = None, **kwargs: Any) -> BeautifulSoup:
        calls.append(features)
        return real_bs(markup, features, **kwargs)

    monkeypatch.setattr(archive_mod, "BeautifulSoup", spy)
    soup = _parse_workshop_html(SAMPLE_HTML)
    assert calls == ["lxml"]
    assert soup.find("div") is not None


def test_parse_workshop_html_falls_back_without_lxml(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_bs = BeautifulSoup

    def no_lxml(markup: str, features: str | None = None, **kwargs: Any) -> BeautifulSoup:
        if features == "lxml":
            raise FeatureNotFound(
                "Couldn't find a tree builder with the features you "
                "requested: lxml. Do you need to install a parser library?"
            )
        return real_bs(markup, features, **kwargs)

    monkeypatch.setattr(archive_mod, "BeautifulSoup", no_lxml)

    with caplog.at_level("WARNING", logger=archive_mod.logger.name):
        soup = _parse_workshop_html(SAMPLE_HTML)

    assert soup.find("div") is not None
    assert "falling back to html.parser" in caplog.text


def test_archive_succeeds_when_lxml_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_bs = BeautifulSoup

    def no_lxml(markup: str, features: str | None = None, **kwargs: Any) -> BeautifulSoup:
        if features == "lxml":
            raise FeatureNotFound("no lxml")
        return real_bs(markup, features or "html.parser", **kwargs)

    monkeypatch.setattr(archive_mod, "BeautifulSoup", no_lxml)

    session = MagicMock()
    session.cookies = {}

    with OfflinePageArchiver(session=session) as archiver:
        monkeypatch.setattr(
            archiver, "_fetch_main_html", lambda _url: SAMPLE_HTML
        )
        monkeypatch.setattr(
            archiver,
            "_rewrite_and_download_assets",
            lambda *_a, **_k: {"ok": 0, "fail": 0, "unique": 0},
        )
        with caplog.at_level("WARNING", logger=archive_mod.logger.name):
            path = archiver.archive("3761838546", tmp_path / ".info", overwrite=True)

    assert path.is_file()
    assert is_stub_offline_page(path) is False
    text = path.read_text(encoding="utf-8")
    assert "smm-offline-banner" in text
    assert "falling back to html.parser" in caplog.text
