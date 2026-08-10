"""GitHub Playwright DOM offline snapshot — no asset downloader / no fallback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    PLATFORM_GITHUB,
    PROVIDER_GITHUB_SNAPSHOT,
)
from services.file_ops import INFO_DIR_NAME
from services.offline.github import GithubOfflineProvider
from services.offline.github_browser_snapshot import (
    GitHubBrowserSnapshot,
    capture_github_page_content,
    is_empty_react_shell,
    looks_like_rendered_github_repo,
    validate_github_dom,
)


GITHUB_RENDERED_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>UE4SS-RE/RE-UE4SS</title>
  <link rel="stylesheet" href="https://github.githubassets.com/assets/light-xyz.css">
  <style>.Header{display:flex}.markdown-body{color:#e6edf3}</style>
  <script src="https://github.githubassets.com/assets/chunk.js"></script>
</head>
<body>
  <header class="Header AppHeader"><a href="/login">Sign in</a></header>
  <div id="js-repo-pjax-container" class="repository-content">
    <strong itemprop="name">RE-UE4SS</strong>
    <nav><a>Code</a><a>Issues</a></nav>
    <div class="Box">
      <div class="Box-row react-directory-filename-column">src</div>
      <div class="Box-row react-directory-filename-column">README.md</div>
      <div class="Box-row react-directory-filename-column">LICENSE</div>
    </div>
    <div id="readme" data-testid="readme">
      <article class="markdown-body">
        <h1>RE-UE4SS</h1>
        <p>Injectable LUA scripting system for Unreal Engine games.</p>
      </article>
    </div>
    <aside class="Layout-sidebar"><h2>About</h2></aside>
  </div>
</body>
</html>
"""


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "gh_browser.db")
    yield manager
    DatabaseManager.reset_instance()


def test_empty_root_rejected() -> None:
    shell = '<html><body><div id="root"></div></body></html>'
    assert is_empty_react_shell(shell) is True
    assert validate_github_dom(shell) == "UNRENDERED_REACT_SHELL"


def test_sign_in_header_ok_when_repo_rendered() -> None:
    assert looks_like_rendered_github_repo(GITHUB_RENDERED_HTML) is True
    assert validate_github_dom(GITHUB_RENDERED_HTML) is None


def test_saves_playwright_dom_keeps_styles_no_asset_download(tmp_path: Path) -> None:
    out = tmp_path / "offline"
    html_in = GITHUB_RENDERED_HTML.replace(
        '<nav><a>Code</a><a>Issues</a></nav>',
        '<nav>'
        '<a href="/UE4SS-RE/RE-UE4SS">Code</a>'
        '<a href="/UE4SS-RE/RE-UE4SS/issues">Issues</a>'
        '<a href="/UE4SS-RE/RE-UE4SS/releases">Releases</a>'
        "</nav>",
    )
    with GitHubBrowserSnapshot(capture_func=lambda _u: html_in) as snap:
        result = snap.snapshot("https://github.com/UE4SS-RE/RE-UE4SS", out)

    assert result.success is True
    assert result.source == "playwright_page_content"
    assert result.backend == "playwright"
    html = result.html_path.read_text(encoding="utf-8")
    assert "RE-UE4SS" in html
    assert "Injectable LUA scripting system" in html
    assert "react-directory-filename-column" in html or "Box-row" in html
    assert "<style>" in html or "stylesheet" in html.lower()
    assert "github.githubassets.com" in html  # remote CSS href preserved
    assert "chunk.js" not in html  # scripts stripped
    assert "GitHub snapshot failed" not in html
    assert "LOGIN_REQUIRED" not in html
    assert 'smm-offline-provider" content="github-fallback"' not in html
    # Relative nav links rewritten to absolute GitHub URLs (not file://).
    assert 'href="https://github.com/UE4SS-RE/RE-UE4SS/issues"' in html
    assert 'href="https://github.com/UE4SS-RE/RE-UE4SS/releases"' in html
    assert "file://" not in html
    # No downloaded CSS files — assets dir may exist empty.
    css_files = list((out / "assets").rglob("*.css")) if (out / "assets").exists() else []
    assert css_files == []
    meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert meta["source"] == "playwright_page_content"
    assert meta["snapshot_type"] == "browser_dom"


def test_capture_failure_does_not_write_fallback(tmp_path: Path) -> None:
    def boom(_url: str) -> str:
        raise RuntimeError("no browser")

    out = tmp_path / "offline"
    with GitHubBrowserSnapshot(capture_func=boom) as snap:
        result = snap.snapshot("https://github.com/UE4SS-RE/RE-UE4SS", out)
    assert result.success is False
    assert not (out / "index.html").is_file()
    assert "fallback" not in (result.error or "").lower()


def test_provider_uses_playwright_result(tmp_path: Path, db: DatabaseManager) -> None:
    info = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="UE4SS-RE/RE-UE4SS",
        source_url="https://github.com/UE4SS-RE/RE-UE4SS",
        title="RE-UE4SS",
        app_id=1623730,
        game_name="Palworld",
    )
    lib = tmp_path / "library"
    folder = lib / "Palworld" / "RE-UE4SS"
    info_dir = folder / INFO_DIR_NAME
    info_dir.mkdir(parents=True)
    (info_dir / "mod.json").write_text(
        json.dumps({"published_file_id": info.mod_id, "title": "RE-UE4SS"}),
        encoding="utf-8",
    )

    snap = GitHubBrowserSnapshot(capture_func=lambda _u: GITHUB_RENDERED_HTML)
    result = GithubOfflineProvider(browser_snapshot=snap).update_offline_page(
        info.mod_id, managed_path=folder, library_root=lib
    )
    assert result.status == OFFLINE_STATUS_ARCHIVED
    assert result.provider == PROVIDER_GITHUB_SNAPSHOT
    html = result.index_path.read_text(encoding="utf-8")
    assert "Injectable LUA scripting system" in html
    assert "GitHub snapshot failed" not in html


@pytest.mark.network
def test_real_ue4ss_github_playwright_acceptance(tmp_path: Path) -> None:
    """
    Live acceptance: Playwright must produce a real GitHub repo DOM.

    Requires network + Playwright Chromium.
    """
    pytest.importorskip("playwright")
    url = "https://github.com/UE4SS-RE/RE-UE4SS"
    out = tmp_path / "offline"

    # Prove HTML originates from Playwright page.content(), not requests.
    raw = capture_github_page_content(url, timeout_ms=30_000, render_wait_ms=5_000)
    assert validate_github_dom(raw) is None
    assert "RE-UE4SS" in raw
    assert "markdown-body" in raw.lower() or "readme" in raw.lower()

    with GitHubBrowserSnapshot() as snap:
        result = snap.snapshot(url, out)

    assert result.success is True
    assert result.source == "playwright_page_content"
    html = result.html_path.read_text(encoding="utf-8")
    assert "RE-UE4SS" in html
    assert "UE4SS-RE" in html or "ue4ss" in html.lower()
    # README body present (not empty root).
    assert "markdown-body" in html.lower() or "readme" in html.lower()
    assert "GitHub snapshot failed" not in html
    assert "LOGIN_REQUIRED" not in html
    # Not a resource-pile / summary fallback.
    assert "github-readme-fallback" not in html
    assert "github_fallback.css" not in html
    meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert meta["source"] == "playwright_page_content"
