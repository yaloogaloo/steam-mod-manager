"""Browser-level offline snapshot (Playwright capture + resource rewrite)."""

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
from services.offline.browser_snapshot.manager import (
    BrowserSnapshotProvider,
    BrowserSnapshotResult,
)
from services.offline.browser_snapshot.playwright_capture import (
    PageCapture,
    PlaywrightCapture,
    PlaywrightCaptureError,
)
from services.offline.browser_snapshot.resource_rewriter import ResourceRewriter
from services.offline.github import GithubOfflineProvider
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
    manager = DatabaseManager.instance(tmp_path / "browser_snap.db")
    yield manager
    DatabaseManager.reset_instance()


def test_css_url_rewrite_and_manifest(tmp_path: Path) -> None:
    page_url = "https://example.com/mod/1"
    html = """<!DOCTYPE html>
<html><head>
<link rel="stylesheet" href="https://cdn.example.com/app.css">
</head>
<body>
<img src="https://cdn.example.com/cover.png" alt="c">
<h1>Hello Snapshot</h1>
</body></html>
"""
    css = "body{background:url('https://cdn.example.com/bg.webp')} .x{color:red}"
    pages = {
        "https://cdn.example.com/app.css": _FakeResponse(
            content=css.encode("utf-8"),
            url="https://cdn.example.com/app.css",
            headers={"Content-Type": "text/css"},
        ),
        "https://cdn.example.com/cover.png": _FakeResponse(
            content=b"PNGDATA",
            url="https://cdn.example.com/cover.png",
            headers={"Content-Type": "image/png"},
        ),
        "https://cdn.example.com/bg.webp": _FakeResponse(
            content=b"WEBPDATA",
            url="https://cdn.example.com/bg.webp",
            headers={"Content-Type": "image/webp"},
        ),
    }
    capture = PageCapture(
        html=html,
        page_url=page_url,
        discovered_urls=[
            "https://cdn.example.com/app.css",
            "https://cdn.example.com/cover.png",
        ],
        stylesheets=[],
    )
    out = tmp_path / "offline"
    with ResourceRewriter(session=_session_for(pages)) as rewriter:
        result = rewriter.rewrite(capture, out)

    assert result.success
    assert result.html_path == out / "index.html"
    assert result.html_path.is_file()
    assert result.manifest_path == out / "snapshot_manifest.json"
    assert result.manifest_path.is_file()

    saved = result.html_path.read_text(encoding="utf-8")
    assert "Hello Snapshot" in saved
    assert "./assets/" in saved or 'href="./assets/' in saved
    assert "cdn.example.com/app.css" not in saved
    assert "cdn.example.com/cover.png" not in saved

    # CSS nested url() rewritten to local filename inside assets/
    css_files = list((out / "assets").glob("*.css"))
    assert css_files
    css_text = css_files[0].read_text(encoding="utf-8")
    assert "cdn.example.com/bg.webp" not in css_text
    assert "bg" in css_text or ".webp" in css_text

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert isinstance(manifest, list)
    assert any(e.get("status") == "success" and e.get("type") == "css" for e in manifest)
    assert any(e.get("type") == "image" and e.get("status") == "success" for e in manifest)


def test_browser_provider_writes_offline_index(tmp_path: Path) -> None:
    page_url = "https://github.com/owner/repo"
    html = "<!DOCTYPE html><html><body><h1>Repo</h1></body></html>"
    capture = PageCapture(html=html, page_url=page_url, discovered_urls=[], stylesheets=[])

    fake_capture = MagicMock(spec=PlaywrightCapture)
    fake_capture.capture.return_value = capture

    out = tmp_path / "offline"
    provider = BrowserSnapshotProvider(
        capture=fake_capture,
        rewriter=ResourceRewriter(session=_session_for({})),
        enable_legacy_fallback=False,
    )
    result = provider.snapshot(page_url, out)
    assert result.success
    assert result.used_fallback is False
    assert result.backend == "browser"
    assert (out / "index.html").is_file()
    assert "Repo" in (out / "index.html").read_text(encoding="utf-8")
    assert (out / "snapshot_manifest.json").is_file()


