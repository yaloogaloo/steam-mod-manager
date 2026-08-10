"""Readable offline snapshot — parse main content, local CSS, no image downloads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    PLATFORM_NEXUS,
    PROVIDER_NEXUS_MANUAL_IMPORT,
)
from services.file_ops import INFO_DIR_NAME
from services.offline.browser_snapshot.manager import (
    BrowserSnapshotProvider,
    BrowserSnapshotResult,
)
from services.offline.nexus_manual import NexusManualOfflineProvider
from services.offline.readable_snapshot import (
    GithubReadableParser,
    NexusReadableParser,
    ReadableSnapshotProvider,
    ReadableSnapshotResult,
    _is_cloudflare_challenge,
    render_readable_html,
    run_readable_offline_snapshot,
    write_readable_page,
)


NEXUS_SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head><title>Cool Mod at Nexus</title>
<script src="/cdn/app.bundle.js"></script>
<link rel="stylesheet" href="/cdn/app.css">
</head>
<body>
<header class="site-header">Nav / Login</header>
<main class="mod-page">
  <h1>Cool Nexus Mod</h1>
  <a href="/users/42">ModAuthor</a>
  <div class="tags"><a class="tag">Gameplay</a><a class="tag">UI</a></div>
  <dl>
    <dt>Version</dt><dd>1.2.3</dd>
    <dt>Unique DLs</dt><dd>999</dd>
  </dl>
  <div class="mod-description">
    <p>Full description text for offline reading.</p>
    <img src="https://cdn.example.com/cover.png" alt="cover art">
    <script>track('ad')</script>
  </div>
  <section class="requirements">
    <ul><li>UE4SS</li><li>Base Mod</li></ul>
  </section>
  <table>
    <tr><th>Name</th><th>Size</th></tr>
    <tr><td><a>MainFile.zip</a></td><td>12 MB</td></tr>
    <tr><td><a>Optional.pak</a></td><td>1 MB</td></tr>
  </table>
</main>
<footer>Ads / tracking</footer>
</body>
</html>
"""

GITHUB_SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head><title>owner/cool-repo</title></head>
<body>
<strong itemprop="name"><a>cool-repo</a></strong>
<article class="markdown-body">
  <h1>README</h1>
  <p>Repository readme body for offline.</p>
  <img src="https://raw.githubusercontent.com/o/r/main/shot.png" alt="shot">
