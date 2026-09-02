"""Folder-copy deploy timing on synthetic fixtures (regression guard, not benchmark suite)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_TYPE_FOLDER_COPY, DatabaseManager
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "perf.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _make_fixture(library: Path, *, size_mb: int, mid: str) -> Path:
    folder = library / "Palworld" / f"Perf{size_mb}MB"
    folder.mkdir(parents=True)
    info = folder / INFO_DIR_NAME
    info.mkdir()
    (folder / "data.bin").write_bytes(b"x" * (size_mb * 1024 * 1024))
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "Perf{size_mb}MB",\n'
        '  "app_id": 1623730,\n'
        '  "game_name": "Palworld"\n'
        "}\n",
        encoding="utf-8",
    )
    return folder


@pytest.mark.parametrize("size_mb", [10, 18])
def test_folder_copy_fixture_seconds_level(
    tmp_path: Path, db: DatabaseManager, size_mb: int
) -> None:
    library = tmp_path / "mod"
    mods = tmp_path / "Mods"
    mods.mkdir()
    _make_fixture(library, size_mb=size_mb, mid=f"3780{size_mb:04d}")
    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        mod_path=str(mods),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id=f"3780{size_mb:04d}",
            title=f"Perf{size_mb}",
            app_id=1623730,
        )
    )
    deployer = ModDeployer(library_root=library, db=db)
    t0 = time.perf_counter()
    out = deployer.deploy_mod(f"3780{size_mb:04d}")
    elapsed = time.perf_counter() - t0
    assert out.get("success") is True, out
    # Structural fix: no unbounded rglob — 18MB should finish quickly on tmpfs.
    assert elapsed < 15.0, f"deploy took {elapsed:.2f}s for {size_mb}MB fixture"
