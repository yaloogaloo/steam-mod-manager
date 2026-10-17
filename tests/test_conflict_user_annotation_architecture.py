"""Architecture: system modules must not write user conflict annotation.

ARCHITECTURE RULE
-----------------
Conflict is user annotation (equivalent to invalid / abandoned). Import,
Deploy, Reconcile, Refresh, Startup, Archive, and ConflictDetector must not
produce ``mods.conflict_status=conflict``. Detector may emit diagnostics only.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.db_manager import RELATIONSHIP_CONFLICT, DatabaseManager
from core.mod_status import CONFLICT_STATUS_CONFLICT, CONFLICT_STATUS_NONE
from services.conflict import ConflictDetector, ConflictType
from services.deploy import _schedule_post_deploy_conflict_scan
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "arch_conflict.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(
    library: Path,
    db: DatabaseManager,
    *,
    external_id: str,
    title: str = "",
) -> tuple[Path, str]:
    folder = library / "Game" / external_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "mod.pak").write_bytes(b"x")
    created = create_steam_test_mod(
        db, external_id=external_id, title=title or f"M{external_id}"
    )
    pk = str(created.mod_id)
    prove_managed_folder(db, folder, handle=pk, title=title or f"M{external_id}")
    return folder, pk


def _write_targets(folder: Path, mid: str, targets: list[str]) -> None:
    from services.deploy_rules.manifest import (
        DeployManifest,
        ManifestFileEntry,
        save_manifest,
    )

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


def test_detector_persist_never_writes_conflict_status(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "shared.pak").resolve())
    (tmp_path / "game").mkdir()
    folders: dict[str, str] = {}
    for mid in ("101", "102"):
        folder, pk = _seed(library, db, external_id=mid)
        _write_targets(folder, pk, [shared])
        folders[mid] = pk
    pk_a, pk_b = folders["101"], folders["102"]
    db.add_mod_relationship(pk_a, pk_b, RELATIONSHIP_CONFLICT)

    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports[pk_a].conflicts
    assert any(
        c.conflict_type == ConflictType.RELATIONSHIP.value
        for c in reports[pk_a].conflicts
    )
    assert db.get_mod_status(pk_a).conflict_status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(pk_b).conflict_status == CONFLICT_STATUS_NONE


def test_post_deploy_scan_is_noop_for_conflict_status(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "a.pak").resolve())
    (tmp_path / "game").mkdir()
    pks: list[str] = []
    for mid in ("201", "202"):
        folder, pk = _seed(library, db, external_id=mid)
        _write_targets(folder, pk, [shared])
        pks.append(pk)
    db.add_mod_relationship(pks[0], pks[1], RELATIONSHIP_CONFLICT)
    _schedule_post_deploy_conflict_scan(library, db=db)
    assert db.get_mod_status(pks[0]).conflict_status == CONFLICT_STATUS_NONE


def test_user_mark_survives_detector_and_is_only_user_writer(
    tmp_path: Path, db: DatabaseManager
) -> None:
    from services.user_annotation import (
        clear_conflict_annotation,
        set_conflict_annotation,
    )

    library = tmp_path / "mod"
    folder, pk = _seed(library, db, external_id="301", title="U")
    _write_targets(folder, pk, [str((tmp_path / "t.pak").resolve())])
    set_conflict_annotation(pk, note="user", db=db)
    ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert db.get_mod_status(pk).conflict_status == CONFLICT_STATUS_CONFLICT
    clear_conflict_annotation(pk, db=db)
    ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert db.get_mod_status(pk).conflict_status == CONFLICT_STATUS_NONE


def test_migration_clears_polluted_conflict_status(tmp_path: Path) -> None:
    from services.user_annotation import set_conflict_annotation

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "migrate_conflict.db")
    created = create_steam_test_mod(db, external_id="401", title="Polluted")
    pk = str(created.mod_id)
    with db._lock:  # noqa: SLF001
        # Simulate upgrade: drop one-shot flag, plant polluted row, re-run.
        db._conn.execute(  # noqa: SLF001
            "DELETE FROM schema_flags WHERE flag = ?",
            ("cleared_system_conflict_v1",),
        )
        db._conn.execute(  # noqa: SLF001
            "UPDATE mods SET conflict_status = ?, conflict_note = ? WHERE mod_id = ?",
            ("conflict", "system inferred", int(pk)),
        )
        db._clear_system_inferred_conflict_pollution()  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    assert db.get_mod_status(pk).conflict_status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(pk).conflict_note == ""
    # Second run is a no-op (flag set) — user marks must survive.
    set_conflict_annotation(pk, note="user", db=db)
    with db._lock:  # noqa: SLF001
        db._clear_system_inferred_conflict_pollution()  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    assert db.get_mod_status(pk).conflict_status == CONFLICT_STATUS_CONFLICT
    DatabaseManager.reset_instance()


def test_conflict_detector_source_has_no_conflict_status_assignment() -> None:
    """Static: ConflictDetector module must not assign conflict_status= in persist."""
    import services.conflict as conflict_mod

    src = inspect.getsource(conflict_mod)
    tree = ast.parse(src)
    banned = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "conflict_status":
                    banned += 1
    assert banned == 0, "ConflictDetector must not call update with conflict_status="


def test_library_cache_does_not_conflate_conflict_into_identity(
    tmp_path: Path, db: DatabaseManager
) -> None:
    from services.library_status import CONTENT_HEALTHY, CONTENT_IDENTITY_CONFLICT
    from services.mod_library_cache import get_library_cache
    from services.user_annotation import set_conflict_annotation

    library = tmp_path / "mod"
    folder, pk = _seed(library, db, external_id="501", title="Card")
    set_conflict_annotation(pk, note="u", db=db)
    cache = get_library_cache()
    snap = cache.load_snapshot(library, force=True)
    cards = [c for c in snap.cards if str(c.id) == pk]
    assert cards
    assert cards[0].conflict is True
    assert cards[0].conflict_status == CONFLICT_STATUS_CONFLICT
    assert cards[0].content_status != CONTENT_IDENTITY_CONFLICT
    assert cards[0].content_status == CONTENT_HEALTHY or not cards[0].missing_content
