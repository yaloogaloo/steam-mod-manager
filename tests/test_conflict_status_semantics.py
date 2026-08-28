"""FILE_OVERWRITE status is ``conflict`` for both preview and post-deploy scan."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_status import CONFLICT_STATUS_CONFLICT, CONFLICT_STATUS_NONE
from core.models import ModMetadata
from services.conflict import ConflictDetector, ConflictType
from services.deploy import ModDeployer
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    save_manifest,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "status_semantics.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed(library: Path, mid: str) -> Path:
    folder = library / "BG3" / mid
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        f'{{"published_file_id":"{mid}","title":"M{mid}"}}',
        encoding="utf-8",
    )
    return folder


def _write(folder: Path, mid: str, target: str) -> None:
    save_manifest(
        folder,
        DeployManifest(
            mod_id=mid,
            deploy_time="t",
            deploy_type="folder_copy",
            files=[ManifestFileEntry(source=Path(target).name, target=target)],
        ),
    )


def test_case1_identical_dll_persists_conflict(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "BG3" / "bin" / "foo.dll").resolve())
    a = _seed(library, "801")
    b = _seed(library, "802")
    _write(a, "801", shared)
    _write(b, "802", shared)
    db.upsert_mod(ModMetadata(published_file_id="801", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="802", title="B"))

    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports["801"].status == CONFLICT_STATUS_CONFLICT
    assert reports["802"].status == CONFLICT_STATUS_CONFLICT
    assert reports["801"].conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value
    assert db.get_mod_status(801).conflict_status == "conflict"
    assert db.get_mod_status(802).conflict_status == "conflict"


def test_case2_preview_status_is_conflict_not_warning(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "BG3" / "Mods" / "a.pak").resolve())
    a = _seed(library, "811")
    _write(a, "811", shared)
    db.upsert_mod(ModMetadata(published_file_id="811", title="A"))

    det = ConflictDetector(library, db=db)
    preview = det.preview_targets("812", [shared])
    assert preview.status == CONFLICT_STATUS_CONFLICT
    assert preview.status != "warning"
    assert preview.conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value

    payload = ModDeployer(library_root=library, db=db).check_conflict_preview(
        "812", [shared]
    )
    assert payload is not None
    assert payload["conflict"] is True
    assert payload["status"] == "conflict"
    assert payload["conflicts"][0]["type"] == "FILE_OVERWRITE"


def test_case3_distinct_targets_no_conflict(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods = tmp_path / "BG3" / "Mods"
    t_a = str((mods / "A.pak").resolve())
    t_b = str((mods / "B.pak").resolve())
    a = _seed(library, "821")
    b = _seed(library, "822")
    _write(a, "821", t_a)
    _write(b, "822", t_b)
    db.upsert_mod(ModMetadata(published_file_id="821", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="822", title="B"))

    det = ConflictDetector(library, db=db)
    reports = det.check_all_mods(persist=True)
    assert reports["821"].status == CONFLICT_STATUS_NONE
    assert reports["822"].status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(821).conflict_status == "none"

    preview = det.preview_targets("822", [t_b])
    assert preview.status == CONFLICT_STATUS_NONE
    assert preview.conflicts == []


def test_case4_same_dir_distinct_paks_no_pak_overlap(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods = tmp_path / "Mods"
    t_a = str((mods / "A.pak").resolve())
    t_b = str((mods / "B.pak").resolve())
    a = _seed(library, "831")
    b = _seed(library, "832")
    _write(a, "831", t_a)
    _write(b, "832", t_b)
    db.upsert_mod(ModMetadata(published_file_id="831", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="832", title="B"))

    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert not any(
        c.conflict_type == ConflictType.PAK_OVERLAP.value
        for r in reports.values()
        for c in r.conflicts
    )
    assert reports["831"].status == CONFLICT_STATUS_NONE
    assert reports["832"].status == CONFLICT_STATUS_NONE
