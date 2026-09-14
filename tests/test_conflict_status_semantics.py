"""FILE_OVERWRITE is a diagnostic; persist must not write conflict_status."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_status import CONFLICT_STATUS_NONE
from services.conflict import ConflictDetector, ConflictType
from services.deploy import ModDeployer
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    save_manifest,
)
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "status_semantics.db")
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
    folder = library / "BG3" / external_id
    folder.mkdir(parents=True, exist_ok=True)
    created = create_steam_test_mod(
        db, external_id=external_id, title=title or f"M{external_id}"
    )
    pk = str(created.mod_id)
    prove_managed_folder(db, folder, handle=pk, title=title or f"M{external_id}")
    return folder, pk


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
    a, pk_a = _seed(library, db, external_id="801", title="A")
    b, pk_b = _seed(library, db, external_id="802", title="B")
    _write(a, pk_a, shared)
    _write(b, pk_b, shared)

    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports[pk_a].status == CONFLICT_STATUS_NONE
    assert reports[pk_b].status == CONFLICT_STATUS_NONE
    assert reports[pk_a].conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value
    assert db.get_mod_status(pk_a).conflict_status == "none"
    assert db.get_mod_status(pk_b).conflict_status == "none"


def test_case2_preview_reports_overwrite_not_relationship(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "BG3" / "Mods" / "a.pak").resolve())
    a, pk_a = _seed(library, db, external_id="811", title="A")
    _write(a, pk_a, shared)
    # Preview a different (non-existent) candidate claiming the same target.
    candidate_pk = "812"

    det = ConflictDetector(library, db=db)
    preview = det.preview_targets(candidate_pk, [shared])
    assert preview.status == CONFLICT_STATUS_NONE
    assert preview.conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value

    payload = ModDeployer(library_root=library, db=db).check_conflict_preview(
        candidate_pk, [shared]
    )
    assert payload is not None
    assert payload["overwrite"] is True
    assert payload["conflict"] is False
    assert payload["status"] == "none"
    assert payload["conflicts"][0]["type"] == "FILE_OVERWRITE"


def test_case3_distinct_targets_no_conflict(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods = tmp_path / "BG3" / "Mods"
    t_a = str((mods / "A.pak").resolve())
    t_b = str((mods / "B.pak").resolve())
    a, pk_a = _seed(library, db, external_id="821", title="A")
    b, pk_b = _seed(library, db, external_id="822", title="B")
    _write(a, pk_a, t_a)
    _write(b, pk_b, t_b)

    det = ConflictDetector(library, db=db)
    reports = det.check_all_mods(persist=True)
    assert reports[pk_a].status == CONFLICT_STATUS_NONE
    assert reports[pk_b].status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(pk_a).conflict_status == "none"

    preview = det.preview_targets(pk_b, [t_b])
    assert preview.status == CONFLICT_STATUS_NONE
    assert preview.conflicts == []


def test_case4_same_dir_distinct_paks_no_pak_overlap(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods = tmp_path / "Mods"
    t_a = str((mods / "A.pak").resolve())
    t_b = str((mods / "B.pak").resolve())
    a, pk_a = _seed(library, db, external_id="831", title="A")
    b, pk_b = _seed(library, db, external_id="832", title="B")
    _write(a, pk_a, t_a)
    _write(b, pk_b, t_b)

    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert not any(
        c.conflict_type == ConflictType.PAK_OVERLAP.value
        for r in reports.values()
        for c in r.conflicts
    )
    assert reports[pk_a].status == CONFLICT_STATUS_NONE
    assert reports[pk_b].status == CONFLICT_STATUS_NONE
