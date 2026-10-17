"""ConflictDetector — deploy_manifest target path overlap (V1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_status import CONFLICT_STATUS_NONE
from services.conflict import ConflictDetector
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    save_manifest,
)
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "conflict.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed_mod(
    library: Path,
    db: DatabaseManager,
    *,
    external_id: str,
    title: str = "",
) -> tuple[Path, str]:
    game = library / "TestGame"
    folder = game / external_id
    folder.mkdir(parents=True, exist_ok=True)
    created = create_steam_test_mod(
        db, external_id=external_id, title=title or external_id
    )
    pk = str(created.mod_id)
    prove_managed_folder(
        db, folder, handle=pk, title=title or external_id
    )
    return folder, pk


def _write_manifest(folder: Path, mod_id: str, targets: list[str]) -> None:
    man = DeployManifest(
        mod_id=mod_id,
        deploy_time="2020-01-01T00:00:00+00:00",
        deploy_type="folder_copy",
        files=[
            ManifestFileEntry(source=f"src/{Path(t).name}", target=t)
            for t in targets
        ],
    )
    save_manifest(folder, man)


def test_same_target_is_conflict(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "a.pak").resolve())
    a, pk_a = _seed_mod(library, db, external_id="1001", title="A")
    b, pk_b = _seed_mod(library, db, external_id="1002", title="B")
    _write_manifest(a, pk_a, [shared])
    _write_manifest(b, pk_b, [shared])

    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports[pk_a].status == CONFLICT_STATUS_NONE
    assert reports[pk_b].status == CONFLICT_STATUS_NONE
    assert len(reports[pk_a].conflicts) == 1
    assert set(reports[pk_a].conflicts[0].mods) == {pk_a, pk_b}
    assert reports[pk_a].conflicts[0].conflict_type == "FILE_OVERWRITE"
    assert db.get_mod_status(pk_a).conflict_status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(pk_b).conflict_status == CONFLICT_STATUS_NONE


def test_different_targets_none(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    a, pk_a = _seed_mod(library, db, external_id="2001")
    b, pk_b = _seed_mod(library, db, external_id="2002")
    # Different directories — not FILE_OVERWRITE and not same-dir PAK_OVERLAP
    _write_manifest(a, pk_a, [str((tmp_path / "game_a" / "a.pak").resolve())])
    _write_manifest(b, pk_b, [str((tmp_path / "game_b" / "b.pak").resolve())])

    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports[pk_a].status == CONFLICT_STATUS_NONE
    assert reports[pk_b].status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(pk_a).conflict_status == CONFLICT_STATUS_NONE


def test_check_mod_subset(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "x.pak").resolve())
    a, pk_a = _seed_mod(library, db, external_id="3001")
    b, pk_b = _seed_mod(library, db, external_id="3002")
    _write_manifest(a, pk_a, [shared])
    _write_manifest(b, pk_b, [shared])
    report = ConflictDetector(library, db=db).check_mod(pk_a, persist=False)
    assert report.status == CONFLICT_STATUS_NONE
    assert report.conflicts
    assert report.conflicts[0].conflict_type == "FILE_OVERWRITE"
    assert report.mod_id == pk_a