def test_playwright_failure_falls_back_to_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page_url = "https://www.nexusmods.com/palworld/mods/96"
    html = """<!DOCTYPE html><html><head>
<link rel="stylesheet" href="https://cdn.example.com/n.css">
</head><body><h1>Legacy OK</h1></body></html>"""
    pages = {
        page_url: _FakeResponse(text=html, url=page_url),
        "https://cdn.example.com/n.css": _FakeResponse(
            content=b"body{}",
            url="https://cdn.example.com/n.css",
            headers={"Content-Type": "text/css"},
        ),
    }

    failing = MagicMock(spec=PlaywrightCapture)
    failing.capture.side_effect = PlaywrightCaptureError("Playwright not installed")

    # Patch legacy downloader session via WebSnapshotDownloader constructor injection
    # by patching the class used inside _legacy_fallback.
    from services.offline import snapshot as legacy_mod

    original_cls = legacy_mod.WebSnapshotDownloader

    def _factory(*args, **kwargs):
        kwargs.setdefault("session", _session_for(pages))
        kwargs.setdefault("enable_browser_fallback", False)
        return original_cls(*args, **kwargs)

    monkeypatch.setattr(legacy_mod, "WebSnapshotDownloader", _factory)

    out = tmp_path / "offline"
    provider = BrowserSnapshotProvider(
        capture=failing,
        enable_legacy_fallback=True,
    )
    result = provider.snapshot(page_url, out)
    assert result.success is True
    assert result.used_fallback is True
    assert result.backend == "legacy"
    assert (out / "index.html").is_file()
    assert "Legacy OK" in (out / "index.html").read_text(encoding="utf-8")


def test_github_provider_calls_browser_snapshot(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """GitHub still routes through injectable browser/layout snapshot."""
    lib = tmp_path / "library"
    lib.mkdir()

    github = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="o/r",
        source_url="https://github.com/o/r",
        title="GithubMod",
        app_id=1623730,
        game_name="Palworld",
    )

    folder = lib / "Palworld" / "GithubMod"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps({"published_file_id": github.mod_id, "title": "GithubMod"}),
        encoding="utf-8",
    )

    calls: list[str] = []

    class FakeSnap(BrowserSnapshotProvider):
        def snapshot(self, url: str, output_dir: Path | str) -> BrowserSnapshotResult:
            calls.append(url)
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "assets").mkdir(exist_ok=True)
            index = out / "index.html"
            index.write_text(f"<html>{url}</html>", encoding="utf-8")
            manifest = out / "snapshot_manifest.json"
            manifest.write_text("[]", encoding="utf-8")
            return BrowserSnapshotResult(
                success=True,
                html_path=index,
                asset_count=0,
                used_fallback=False,
                manifest_path=manifest,
                backend="browser",
            )

    gr = GithubOfflineProvider(snapshot_provider=FakeSnap()).update_offline_page(
        github.mod_id, managed_path=folder, library_root=lib
    )

    assert calls == ["https://github.com/o/r"]
    assert gr.provider == PROVIDER_GITHUB_SNAPSHOT
    assert gr.status == OFFLINE_STATUS_ARCHIVED
    assert gr.index_path.as_posix().endswith(".info/offline/index.html")


def test_nexus_update_offline_requires_manual_import(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Nexus no longer auto-scrapes; update_offline_page points users to import."""
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
        json.dumps({"published_file_id": info.mod_id, "title": "N"}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="手动导入"):
        NexusManualOfflineProvider().update_offline_page(
            info.mod_id, managed_path=folder, library_root=lib
        )
    refreshed = db.get_mod_display_info(info.mod_id)
    assert refreshed is not None
    assert refreshed.offline_status == OFFLINE_STATUS_FAILED
    assert refreshed.offline_provider == PROVIDER_NEXUS_MANUAL_IMPORT
    assert not (folder / INFO_DIR_NAME / "offline" / "index.html").is_file()
