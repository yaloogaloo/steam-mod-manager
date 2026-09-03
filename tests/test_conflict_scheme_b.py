"""P0-3 Scheme B: path overlap is diagnostic, not an automatic conflict relationship."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import RELATIONSHIP_CONFLICT, DatabaseManager
from core.mod_status import CONFLICT_STATUS_CONFLICT, CONFLICT_STATUS_NONE
from core.models import ModMetadata
from services.conflict import ConflictClass, ConflictDetector, ConflictType
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    save_manifest,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_invariants import (
    CONFLICT_DETECTOR_AUTO_CREATES_RELATIONSHIP,
    CONFLICT_DETECTOR_CREATES_IDENTITY,
    CONFLICT_DETECTOR_CREATES_MOD,
    CONFLICT_DETECTOR_MUTATES_WORKSPACE_ID,
    CONFLICT_SCAN_OVERWRITES_USER_RESOLUTION,
    PATH_OVERLAP_AUTO_MEANS_CONFLICT,
    scan_conflict_scheme_b,
    scan_id_architecture_source,
    scan_reconcile_identity_lifecycle,
)


ANNO_0360 = "9000000000000360"
ANNO_0362 = "9000000000000362"
WS_0360 = "17863520439005318"
WS_0362 = "17863521013284165"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "scheme_b.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed(library: Path, mid: str, *, title: str = "") -> Path:
    folder = library / "Game" / (title or mid)
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        f'{{"published_file_id":"{mid}","title":"{title or mid}","workspace_id":""}}',
        encoding="utf-8",
    )
    return folder


def _write_targets(folder: Path, mid: str, targets: list[str]) -> None:
    save_manifest(
        folder,
        DeployManifest(
            mod_id=mid,
            deploy_time="t",
            deploy_type="folder_copy",
            files=[
                ManifestFileEntry(source=Path(t).name, target=t) for t in targets
            ],
        ),
    )


def _insert_other(
    db: DatabaseManager, mid: str, title: str, workspace_id: str
) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            """
            INSERT INTO mods (
                mod_id, app_id, title, preview_url, description,
                platform, source_url, external_id, workspace_id, updated_at
            ) VALUES (?, 0, ?, '', '', 'other', '', ?, ?, ?)
            """,
            (int(mid), title, f"local/{title}", workspace_id, "2020-01-01T00:00:00+00:00"),
        )
        db._conn.commit()


def _mods_snapshot(db: DatabaseManager) -> list[tuple]:
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            "SELECT mod_id, platform, app_id, workspace_id, external_id, "
            "source_url, conflict_status, conflict_note, last_check_time, "
            "updated_at FROM mods ORDER BY mod_id"
        ).fetchall()
    return [tuple(r) for r in rows]


def _rel_count(db: DatabaseManager) -> int:
    with db._lock:  # noqa: SLF001
        return int(
            db._conn.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM mod_relationships"
            ).fetchone()[0]
        )


def test_a_same_input_same_diagnostic(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "a.dll").resolve())
    a = _seed(library, "901")
    b = _seed(library, "902")
    _write_targets(a, "901", [shared])
    _write_targets(b, "902", [shared])
    db.upsert_mod(ModMetadata(published_file_id="901", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="902", title="B"))
    det = ConflictDetector(library, db=db)
    first = det.check_all_mods(persist=False)
    second = det.check_all_mods(persist=False)
    assert first["901"].conflicts[0].as_dict() == second["901"].conflicts[0].as_dict()
    assert first["901"].conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value
    assert first["901"].traces[0].rule_id == "FILE_OVERWRITE.identical_resolved_target"
    assert first["901"].traces[0].overlap_count == 1


def test_b_path_overlap_is_diagnostic_only(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "shared.pak").resolve())
    a = _seed(library, "911")
    b = _seed(library, "912")
    _write_targets(a, "911", [shared])
    _write_targets(b, "912", [shared])
    db.upsert_mod(ModMetadata(published_file_id="911", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="912", title="B"))
    before_rel = _rel_count(db)
    before_count = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]  # noqa: SLF001
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports["911"].conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value
    assert reports["911"].status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(911).conflict_status == CONFLICT_STATUS_NONE
    assert _rel_count(db) == before_rel == 0
    assert db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == before_count  # noqa: SLF001


def test_c_resolve_survives_persist_rescan(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "foo.dll").resolve())
    for mid in ("921", "922"):
        folder = _seed(library, mid)
        _write_targets(folder, mid, [shared])
        db.upsert_mod(ModMetadata(published_file_id=mid, title=f"M{mid}"))
    det = ConflictDetector(library, db=db)
    det.check_all_mods(persist=True)
    assert db.get_mod_status(921).conflict_status == CONFLICT_STATUS_NONE
    db.update_mod_status(921, conflict_status=CONFLICT_STATUS_CONFLICT, conflict_note="user")
    det.check_all_mods(persist=True)
    assert db.get_mod_status(921).conflict_status == CONFLICT_STATUS_CONFLICT
    db.update_mod_status(921, conflict_status=CONFLICT_STATUS_NONE, conflict_note="")
    det.check_all_mods(persist=True)
    assert db.get_mod_status(921).conflict_status == CONFLICT_STATUS_NONE
    report = det.check_all_mods(persist=False)["921"]
    assert report.conflicts
    assert report.conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value


def test_d_persist_false_is_read_only(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "x.pak").resolve())
    a = _seed(library, "931")
    b = _seed(library, "932")
    _write_targets(a, "931", [shared])
    _write_targets(b, "932", [shared])
    db.upsert_mod(ModMetadata(published_file_id="931", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="932", title="B"))
    db.update_mod_status(931, conflict_status=CONFLICT_STATUS_CONFLICT, conflict_note="keep")
    before = _mods_snapshot(db)
    before_rel = _rel_count(db)
    ConflictDetector(library, db=db).check_all_mods(persist=False)
    assert _mods_snapshot(db) == before
    assert _rel_count(db) == before_rel
    info = a / INFO_DIR_NAME / "conflict_trace.json"
    assert not info.is_file()


def test_e_detection_does_not_create_mods(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "z.pak").resolve())
    a = _seed(library, "941")
    b = _seed(library, "942")
    _write_targets(a, "941", [shared])
    _write_targets(b, "942", [shared])
    db.upsert_mod(ModMetadata(published_file_id="941", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="942", title="B"))
    ids_before = {
        str(r["mod_id"]): str(r["workspace_id"] or "")
        for r in db._conn.execute("SELECT mod_id, workspace_id FROM mods")  # noqa: SLF001
    }
    ConflictDetector(library, db=db).check_all_mods(persist=True)
    ids_after = {
        str(r["mod_id"]): str(r["workspace_id"] or "")
        for r in db._conn.execute("SELECT mod_id, workspace_id FROM mods")  # noqa: SLF001
    }
    assert ids_before == ids_after


def test_f_identity_invariants_still_clean() -> None:
    assert scan_reconcile_identity_lifecycle() == []
    assert scan_conflict_scheme_b() == []
    src = scan_id_architecture_source()
    codes = {f.violation_code for f in src}
    assert CONFLICT_DETECTOR_CREATES_MOD not in codes
    assert CONFLICT_DETECTOR_CREATES_IDENTITY not in codes
    assert CONFLICT_DETECTOR_MUTATES_WORKSPACE_ID not in codes
    assert CONFLICT_DETECTOR_AUTO_CREATES_RELATIONSHIP not in codes
    assert PATH_OVERLAP_AUTO_MEANS_CONFLICT not in codes
    assert CONFLICT_SCAN_OVERWRITES_USER_RESOLUTION not in codes


def test_g_anno_141_overlap_is_diagnostic_not_relationship(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    stamps = tmp_path / "stamps"
    stamps.mkdir()
    targets = [str((stamps / f"t{i:03d}.stamp").resolve()) for i in range(141)]
    a = _seed(library, ANNO_0360, title="全产业模板")
    b = _seed(library, ANNO_0362, title="布局模板")
    _write_targets(a, ANNO_0360, targets)
    _write_targets(b, ANNO_0362, targets)
    _insert_other(db, ANNO_0360, "全产业模板", WS_0360)
    _insert_other(db, ANNO_0362, "布局模板", WS_0362)
    before_count = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]  # noqa: SLF001
    before_rel = _rel_count(db)
    ws_before = {
        ANNO_0360: WS_0360,
        ANNO_0362: WS_0362,
    }
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    ow_a = [
        c
        for c in reports[ANNO_0360].conflicts
        if c.conflict_type == ConflictType.FILE_OVERWRITE.value
    ]
    ow_b = [
        c
        for c in reports[ANNO_0362].conflicts
        if c.conflict_type == ConflictType.FILE_OVERWRITE.value
    ]
    assert len(ow_a) == 141
    assert len(ow_b) == 141
    assert reports[ANNO_0360].status == CONFLICT_STATUS_NONE
    assert reports[ANNO_0362].status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(ANNO_0360).conflict_status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(ANNO_0362).conflict_status == CONFLICT_STATUS_NONE
    assert _rel_count(db) == before_rel == 0
    assert db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == before_count  # noqa: SLF001
    for mid, ws in ws_before.items():
        info = db.get_mod_display_info(mid)
        assert info is not None
        assert str(info.workspace_id) == ws
        assert str(info.mod_id) == mid
        assert str(info.platform) == "other"
    traces = reports[ANNO_0362].traces
    assert traces
    assert traces[0].conflict_type == ConflictClass.FILE_OVERWRITE.value
    assert traces[0].overlap_count == 141
    assert traces[0].workspace_a in (WS_0360, WS_0362)
    assert traces[0].workspace_b in (WS_0360, WS_0362)


def test_relationship_still_persists_as_conflict(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    t_a = str((tmp_path / "A.pak").resolve())
    t_b = str((tmp_path / "B.pak").resolve())
    a = _seed(library, "951")
    b = _seed(library, "952")
    _write_targets(a, "951", [t_a])
    _write_targets(b, "952", [t_b])
    db.upsert_mod(ModMetadata(published_file_id="951", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="952", title="B"))
    db.add_mod_relationship(951, 952, RELATIONSHIP_CONFLICT)
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports["951"].status == CONFLICT_STATUS_CONFLICT
    assert db.get_mod_status(951).conflict_status == CONFLICT_STATUS_CONFLICT
    assert any(
        c.conflict_type == ConflictType.RELATIONSHIP.value
        for c in reports["951"].conflicts
    )
    assert not any(
        c.conflict_type == ConflictType.FILE_OVERWRITE.value
        for c in reports["951"].conflicts
    )
