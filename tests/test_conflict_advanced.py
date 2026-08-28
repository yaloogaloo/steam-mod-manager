"""Advanced conflict detection (FILE_OVERWRITE / PAK_OVERLAP / disabled skip)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_status import CONFLICT_STATUS_CONFLICT, CONFLICT_STATUS_NONE
from core.models import ModMetadata
from services.conflict import ConflictDetector, ConflictType
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    save_manifest,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "adv_conflict.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed(library: Path, mid: str) -> Path:
    folder = library / "G" / mid
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        f'{{"published_file_id":"{mid}","title":"M{mid}"}}',
        encoding="utf-8",
    )
    return folder


def test_same_target_file_overwrite(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "Paks" / "a.pak").resolve())
    a = _seed(library, "1")
    b = _seed(library, "2")
    for folder, mid in ((a, "1"), (b, "2")):
        save_manifest(
            folder,
            DeployManifest(
                mod_id=mid,
                deploy_time="t",
                deploy_type="folder_copy",
                files=[ManifestFileEntry(source="x", target=shared)],
            ),
        )
        db.upsert_mod(ModMetadata(published_file_id=mid, title=mid))
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports["1"].status == CONFLICT_STATUS_CONFLICT
    assert reports["1"].conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value


def test_same_dir_different_pak_is_not_conflict(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Distinct .pak names in one folder are legal (BG3 Mods / Palworld ~mods)."""
    library = tmp_path / "mod"
    mods_dir = tmp_path / "Paks" / "~mods"
    t1 = str((mods_dir / "A.pak").resolve())
    t2 = str((mods_dir / "B.pak").resolve())
    a = _seed(library, "11")
    b = _seed(library, "12")
    save_manifest(
        a,
        DeployManifest(
            mod_id="11",
            deploy_time="t",
            deploy_type="palworld_pak",
            files=[ManifestFileEntry(source="A.pak", target=t1)],
        ),
    )
    save_manifest(
        b,
        DeployManifest(
            mod_id="12",
            deploy_time="t",
            deploy_type="palworld_pak",
            files=[ManifestFileEntry(source="B.pak", target=t2)],
        ),
    )
    db.upsert_mod(ModMetadata(published_file_id="11", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="12", title="B"))
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports["11"].status == CONFLICT_STATUS_NONE
    assert reports["12"].status == CONFLICT_STATUS_NONE
    assert not any(
        c.conflict_type == ConflictType.PAK_OVERLAP.value
        for c in reports["11"].conflicts
    )
    assert db.get_mod_status(11).conflict_status == CONFLICT_STATUS_NONE


def test_disabled_skipped(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "x.pak").resolve())
    a = _seed(library, "21")
    b = _seed(library, "22")
    for folder, mid in ((a, "21"), (b, "22")):
        save_manifest(
            folder,
            DeployManifest(
                mod_id=mid,
                deploy_time="t",
                deploy_type="folder_copy",
                files=[ManifestFileEntry(source="x", target=shared)],
            ),
        )
        db.upsert_mod(ModMetadata(published_file_id=mid, title=mid))
    db.disable_mod(22)
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    # Only one enabled owner → no conflict
    assert "21" in reports
    assert reports["21"].status != CONFLICT_STATUS_CONFLICT or not reports["21"].conflicts
    assert reports["21"].conflicts == [] or all(
        "22" not in c.mods for c in reports["21"].conflicts
    )
    # Disabled mod cleared / not conflicting
    r22 = ConflictDetector(library, db=db).check_mod(22, persist=True)
    assert r22.status != CONFLICT_STATUS_CONFLICT or not r22.conflicts
