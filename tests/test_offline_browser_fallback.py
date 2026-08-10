"""Browser fallback when requests hits 401/403/429 (e.g. Nexus Cloudflare)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from services.offline.browser import BrowserSnapshotBackend
from services.offline.snapshot import WebSnapshotDownloader


class _FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        content: bytes = b"",
        url: str = "",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        encoding: str = "utf-8",
    ) -> None:
        self.text = text
        self._content = content if content else text.encode("utf-8")
        self.content = self._content
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = encoding
        self.request = MagicMock()

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code} Client Error")
            err.response = self  # type: ignore[attr-defined]
            raise err

    def iter_content(self, chunk_size: int = 64 * 1024):
        yield self._content


def _session_for(pages: dict[str, _FakeResponse]) -> MagicMock:
    session = MagicMock()

    def _get(url: str, **kwargs: Any) -> _FakeResponse:
        key = url.split("#", 1)[0]
        if key in pages:
            return pages[key]
        for known, resp in pages.items():
            if key.rstrip("/") == known.rstrip("/"):
                return resp
        raise AssertionError(f"Unexpected GET {url!r}")

    session.get.side_effect = _get
    return session


def test_403_falls_back_to_browser_and_succeeds(tmp_path: Path) -> None:
    page_url = "https://www.nexusmods.com/palworld/mods/96"
    browser_html = """<!DOCTYPE html>
<html><head>
<link rel="stylesheet" href="https://cdn.example.com/nexus.css">
</head>
<body>
<img src="https://cdn.example.com/cover.webp" alt="cover">
<h1>Nexus Mod 96</h1>
</body></html>
"""
    pages = {
        page_url: _FakeResponse(text="Forbidden", url=page_url, status_code=403),
        "https://cdn.example.com/nexus.css": _FakeResponse(
            content=b"body{color:#111}",
            url="https://cdn.example.com/nexus.css",
            headers={"Content-Type": "text/css"},
        ),
        "https://cdn.example.com/cover.webp": _FakeResponse(
            content=b"WEBP",
            url="https://cdn.example.com/cover.webp",
            headers={"Content-Type": "image/webp"},
        ),
    }

    browser = MagicMock(spec=BrowserSnapshotBackend)
    browser.capture.return_value = browser_html

    out = tmp_path / "offline"
    with WebSnapshotDownloader(
        session=_session_for(pages),
        browser_backend=browser,
    ) as dl:
        result = dl.download(page_url, out)

    assert result.success is True
    assert result.used_browser is True
    assert result.error is None
    browser.capture.assert_called_once_with(page_url)
    html = result.html_path.read_text(encoding="utf-8")
    assert "Nexus Mod 96" in html
    assert 'href="assets/' in html
    assert (out / "assets").is_dir()
    assert any((out / "assets").iterdir())
    # Phase 1: no .js saved
    assert not any(p.suffix.lower() == ".js" for p in (out / "assets").iterdir())


def test_200_does_not_start_browser(tmp_path: Path) -> None:
    page_url = "https://github.com/owner/repo"
    html = """<!DOCTYPE html>
<html><head>
<link rel="stylesheet" href="https://cdn.example.com/g.css">
</head>
<body><h1>OK</h1></body></html>
"""
    pages = {
        page_url: _FakeResponse(text=html, url=page_url, status_code=200),
        "https://cdn.example.com/g.css": _FakeResponse(
            content=b".x{}",
            url="https://cdn.example.com/g.css",
            headers={"Content-Type": "text/css"},
        ),
    }
    browser = MagicMock(spec=BrowserSnapshotBackend)
    browser.capture.side_effect = AssertionError("browser must not run on 200")

    out = tmp_path / "offline"
    with WebSnapshotDownloader(
        session=_session_for(pages),
        browser_backend=browser,
    ) as dl:
        result = dl.download(page_url, out)

    assert result.success is True
    assert result.used_browser is False
    browser.capture.assert_not_called()
    assert "OK" in result.html_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("status", [401, 429])
def test_other_challenge_statuses_use_browser(tmp_path: Path, status: int) -> None:
    page_url = "https://www.nexusmods.com/palworld/mods/1"
    pages = {
        page_url: _FakeResponse(text="blocked", url=page_url, status_code=status),
    }
    browser = MagicMock(spec=BrowserSnapshotBackend)
    browser.capture.return_value = "<html><body>From Browser</body></html>"

    with WebSnapshotDownloader(
        session=_session_for(pages),
        browser_backend=browser,
    ) as dl:
        result = dl.download(page_url, tmp_path / f"out_{status}")

    assert result.success is True
    assert result.used_browser is True
    browser.capture.assert_called_once()
