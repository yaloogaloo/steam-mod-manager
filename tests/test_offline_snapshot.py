"""WebSnapshotDownloader — localize HTML + assets for Nexus / GitHub."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

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

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int = 64 * 1024):
        yield self._content


def _session_for(pages: dict[str, _FakeResponse]) -> MagicMock:
    session = MagicMock()

    def _get(url: str, **kwargs: Any) -> _FakeResponse:
        # Normalize trailing slash variance
        key = url.split("#", 1)[0]
        if key in pages:
            return pages[key]
        # Allow relative resolution against known hosts
        for known, resp in pages.items():
            if key.rstrip("/") == known.rstrip("/"):
                return resp
        raise AssertionError(f"Unexpected GET {url!r}")

    session.get.side_effect = _get
    return session


def test_nexus_like_snapshot_rewrites_assets(tmp_path: Path) -> None:
    page_url = "https://www.nexusmods.com/palworld/mods/336"
    html = """<!DOCTYPE html>
<html><head>
<link rel="stylesheet" href="https://cdn.example.com/main.css">
<script src="/static/app.js"></script>
</head>
<body>
<img src="https://cdn.example.com/cover.webp" alt="cover">
<h1>Nexus Mod 336</h1>
</body></html>
"""
    pages = {
        page_url: _FakeResponse(text=html, url=page_url),
        "https://cdn.example.com/main.css": _FakeResponse(
            content=b"body{color:#fff}",
            url="https://cdn.example.com/main.css",
            headers={"Content-Type": "text/css"},
        ),
        "https://www.nexusmods.com/static/app.js": _FakeResponse(
            content=b"console.log(1)",
            url="https://www.nexusmods.com/static/app.js",
            headers={"Content-Type": "application/javascript"},
        ),
        "https://cdn.example.com/cover.webp": _FakeResponse(
            content=b"WEBPDATA",
            url="https://cdn.example.com/cover.webp",
            headers={"Content-Type": "image/webp"},
        ),
    }
    out = tmp_path / "offline"
    with WebSnapshotDownloader(session=_session_for(pages)) as dl:
        result = dl.download(page_url, out)

    assert result.success
    assert result.html_path == out / "index.html"
    assert result.html_path.is_file()
    assert result.used_browser is False
    # Phase 1: CSS + image only (JS not downloaded)
    assert result.asset_count >= 2
    assets = out / "assets"
    assert assets.is_dir()
    assert list(assets.iterdir()), "assets/ should contain downloaded files"
    assert not any(p.suffix.lower() == ".js" for p in assets.iterdir())

    saved = result.html_path.read_text(encoding="utf-8")
    assert "cdn.example.com/main.css" not in saved
    assert "cdn.example.com/cover.webp" not in saved
    assert 'href="assets/' in saved
    assert 'src="assets/' in saved
    assert "Nexus Mod 336" in saved
    # Script src left remote (not localized in phase 1)
    assert "/static/app.js" in saved or "app.js" in saved


def test_github_like_snapshot_rewrites_assets(tmp_path: Path) -> None:
    page_url = "https://github.com/UE4SS-RE/RE-UE4SS"
    html = """<!DOCTYPE html>
<html><head>
<link href="https://github.githubassets.com/assets/global.css" rel="stylesheet">
</head>
<body>
<img src="https://avatars.githubusercontent.com/u/1?s=64" alt="avatar">
<article class="markdown-body"><h1>RE-UE4SS</h1></article>
</body></html>
"""
    pages = {
        page_url: _FakeResponse(text=html, url=page_url),
        "https://github.githubassets.com/assets/global.css": _FakeResponse(
            content=b".markdown-body{font:14px}",
            url="https://github.githubassets.com/assets/global.css",
            headers={"Content-Type": "text/css"},
        ),
        "https://avatars.githubusercontent.com/u/1?s=64": _FakeResponse(
            content=b"PNG",
            url="https://avatars.githubusercontent.com/u/1?s=64",
            headers={"Content-Type": "image/png"},
        ),
    }
    out = tmp_path / "offline"
    with WebSnapshotDownloader(session=_session_for(pages)) as dl:
        result = dl.download(page_url, out)

    assert result.success
    assert (out / "assets").is_dir()
    assert any((out / "assets").iterdir())
    saved = result.html_path.read_text(encoding="utf-8")
    assert "github.githubassets.com" not in saved
    assert 'href="assets/' in saved
    assert 'src="assets/' in saved
    assert "RE-UE4SS" in saved


def test_snapshot_empty_url_fails(tmp_path: Path) -> None:
    with WebSnapshotDownloader(session=MagicMock()) as dl:
        result = dl.download("", tmp_path / "out")
    assert result.success is False
    assert result.error
    assert result.asset_count == 0
