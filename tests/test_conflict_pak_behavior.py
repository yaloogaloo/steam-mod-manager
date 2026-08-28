"""Conflict detection: same-dir paks are legal; only identical targets conflict."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import RELATIONSHIP_CONFLICT, DatabaseManager
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
    manager = DatabaseManager(tmp_path / "pak_behavior.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed(library: Path, mid: str, *, title: str = "") -> Path:
    folder = library / "BaldursGate3" / mid
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        f'{{"published_file_id":"{mid}","title":"{title or mid}"}}',
        encoding="utf-8",
    )
    return folder


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
    a = _seed(library, "101", title="ModA")
    b = _seed(library, "102", title="ModB")
    _manifest(a, "101", t_a)
    _manifest(b, "102", t_b)
    db.upsert_mod(ModMetadata(published_file_id="101", title="ModA"))
    db.upsert_mod(ModMetadata(published_file_id="102", title="ModB"))

    det = ConflictDetector(library, db=db)
    reports = det.check_all_mods(persist=True)
    assert reports["101"].status == CONFLICT_STATUS_NONE
    assert reports["102"].status == CONFLICT_STATUS_NONE
    assert reports["101"].conflicts == []
    assert reports["102"].conflicts == []
    assert not any(
        c.conflict_type == ConflictType.PAK_OVERLAP.value
        for rep in reports.values()
        for c in rep.conflicts
    )
    assert db.get_mod_status(101).conflict_status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(102).conflict_status == CONFLICT_STATUS_NONE

    # preview_targets agrees with check_all_mods (no path conflict)
    preview = det.preview_targets("101", [t_a])
    assert preview.status == CONFLICT_STATUS_NONE
    assert preview.conflicts == []


def test_case2_identical_pak_target_is_file_overwrite(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "BG3" / "Mods" / "Test.pak").resolve())
    a = _seed(library, "201", title="ModA")
    b = _seed(library, "202", title="ModB")
    _manifest(a, "201", shared)
    _manifest(b, "202", shared)
    db.upsert_mod(ModMetadata(published_file_id="201", title="ModA"))
    db.upsert_mod(ModMetadata(published_file_id="202", title="ModB"))

    det = ConflictDetector(library, db=db)
    reports = det.check_all_mods(persist=True)
    assert reports["201"].status == CONFLICT_STATUS_CONFLICT
    assert reports["202"].status == CONFLICT_STATUS_CONFLICT
    assert reports["201"].conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value
    assert set(reports["201"].conflicts[0].mods) == {"201", "202"}
    assert db.get_mod_status(201).conflict_status == CONFLICT_STATUS_CONFLICT

    preview = det.preview_targets("201", [shared])
    assert preview.status == CONFLICT_STATUS_CONFLICT
    assert preview.conflicts
    assert preview.conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value
    assert "202" in preview.conflicts[0].mods


def test_case3_user_relationship_conflict_still_visible(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_dir = tmp_path / "BG3" / "Mods"
    # Distinct targets — no FILE_OVERWRITE
    t_a = str((mods_dir / "RelA.pak").resolve())
    t_b = str((mods_dir / "RelB.pak").resolve())
    a = _seed(library, "301", title="Source")
    b = _seed(library, "302", title="DeclaredRival")
    _manifest(a, "301", t_a)
    _manifest(b, "302", t_b)
    db.upsert_mod(ModMetadata(published_file_id="301", title="Source"))
    db.upsert_mod(ModMetadata(published_file_id="302", title="DeclaredRival"))
    db.add_mod_relationship(301, 302, RELATIONSHIP_CONFLICT)

    # Relationship API still surfaces the declaration
    grouped = db.get_mod_relationships("301")
    assert any(
        str(item.get("mod_id") or item.get("target_mod_id")) == "302"
        for item in (grouped.get("conflicts") or [])
    )
    warns = db.check_relationship_deploy_warnings(301)
    assert any(w.get("type") == "known_conflict" for w in warns)

    # Detector keeps RELATIONSHIP as a separate entry (not FILE_OVERWRITE)
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    rel_entries = [
        c
        for c in reports["301"].conflicts
        if c.conflict_type == ConflictType.RELATIONSHIP.value
    ]
    assert rel_entries
    assert "302" in rel_entries[0].mods
    assert not any(
        c.conflict_type == ConflictType.FILE_OVERWRITE.value
        for c in reports["301"].conflicts
    )
    assert reports["301"].status == CONFLICT_STATUS_CONFLICT


def test_case4_disabled_mod_excluded_from_path_conflict(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "BG3" / "Mods" / "Shared.pak").resolve())
    a = _seed(library, "401", title="Enabled")
    b = _seed(library, "402", title="Disabled")
    _manifest(a, "401", shared)
    _manifest(b, "402", shared)
    db.upsert_mod(ModMetadata(published_file_id="401", title="Enabled"))
    db.upsert_mod(ModMetadata(published_file_id="402", title="Disabled"))
    db.disable_mod(402)

    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports["401"].status == CONFLICT_STATUS_NONE
    assert reports["401"].conflicts == []
    assert all("402" not in c.mods for c in reports["401"].conflicts)

    preview = ConflictDetector(library, db=db).preview_targets("401", [shared])
    assert preview.conflicts == []
    assert preview.status == CONFLICT_STATUS_NONE
