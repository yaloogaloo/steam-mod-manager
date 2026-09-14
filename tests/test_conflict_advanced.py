"""Advanced conflict detection (FILE_OVERWRITE / PAK_OVERLAP / disabled skip)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_status import CONFLICT_STATUS_CONFLICT, CONFLICT_STATUS_NONE
from services.conflict import ConflictDetector, ConflictType
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    save_manifest,
)
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "adv_conflict.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed(
    library: Path,
    db: DatabaseManager,
    *,
    external_id: str,
    title: str = "",
) -> tuple[Path, str]:
    folder = library / "G" / external_id
    folder.mkdir(parents=True, exist_ok=True)
    created = create_steam_test_mod(
        db, external_id=external_id, title=title or external_id
    )
    pk = str(created.mod_id)
    prove_managed_folder(db, folder, handle=pk, title=title or f"M{external_id}")
    return folder, pk


def test_same_target_file_overwrite(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "Paks" / "a.pak").resolve())
    a, pk_a = _seed(library, db, external_id="1")
    b, pk_b = _seed(library, db, external_id="2")
    for folder, pk in ((a, pk_a), (b, pk_b)):
        save_manifest(
            folder,
            DeployManifest(
                mod_id=pk,
                deploy_time="t",
                deploy_type="folder_copy",
                files=[ManifestFileEntry(source="x", target=shared)],
            ),
        )
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports[pk_a].status == CONFLICT_STATUS_NONE
    assert reports[pk_a].conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value


def test_same_dir_different_pak_is_not_conflict(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Distinct .pak names in one folder are legal (BG3 Mods / Palworld ~mods)."""
    library = tmp_path / "mod"
    mods_dir = tmp_path / "Paks" / "~mods"
    t1 = str((mods_dir / "A.pak").resolve())
    t2 = str((mods_dir / "B.pak").resolve())
    a, pk_a = _seed(library, db, external_id="11", title="A")
    b, pk_b = _seed(library, db, external_id="12", title="B")
    save_manifest(
        a,
        DeployManifest(
            mod_id=pk_a,
            deploy_time="t",
            deploy_type="palworld_pak",
            files=[ManifestFileEntry(source="A.pak", target=t1)],
        ),
    )
    save_manifest(
        b,
        DeployManifest(
            mod_id=pk_b,
            deploy_time="t",
            deploy_type="palworld_pak",
            files=[ManifestFileEntry(source="B.pak", target=t2)],
        ),
    )
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports[pk_a].status == CONFLICT_STATUS_NONE
    assert reports[pk_b].status == CONFLICT_STATUS_NONE
    assert not any(
        c.conflict_type == ConflictType.PAK_OVERLAP.value
        for c in reports[pk_a].conflicts
    )
    assert db.get_mod_status(pk_a).conflict_status == CONFLICT_STATUS_NONE


def test_disabled_skipped(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "x.pak").resolve())
    a, pk_a = _seed(library, db, external_id="21")
    b, pk_b = _seed(library, db, external_id="22")
    for folder, pk in ((a, pk_a), (b, pk_b)):
        save_manifest(
            folder,
            DeployManifest(
                mod_id=pk,
                deploy_time="t",
                deploy_type="folder_copy",
                files=[ManifestFileEntry(source="x", target=shared)],
            ),
        )
    db.disable_mod(pk_b)
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    # Only one enabled owner → no conflict
    assert pk_a in reports
    assert reports[pk_a].status != CONFLICT_STATUS_CONFLICT or not reports[pk_a].conflicts
    assert reports[pk_a].conflicts == [] or all(
        pk_b not in c.mods for c in reports[pk_a].conflicts
    )
    # Disabled mod cleared / not conflicting
    r22 = ConflictDetector(library, db=db).check_mod(pk_b, persist=True)
    assert r22.status != CONFLICT_STATUS_CONFLICT or not r22.conflicts
