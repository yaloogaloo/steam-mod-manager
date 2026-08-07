"""ConflictDetector — deploy_manifest target path overlap (V1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_status import CONFLICT_STATUS_CONFLICT, CONFLICT_STATUS_NONE
from core.models import ModMetadata
from services.conflict import ConflictDetector
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    save_manifest,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "conflict.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed_mod(library: Path, mod_id: str, *, title: str = "") -> Path:
    game = library / "TestGame"
    folder = game / mod_id
    folder.mkdir(parents=True, exist_ok=True)
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        f'{{"published_file_id": "{mod_id}", "title": "{title or mod_id}"}}',
        encoding="utf-8",
    )
    return folder


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
    a = _seed_mod(library, "1001", title="A")
    b = _seed_mod(library, "1002", title="B")
    _write_manifest(a, "1001", [shared])
    _write_manifest(b, "1002", [shared])
    db.upsert_mod(ModMetadata(published_file_id="1001", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="1002", title="B"))

    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports["1001"].status == CONFLICT_STATUS_CONFLICT
    assert reports["1002"].status == CONFLICT_STATUS_CONFLICT
    assert len(reports["1001"].conflicts) == 1
    assert set(reports["1001"].conflicts[0].mods) == {"1001", "1002"}
    assert db.get_mod_status(1001).conflict_status == CONFLICT_STATUS_CONFLICT
    assert db.get_mod_status(1002).conflict_status == CONFLICT_STATUS_CONFLICT


def test_different_targets_none(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    a = _seed_mod(library, "2001")
    b = _seed_mod(library, "2002")
    # Different directories — not FILE_OVERWRITE and not same-dir PAK_OVERLAP
    _write_manifest(a, "2001", [str((tmp_path / "game_a" / "a.pak").resolve())])
    _write_manifest(b, "2002", [str((tmp_path / "game_b" / "b.pak").resolve())])
    db.upsert_mod(ModMetadata(published_file_id="2001", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="2002", title="B"))

    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports["2001"].status == CONFLICT_STATUS_NONE
    assert reports["2002"].status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(2001).conflict_status == CONFLICT_STATUS_NONE


def test_check_mod_subset(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "x.pak").resolve())
    a = _seed_mod(library, "3001")
    b = _seed_mod(library, "3002")
    _write_manifest(a, "3001", [shared])
    _write_manifest(b, "3002", [shared])
    report = ConflictDetector(library, db=db).check_mod(3001, persist=False)
    assert report.status == CONFLICT_STATUS_CONFLICT
    assert report.mod_id == "3001"
