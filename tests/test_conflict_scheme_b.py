"""P0-3 Scheme B: path overlap is diagnostic, not an automatic conflict relationship."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import RELATIONSHIP_CONFLICT, DatabaseManager
from core.mod_status import CONFLICT_STATUS_CONFLICT, CONFLICT_STATUS_NONE
from services.conflict import ConflictClass, ConflictDetector, ConflictType
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    save_manifest,
)
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
from services.user_annotation import clear_conflict_annotation, set_conflict_annotation
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


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


def _seed_folder(library: Path, mid: str, *, title: str = "") -> Path:
    """Filesystem-only seed (Anno forced-PK forensic path uses this + SQL INSERT)."""
    folder = library / "Game" / (title or mid)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _seed(
    library: Path,
    db: DatabaseManager,
    *,
    external_id: str,
    title: str = "",
) -> tuple[Path, str]:
    folder = _seed_folder(library, external_id, title=title)
    created = create_steam_test_mod(
        db, external_id=external_id, title=title or external_id
    )
    pk = str(created.mod_id)
    prove_managed_folder(db, folder, handle=pk, title=title or external_id)
    return folder, pk


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
    a, pk_a = _seed(library, db, external_id="901", title="A")
    b, pk_b = _seed(library, db, external_id="902", title="B")
    _write_targets(a, pk_a, [shared])
    _write_targets(b, pk_b, [shared])
    det = ConflictDetector(library, db=db)
    first = det.check_all_mods(persist=False)
    second = det.check_all_mods(persist=False)
    assert first[pk_a].conflicts[0].as_dict() == second[pk_a].conflicts[0].as_dict()
    assert first[pk_a].conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value
    assert first[pk_a].traces[0].rule_id == "FILE_OVERWRITE.identical_resolved_target"
    assert first[pk_a].traces[0].overlap_count == 1


def test_b_path_overlap_is_diagnostic_only(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "shared.pak").resolve())
    a, pk_a = _seed(library, db, external_id="911", title="A")
    b, pk_b = _seed(library, db, external_id="912", title="B")
    _write_targets(a, pk_a, [shared])
    _write_targets(b, pk_b, [shared])
    before_rel = _rel_count(db)
    before_count = db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0]  # noqa: SLF001
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports[pk_a].conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value
    assert reports[pk_a].status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(pk_a).conflict_status == CONFLICT_STATUS_NONE
    assert _rel_count(db) == before_rel == 0
    assert db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0] == before_count  # noqa: SLF001


def test_c_resolve_survives_persist_rescan(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "foo.dll").resolve())
    pks: list[str] = []
    for mid in ("921", "922"):
        folder, pk = _seed(library, db, external_id=mid, title=f"M{mid}")
        _write_targets(folder, pk, [shared])
        pks.append(pk)
    pk_a = pks[0]
    det = ConflictDetector(library, db=db)
    det.check_all_mods(persist=True)
    assert db.get_mod_status(pk_a).conflict_status == CONFLICT_STATUS_NONE
    set_conflict_annotation(pk_a, note="user", db=db)
    det.check_all_mods(persist=True)
    assert db.get_mod_status(pk_a).conflict_status == CONFLICT_STATUS_CONFLICT
    clear_conflict_annotation(pk_a, db=db)
    det.check_all_mods(persist=True)
    assert db.get_mod_status(pk_a).conflict_status == CONFLICT_STATUS_NONE
    report = det.check_all_mods(persist=False)[pk_a]
    assert report.conflicts
    assert report.conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value


def test_d_persist_false_is_read_only(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "x.pak").resolve())
    a, pk_a = _seed(library, db, external_id="931", title="A")
    b, pk_b = _seed(library, db, external_id="932", title="B")
    _write_targets(a, pk_a, [shared])
    _write_targets(b, pk_b, [shared])
    set_conflict_annotation(pk_a, note="keep", db=db)
    before = _mods_snapshot(db)
    before_rel = _rel_count(db)
    ConflictDetector(library, db=db).check_all_mods(persist=False)
    assert _mods_snapshot(db) == before
    assert _rel_count(db) == before_rel
    from services.file_ops import INFO_DIR_NAME

    info = a / INFO_DIR_NAME / "conflict_trace.json"
    assert not info.is_file()


def test_e_detection_does_not_create_mods(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "z.pak").resolve())
    a, pk_a = _seed(library, db, external_id="941", title="A")
    b, pk_b = _seed(library, db, external_id="942", title="B")
    _write_targets(a, pk_a, [shared])
    _write_targets(b, pk_b, [shared])
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
    """Forced high-range PKs (Anno forensic fixture) — not create_steam_test_mod."""
    library = tmp_path / "mod"
    stamps = tmp_path / "stamps"
    stamps.mkdir()
    targets = [str((stamps / f"t{i:03d}.stamp").resolve()) for i in range(141)]
    a = _seed_folder(library, ANNO_0360, title="全产业模板")
    b = _seed_folder(library, ANNO_0362, title="布局模板")
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
    a, pk_a = _seed(library, db, external_id="951", title="A")
    b, pk_b = _seed(library, db, external_id="952", title="B")
    _write_targets(a, pk_a, [t_a])
    _write_targets(b, pk_b, [t_b])
    db.add_mod_relationship(pk_a, pk_b, RELATIONSHIP_CONFLICT)
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    # Scheme B: relationship is a diagnostic; detector must not write user conflict_status.
    assert any(
        c.conflict_type == ConflictType.RELATIONSHIP.value
        for c in reports[pk_a].conflicts
    )
    assert db.get_mod_status(pk_a).conflict_status == CONFLICT_STATUS_NONE
    assert not any(
        c.conflict_type == ConflictType.FILE_OVERWRITE.value
        for c in reports[pk_a].conflicts
    )
