"""Folder-copy deploy timing on synthetic fixtures (regression guard, not benchmark suite)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_TYPE_FOLDER_COPY, DatabaseManager
from services.deploy import ModDeployer
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    write_info_sidecar,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "perf.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _make_fixture(db: DatabaseManager, library: Path, *, size_mb: int, mid: str) -> Path:
    folder = library / "Palworld" / f"Perf{size_mb}MB"
    folder.mkdir(parents=True)
    (folder / "data.bin").write_bytes(b"x" * (size_mb * 1024 * 1024))
    created = create_steam_test_mod(
        db, external_id=mid, title=f"Perf{size_mb}", app_id=1623730, game_name="Palworld"
    )
    write_info_sidecar(
        folder,
        internal_id=str(created.mod_id),
        title=f"Perf{size_mb}MB",
        external_id=mid,
        workspace_id=str(created.workspace_id or mid),
        app_id=1623730,
        game_name="Palworld",
    )
    bind_managed_path(
        db, created.mod_id, folder, title=f"Perf{size_mb}MB", game_name="Palworld"
    )
    return folder


@pytest.mark.parametrize("size_mb", [10, 18])
def test_folder_copy_fixture_seconds_level(
    tmp_path: Path, db: DatabaseManager, size_mb: int
) -> None:
    library = tmp_path / "mod"
    mods = tmp_path / "Mods"
    mods.mkdir()
    mid = f"3780{size_mb:04d}"
    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        mod_path=str(mods),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    _make_fixture(db, library, size_mb=size_mb, mid=mid)

    deployer = ModDeployer(library_root=library, db=db)
    t0 = time.perf_counter()
    out = deployer.deploy_mod(mid)
    elapsed = time.perf_counter() - t0
    assert out.get("success") is True, out
    # Structural fix: no unbounded rglob — 18MB should finish quickly on tmpfs.
    assert elapsed < 15.0, f"deploy took {elapsed:.2f}s for {size_mb}MB fixture"
