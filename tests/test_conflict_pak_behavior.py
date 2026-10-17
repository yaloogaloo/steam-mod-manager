"""Conflict detection: same-dir paks are legal; only identical targets conflict."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import RELATIONSHIP_CONFLICT, DatabaseManager
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
    manager = DatabaseManager(tmp_path / "pak_behavior.db")
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
    folder = library / "BaldursGate3" / external_id
    folder.mkdir(parents=True, exist_ok=True)
    created = create_steam_test_mod(
        db, external_id=external_id, title=title or external_id
    )
    pk = str(created.mod_id)
    prove_managed_folder(db, folder, handle=pk, title=title or external_id)
    return folder, pk


def _manifest(folder: Path, mid: str, target: str) -> None:
    save_manifest(
        folder,
        DeployManifest(
            mod_id=mid,
            deploy_time="t",
            deploy_type="pak_mod_path",
            files=[ManifestFileEntry(source=Path(target).name, target=target)],
        ),
    )


def test_case1_distinct_paks_same_mods_dir_no_conflict(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_dir = tmp_path / "BG3" / "Mods"
    t_a = str((mods_dir / "A.pak").resolve())
    t_b = str((mods_dir / "B.pak").resolve())
    a, pk_a = _seed(library, db, external_id="101", title="ModA")
    b, pk_b = _seed(library, db, external_id="102", title="ModB")
    _manifest(a, pk_a, t_a)
    _manifest(b, pk_b, t_b)

    det = ConflictDetector(library, db=db)
    reports = det.check_all_mods(persist=True)
    assert reports[pk_a].status == CONFLICT_STATUS_NONE
    assert reports[pk_b].status == CONFLICT_STATUS_NONE
    assert reports[pk_a].conflicts == []
    assert reports[pk_b].conflicts == []
    assert not any(
        c.conflict_type == ConflictType.PAK_OVERLAP.value
        for rep in reports.values()
        for c in rep.conflicts
    )
    assert db.get_mod_status(pk_a).conflict_status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(pk_b).conflict_status == CONFLICT_STATUS_NONE

    # preview_targets agrees with check_all_mods (no path conflict)
    preview = det.preview_targets(pk_a, [t_a])
    assert preview.status == CONFLICT_STATUS_NONE
    assert preview.conflicts == []


def test_case2_identical_pak_target_is_file_overwrite(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "BG3" / "Mods" / "Test.pak").resolve())
    a, pk_a = _seed(library, db, external_id="201", title="ModA")
    b, pk_b = _seed(library, db, external_id="202", title="ModB")
    _manifest(a, pk_a, shared)
    _manifest(b, pk_b, shared)

    det = ConflictDetector(library, db=db)
    reports = det.check_all_mods(persist=True)
    assert reports[pk_a].status == CONFLICT_STATUS_NONE
    assert reports[pk_b].status == CONFLICT_STATUS_NONE
    assert reports[pk_a].conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value
    assert set(reports[pk_a].conflicts[0].mods) == {pk_a, pk_b}
    assert db.get_mod_status(pk_a).conflict_status == CONFLICT_STATUS_NONE

    preview = det.preview_targets(pk_a, [shared])
    assert preview.status == CONFLICT_STATUS_NONE
    assert preview.conflicts
    assert preview.conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value
    assert pk_b in preview.conflicts[0].mods


def test_case3_user_relationship_conflict_still_visible(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_dir = tmp_path / "BG3" / "Mods"
    # Distinct targets — no FILE_OVERWRITE
    t_a = str((mods_dir / "RelA.pak").resolve())
    t_b = str((mods_dir / "RelB.pak").resolve())
    a, pk_a = _seed(library, db, external_id="301", title="Source")
    b, pk_b = _seed(library, db, external_id="302", title="DeclaredRival")
    _manifest(a, pk_a, t_a)
    _manifest(b, pk_b, t_b)
    db.add_mod_relationship(pk_a, pk_b, RELATIONSHIP_CONFLICT)

    # Relationship API still surfaces the declaration
    grouped = db.get_mod_relationships(pk_a)
    assert any(
        str(item.get("mod_id") or item.get("target_mod_id")) == pk_b
        for item in (grouped.get("conflicts") or [])
    )
    warns = db.check_relationship_deploy_warnings(pk_a)
    assert any(w.get("type") == "known_conflict" for w in warns)

    # Detector keeps RELATIONSHIP as a separate entry (not FILE_OVERWRITE)
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    rel_entries = [
        c
        for c in reports[pk_a].conflicts
        if c.conflict_type == ConflictType.RELATIONSHIP.value
    ]
    assert rel_entries
    assert pk_b in rel_entries[0].mods
    assert not any(
        c.conflict_type == ConflictType.FILE_OVERWRITE.value
        for c in reports[pk_a].conflicts
    )
    assert reports[pk_a].status == CONFLICT_STATUS_CONFLICT


def test_case4_disabled_mod_excluded_from_path_conflict(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "BG3" / "Mods" / "Shared.pak").resolve())
    a, pk_a = _seed(library, db, external_id="401", title="Enabled")
    b, pk_b = _seed(library, db, external_id="402", title="Disabled")
    _manifest(a, pk_a, shared)
    _manifest(b, pk_b, shared)
    db.disable_mod(pk_b)

    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports[pk_a].status == CONFLICT_STATUS_NONE
    assert reports[pk_a].conflicts == []
    assert all(pk_b not in c.mods for c in reports[pk_a].conflicts)

    preview = ConflictDetector(library, db=db).preview_targets(pk_a, [shared])
    assert preview.conflicts == []
    assert preview.status == CONFLICT_STATUS_NONE
