"""GitHub offline Playwright DOM snapshot."""

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
    GitHubBrowserSnapshotResult,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "github_offline.db")
    yield manager
    DatabaseManager.reset_instance()


def test_github_snapshots_repo_page(tmp_path: Path, db: DatabaseManager) -> None:
    info = db.register_external_mod(
        platform=PLATFORM_GITHUB,
        external_id="owner/cool-repo",
        source_url="https://github.com/owner/cool-repo",
        title="Cool Repo",
        app_id=1623730,
        game_name="Palworld",
    )
    mid = info.mod_id

    lib = tmp_path / "library"
    folder = lib / "Palworld" / "Cool Repo"
    info_dir = folder / INFO_DIR_NAME
    info_dir.mkdir(parents=True)
    (info_dir / "mod.json").write_text(
        json.dumps({"published_file_id": mid, "title": "Cool Repo"}),
        encoding="utf-8",
    )

    class FakeBrowser(GitHubBrowserSnapshot):
        def snapshot(self, url: str, output_dir: Path | str) -> GitHubBrowserSnapshotResult:
            assert url == "https://github.com/owner/cool-repo"
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / "assets").mkdir(exist_ok=True)
            index = out / "index.html"
            index.write_text(
                "<!DOCTYPE html><html><head>"
                '<link rel="stylesheet" href="https://github.githubassets.com/x.css">'
                "<style>.repo{}</style></head><body>"
                '<div class="repository-content" id="js-repo-pjax-container">'
                "<h1>owner/cool-repo</h1>"
                '<div class="Box-row">README.md</div>'
                '<article class="markdown-body" id="readme">README body here</article>'
                "</div></body></html>",
                encoding="utf-8",
            )
            (out / "metadata.json").write_text(
                json.dumps(
                    {
                        "provider": "github_snapshot",
                        "snapshot_type": "browser_dom",
                        "source": "playwright_page_content",
                        "source_url": url,
                    }
                ),
                encoding="utf-8",
            )
            return GitHubBrowserSnapshotResult(
                success=True,
                html_path=index,
                backend="playwright",
                source="playwright_page_content",
            )

    result = GithubOfflineProvider(browser_snapshot=FakeBrowser()).update_offline_page(
        mid, managed_path=folder, library_root=lib
    )

    html = result.index_path.read_text(encoding="utf-8")
    assert "owner/cool-repo" in html
    assert "README" in html
    assert "GitHub snapshot failed" not in html
    assert result.index_path == info_dir / "offline" / "index.html"
    assert result.status == OFFLINE_STATUS_ARCHIVED
    assert result.provider == PROVIDER_GITHUB_SNAPSHOT
