"""GitHub local offline HTML generation (no API)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    FILE_TYPE_MAIN,
    OFFLINE_STATUS_GENERATED,
    PLATFORM_GITHUB,
    PROVIDER_GITHUB_GENERATOR,
    ModFileEntry,
    ModFilesBundle,
)
from services.file_ops import INFO_DIR_NAME
from services.offline.github import GithubOfflineProvider


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "github_offline.db")
    yield manager
    DatabaseManager.reset_instance()


def test_github_generates_repo_source_files_readme(
    tmp_path: Path, db: DatabaseManager
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
    db.set_mod_files(
        mid,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    name="pak",
                    filename="mod.pak",
                    path="mod.pak",
                    type=FILE_TYPE_MAIN,
                    enabled=True,
                )
            ]
        ),
    )

    lib = tmp_path / "library"
    folder = lib / "Palworld" / "Cool Repo"
    info_dir = folder / INFO_DIR_NAME
    info_dir.mkdir(parents=True)
    (info_dir / "mod.json").write_text(
        json.dumps({"published_file_id": mid, "title": "Cool Repo"}),
        encoding="utf-8",
    )
    (folder / "README.md").write_text(
        "# Hello\n\nThis is a local README summary.",
        encoding="utf-8",
    )

    result = GithubOfflineProvider().update_offline_page(
        mid, managed_path=folder, library_root=lib
    )

    html = result.index_path.read_text(encoding="utf-8")
    assert "owner/cool-repo" in html
    assert "https://github.com/owner/cool-repo" in html
    assert "mod.pak" in html or "pak" in html
    assert "Hello" in html
    assert "local README" in html
    assert result.status == OFFLINE_STATUS_GENERATED
    assert result.provider == PROVIDER_GITHUB_GENERATOR
