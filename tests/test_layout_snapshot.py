"""Layout-preserving offline snapshots for Nexus / GitHub."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PROVIDER_GITHUB_SNAPSHOT,
    PROVIDER_NEXUS_MANUAL_IMPORT,
)
from services.file_ops import INFO_DIR_NAME
from services.offline.github import GithubOfflineProvider
from services.offline.layout_snapshot import (
    LayoutSnapshotDownloader,
    LayoutSnapshotResult,
    NexusSnapshotProvider,
    write_github_fallback,
    write_nexus_fallback,
)
from services.offline.nexus_manual import NexusManualOfflineProvider


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
        key = url.split("#", 1)[0]
        if key in pages:
            return pages[key]
        for known, resp in pages.items():
            if key.rstrip("/") == known.rstrip("/"):
                return resp
        raise AssertionError(f"Unexpected GET {url!r}")

    session.get.side_effect = _get
    return session


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "layout_snap.db")
    yield manager
    DatabaseManager.reset_instance()


def test_github_layout_keeps_title_readme_css(tmp_path: Path) -> None:
    page_url = "https://github.com/owner/cool-repo"
    html = """<!DOCTYPE html>
<html><head>
<title>owner/cool-repo</title>
<link rel="stylesheet" href="https://cdn.example.com/github.css">
<script src="https://cdn.example.com/huge.bundle.js"></script>
</head>
<body>
<main>
  <h1 class="repo-title">owner / cool-repo</h1>
  <article class="markdown-body"><h1>README</h1><p>Hello offline</p></article>
  <img src="https://cdn.example.com/avatar.png" class="avatar" alt="avatar">
  <img src="https://cdn.example.com/noise.webp" alt="noise">
</main>
</body></html>
"""
    pages = {
        page_url: _FakeResponse(text=html, url=page_url),
        "https://cdn.example.com/github.css": _FakeResponse(
            content=b".markdown-body{color:#fff}",
            headers={"Content-Type": "text/css"},
            url="https://cdn.example.com/github.css",
        ),
        "https://cdn.example.com/avatar.png": _FakeResponse(
            content=b"PNG",
            headers={"Content-Type": "image/png"},
            url="https://cdn.example.com/avatar.png",
        ),
        "https://cdn.example.com/noise.webp": _FakeResponse(
            content=b"WEBP",
            headers={"Content-Type": "image/webp"},
            url="https://cdn.example.com/noise.webp",
        ),
        "https://cdn.example.com/huge.bundle.js": _FakeResponse(
            content=b"console.log(1)",
            headers={"Content-Type": "application/javascript"},
            url="https://cdn.example.com/huge.bundle.js",
        ),
    }
    out = tmp_path / "offline"
    with LayoutSnapshotDownloader(
        session=_session_for(pages), platform=PLATFORM_GITHUB
    ) as dl:
        result = dl.download(page_url, out)

    assert result.success
    assert result.backend == "layout"
    assert result.html_path.is_file()
    saved = result.html_path.read_text(encoding="utf-8")
    assert "cool-repo" in saved
    assert "README" in saved or "Hello offline" in saved
    assert "./assets/" in saved
    assert "huge.bundle.js" not in saved
    assert not list((out / "assets").glob("*.js"))
    assert (out / "metadata.json").is_file()
    meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert meta["source_url"] == page_url
    assert meta["provider"] == "layout_snapshot"
    assert any(p.suffix == ".css" for p in (out / "assets").rglob("*"))


def test_nexus_layout_title_description_metadata(tmp_path: Path) -> None:
    page_url = "https://www.nexusmods.com/palworld/mods/96"
    html = """<!DOCTYPE html>
<html><head>
<title>Cool Nexus Mod</title>
<link rel="stylesheet" href="/static/nexus.css">
</head>
<body>
<div class="mod-page">
  <h1>Cool Nexus Mod</h1>
  <div class="mod-description"><p>A long description.</p></div>
  <aside class="metadata"><span class="author">AuthorX</span></aside>
</div>
</body></html>
"""
    pages = {
        page_url: _FakeResponse(text=html, url=page_url),
        "https://www.nexusmods.com/static/nexus.css": _FakeResponse(
            content=b".mod-page{display:block}",
            headers={"Content-Type": "text/css"},
            url="https://www.nexusmods.com/static/nexus.css",
        ),
    }
    out = tmp_path / "offline"
    with LayoutSnapshotDownloader(
        session=_session_for(pages), platform=PLATFORM_NEXUS
    ) as dl:
        result = dl.download(page_url, out)

    assert result.success
    html_out = result.html_path.read_text(encoding="utf-8")
    assert "Cool Nexus Mod" in html_out
    assert "long description" in html_out
    assert "./assets/" in html_out
    assert (out / "metadata.json").is_file()


def test_fallback_is_not_plain_text(tmp_path: Path) -> None:
    gh = write_github_fallback(
        tmp_path / "gh",
        source_url="https://github.com/o/r",
        reason="BLOCKED_BY_ANTI_BOT",
        title="o/r",
    )
    text = gh.read_text(encoding="utf-8")
    assert "README" in text
    assert "tabs" in text
    assert '<link rel="stylesheet"' in text
    assert "github_fallback.css" in text

    nx = write_nexus_fallback(
        tmp_path / "nx",
        source_url="https://www.nexusmods.com/palworld/mods/1",
        reason="BLOCKED_BY_ANTI_BOT",
        title="Mod One",
    )
    ntext = nx.read_text(encoding="utf-8")
    assert "Description" in ntext
    assert "Metadata" in ntext
    assert "Files" in ntext
    assert "nexus_fallback.css" in ntext
    assert 'class="hero"' in ntext or 'class="wrap"' in ntext


def test_cloudflare_uses_styled_fallback_not_crash(tmp_path: Path) -> None:
    page_url = "https://www.nexusmods.com/palworld/mods/96"
    cf_html = """<!DOCTYPE html><html><body>
