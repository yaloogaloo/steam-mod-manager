"""GitHub offline webpage snapshot (no API)."""

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
from services.offline.snapshot import SnapshotResult, WebSnapshotDownloader


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "github_offline.db")
    yield manager
    DatabaseManager.reset_instance()


def test_github_snapshots_repo_page(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    def fake_download(self, url: str, output_dir: Path):
        assert url == "https://github.com/owner/cool-repo"
        out = Path(output_dir)
        assets = out / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "global.css").write_text(".repo{}", encoding="utf-8")
        index = out / "index.html"
        index.write_text(
            '<html><link href="assets/global.css">'
            "<h1>owner/cool-repo</h1><div class=markdown-body>README</div></html>",
            encoding="utf-8",
        )
        return SnapshotResult(success=True, html_path=index, asset_count=1)

    monkeypatch.setattr(WebSnapshotDownloader, "download", fake_download)

    result = GithubOfflineProvider().update_offline_page(
        mid, managed_path=folder, library_root=lib
    )

    html = result.index_path.read_text(encoding="utf-8")
    assert "owner/cool-repo" in html
    assert 'href="assets/global.css"' in html
    assert result.index_path == info_dir / "offline" / "index.html"
    assert result.status == OFFLINE_STATUS_ARCHIVED
    assert result.provider == PROVIDER_GITHUB_SNAPSHOT