</article>
<a class="topic-tag" href="/topics/lua">lua</a>
<a href="/owner/cool-repo/releases/tag/v2.0.0">v2.0.0</a>
<div id="repo-stars-counter-star">123</div>
</body>
</html>
"""


def test_nexus_html_parsing_extracts_main_fields() -> None:
    page = NexusReadableParser().parse(
        NEXUS_SAMPLE_HTML,
        source_url="https://www.nexusmods.com/palworld/mods/336",
    )
    assert page.title == "Cool Nexus Mod"
    assert page.author == "ModAuthor"
    assert "Full description text" in page.description_html
    assert "img-placeholder" in page.description_html
    assert "cover art" in page.description_html
    assert "<img" not in page.description_html.lower()
    assert 'data-src="https://cdn.example.com/cover.png"' in page.description_html
    # Script must not remain in content.
    assert "<script" not in page.description_html.lower()
    assert page.version == "1.2.3"
    assert "Gameplay" in page.tags
    assert any("MainFile.zip" in f.name for f in page.files)
    assert any("UE4SS" in r for r in page.requirements)


def test_github_html_parsing_readme_and_releases() -> None:
    page = GithubReadableParser().parse(
        GITHUB_SAMPLE_HTML,
        source_url="https://github.com/owner/cool-repo",
    )
    assert "cool-repo" in page.title
    assert page.author == "owner"
    assert "Repository readme body" in page.description_html
    assert "img-placeholder" in page.description_html
    assert any("v2.0.0" in f.name for f in page.files)
    assert "lua" in page.tags


def test_layout_css_generated_and_main_content_saved(tmp_path: Path) -> None:
    provider = ReadableSnapshotProvider(
        fetch_func=lambda url: (NEXUS_SAMPLE_HTML, 200, url),
    )
    out = tmp_path / "offline"
    result = provider.snapshot(
        "https://www.nexusmods.com/palworld/mods/336",
        out,
        platform=PLATFORM_NEXUS,
    )
    assert result.success is True
    assert result.html_path.is_file()
    assert result.css_path is not None and result.css_path.is_file()
    assert result.css_path.name == "style.css"
    html = result.html_path.read_text(encoding="utf-8")
    css = result.css_path.read_text(encoding="utf-8")
    assert "Cool Nexus Mod" in html
    assert "Full description text" in html
    assert "MainFile.zip" in html
    assert 'href="./style.css"' in html
    assert ".card" in css
    assert ".section-title" in css
    assert ".meta-grid" in css
    assert result.asset_count == 0


def test_images_not_downloaded(tmp_path: Path) -> None:
    provider = ReadableSnapshotProvider(
        fetch_func=lambda url: (NEXUS_SAMPLE_HTML, 200, url),
    )
    out = tmp_path / "offline"
    result = provider.snapshot(
        "https://www.nexusmods.com/palworld/mods/336",
        out,
    )
    assert result.success is True
    assets = out / "assets"
    assert not assets.exists() or not any(assets.rglob("*"))
    # No binary image files written anywhere under output.
    written = [p for p in out.rglob("*") if p.is_file()]
    assert {p.suffix.lower() for p in written} <= {".html", ".css", ".tmp"}
    html = result.html_path.read_text(encoding="utf-8")
    assert "img-placeholder" in html
    assert "<img " not in html.lower()


def test_cloudflare_failure_degrades_to_fallback_page(tmp_path: Path) -> None:
    cf_html = """<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body>Checking your browser before accessing nexusmods.com. Cloudflare cf-ray</body></html>"""

    class FailBrowser(BrowserSnapshotProvider):
        def snapshot(self, url: str, output_dir: Path | str) -> BrowserSnapshotResult:
            return BrowserSnapshotResult(
                success=False,
                html_path=Path(output_dir) / "index.html",
                asset_count=0,
                error="Playwright blocked by Cloudflare",
                backend="browser",
            )

    readable = ReadableSnapshotProvider(
        fetch_func=lambda url: (cf_html, 403, url),
    )
    out = tmp_path / "offline"
    result, status = run_readable_offline_snapshot(
        source_url="https://www.nexusmods.com/palworld/mods/96",
        output_dir=out,
        platform=PLATFORM_NEXUS,
        readable_provider=readable,
        browser_provider=FailBrowser(),
        allow_browser_backup=True,
        allow_legacy_browser_fallback=False,
    )
    assert status == OFFLINE_STATUS_FAILED
    assert result.backend == "fallback"
    assert result.html_path.is_file()
    html = result.html_path.read_text(encoding="utf-8")
    assert "Cloudflare" in html or "challenge" in html.lower()
    assert (out / "style.css").is_file()
    # Must not scrape via legacy WebSnapshotDownloader assets.
    assert not (out / "assets").exists() or not any((out / "assets").iterdir())


def test_cloudflare_detection_helpers() -> None:
    assert _is_cloudflare_challenge("Just a moment... cloudflare", 200) is True
    assert _is_cloudflare_challenge("<html>ok</html>", 403) is True
    assert _is_cloudflare_challenge("<html><body>Normal mod page</body></html>", 200) is False


def test_nexus_provider_is_manual_import(tmp_path: Path) -> None:
    """Nexus offline pages come from user-saved HTML, not browser scrape."""
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "layout_nexus.db")
    try:
        info = db.register_external_mod(
            platform=PLATFORM_NEXUS,
            external_id="336",
            source_url="https://www.nexusmods.com/palworld/mods/336",
            title="Cool Nexus Mod",
            app_id=1623730,
            game_name="Palworld",
        )
        mid = info.mod_id
        lib = tmp_path / "library"
        folder = lib / "Palworld" / "Cool Nexus Mod"
        info_dir = folder / INFO_DIR_NAME
        info_dir.mkdir(parents=True)
        (info_dir / "mod.json").write_text(
            json.dumps({"published_file_id": mid, "title": "Cool Nexus Mod"}),
            encoding="utf-8",
        )
        html = tmp_path / "saved.html"
        html.write_text(
            "<html><h1>Cool Nexus Mod</h1><div class=desc>Full description text</div></html>",
            encoding="utf-8",
        )

        result = NexusManualOfflineProvider().import_offline_page(
            mid, html, managed_path=folder, library_root=lib
        )

        assert result.status == OFFLINE_STATUS_ARCHIVED
        assert result.provider == PROVIDER_NEXUS_MANUAL_IMPORT
        body = result.index_path.read_text(encoding="utf-8")
        assert "Cool Nexus Mod" in body
        assert "Full description text" in body
        refreshed = db.get_mod_display_info(mid)
        assert refreshed is not None
        assert refreshed.offline_status == OFFLINE_STATUS_ARCHIVED
    finally:
        DatabaseManager.reset_instance()


def test_nexus_update_offline_does_not_scrape(tmp_path: Path) -> None:
    """update_offline_page must not invent fallback HTML or call remote scrapers."""
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "nexus_fail.db")
    try:
        info = db.register_external_mod(
            platform=PLATFORM_NEXUS,
            external_id="1",
            source_url="https://www.nexusmods.com/x/mods/1",
            title="N",
            app_id=1,
            game_name="G",
        )
        folder = tmp_path / "lib" / "G" / "N"
        (folder / INFO_DIR_NAME).mkdir(parents=True)
        (folder / INFO_DIR_NAME / "mod.json").write_text(
            json.dumps({"published_file_id": info.mod_id, "title": "N"}),
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="手动导入"):
            NexusManualOfflineProvider().update_offline_page(
                info.mod_id, managed_path=folder, library_root=tmp_path / "lib"
            )
        assert not (folder / INFO_DIR_NAME / "offline" / "index.html").is_file()
    finally:
        DatabaseManager.reset_instance()


def test_render_includes_sections(tmp_path: Path) -> None:
    from services.offline.readable_snapshot import FileEntry, ReadablePage

    page = ReadablePage(
        title="T",
        author="A",
        description_html="<p>Body</p>",
        files=[FileEntry(name="a.zip", detail="1MB")],
        tags=["x"],
        version="9",
        requirements=["req"],
        source_url="https://example.com",
        platform=PLATFORM_NEXUS,
    )
    html = render_readable_html(page)
    assert "Metadata" in html
    assert "Content" in html
    assert "Files" in html
    assert "Requirements" in html
    assert "Body" in html

    index, css = write_readable_page(tmp_path / "offline", page)
    assert index.is_file()
    assert css.is_file()