<title>Just a moment...</title>
<div>Checking your browser before accessing</div>
<div id="cf-chl-widget"></div>
</body></html>"""

    def fetch(_url: str) -> tuple[str, int, str]:
        return cf_html, 403, page_url

    out = tmp_path / "offline"
    provider = NexusSnapshotProvider(
        downloader=LayoutSnapshotDownloader(fetch_func=fetch, platform=PLATFORM_NEXUS),
        allow_browser_backup=False,
    )
    result = provider.snapshot(page_url, out)
    assert result.html_path.is_file()
    assert result.used_fallback is True
    body = result.html_path.read_text(encoding="utf-8")
    assert "Description" in body
    assert "BLOCKED_BY_ANTI_BOT" in body or "fallback" in body.lower()
    assert "nexus_fallback.css" in body


def test_images_mostly_placeholder_not_bulk_download(tmp_path: Path) -> None:
    page_url = "https://example.com/page"
    html = """<!DOCTYPE html><html><head>
<link rel="stylesheet" href="https://cdn.example.com/a.css">
</head><body>
<img src="https://cdn.example.com/a1.png" alt="1">
<img src="https://cdn.example.com/a2.png" alt="2">
<img src="https://cdn.example.com/a3.png" alt="3">
<img src="https://cdn.example.com/a4.png" alt="4">
<img src="https://cdn.example.com/a5.png" alt="5">
</body></html>"""
    pages = {
        page_url: _FakeResponse(text=html, url=page_url),
        "https://cdn.example.com/a.css": _FakeResponse(
            content=b"body{}", headers={"Content-Type": "text/css"}
        ),
    }
    for i in range(1, 6):
        pages[f"https://cdn.example.com/a{i}.png"] = _FakeResponse(
            content=b"PNG", headers={"Content-Type": "image/png"}
        )

    out = tmp_path / "offline"
    with LayoutSnapshotDownloader(
        session=_session_for(pages), download_images=False
    ) as dl:
        result = dl.download(page_url, out)
    assert result.success
    saved = result.html_path.read_text(encoding="utf-8")
    assert "smm-img-placeholder" in saved
    img_dir = out / "assets" / "images"
    if img_dir.exists():
        assert list(img_dir.iterdir()) == []


def test_github_provider_uses_layout_snapshot(tmp_path: Path, db: DatabaseManager) -> None:
    lib = tmp_path / "library"
    lib.mkdir()

    github = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="o/r",
        source_url="https://github.com/o/r",
        title="G",
        app_id=1,
        game_name="Palworld",
    )

    folder = lib / "Palworld" / "G"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps({"published_file_id": github.mod_id, "title": "G"}), encoding="utf-8"
    )

    class FakeLayout:
        def snapshot(self, url: str, output_dir: Path | str) -> LayoutSnapshotResult:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "assets").mkdir(exist_ok=True)
            (out / "assets" / "x.css").write_text("body{}", encoding="utf-8")
            index = out / "index.html"
            index.write_text(
                "<html><h1 class=repo-title>o/r</h1>"
                "<article class=markdown-body>README</article>"
                "<link href='./assets/x.css'></html>",
                encoding="utf-8",
            )
            (out / "metadata.json").write_text("{}", encoding="utf-8")
            return LayoutSnapshotResult(
                success=True, html_path=index, backend="layout", asset_count=1
            )

    gr = GithubOfflineProvider(layout_provider=FakeLayout()).update_offline_page(
        github.mod_id, managed_path=folder, library_root=lib
    )
    assert gr.provider == PROVIDER_GITHUB_SNAPSHOT
    assert gr.status == OFFLINE_STATUS_ARCHIVED
    assert "README" in gr.index_path.read_text(encoding="utf-8")


def test_nexus_auto_update_disabled(tmp_path: Path, db: DatabaseManager) -> None:
    """Nexus offline is manual HTML import — update_offline_page must not scrape."""
    lib = tmp_path / "library"
    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="1",
        source_url="https://www.nexusmods.com/x/mods/1",
        title="N",
        app_id=1,
        game_name="G",
    )
    folder = lib / "G" / "N"
    (folder / INFO_DIR_NAME).mkdir(parents=True)
    (folder / INFO_DIR_NAME / "mod.json").write_text(
        json.dumps({"published_file_id": info.mod_id, "title": "N"}), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="手动导入"):
        NexusManualOfflineProvider().update_offline_page(
            info.mod_id, managed_path=folder, library_root=lib
        )
    refreshed = db.get_mod_display_info(info.mod_id)
    assert refreshed is not None
    assert refreshed.offline_status == OFFLINE_STATUS_FAILED
    assert refreshed.offline_provider == PROVIDER_NEXUS_MANUAL_IMPORT
