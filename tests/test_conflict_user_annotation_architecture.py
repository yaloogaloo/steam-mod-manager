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
from core.models import ModMetadata
from services.conflict import ConflictDetector, ConflictType
from services.deploy import _schedule_post_deploy_conflict_scan
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from tests.helpers.identity import create_steam_test_mod


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "arch_conflict.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(library: Path, mid: str) -> Path:
    folder = library / "Game" / mid
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        f'{{"published_file_id":"{mid}","title":"M{mid}"}}',
        encoding="utf-8",
    )
    (folder / "mod.pak").write_bytes(b"x")
    return folder


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
    for mid in ("101", "102"):
        folder = _seed(library, mid)
        _write_targets(folder, mid, [shared])
        create_steam_test_mod(db, external_id=mid, title=f"M{mid}")
    db.add_mod_relationship(101, 102, RELATIONSHIP_CONFLICT)

    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports["101"].conflicts
    assert any(
        c.conflict_type == ConflictType.RELATIONSHIP.value
        for c in reports["101"].conflicts
    )
    assert db.get_mod_status(101).conflict_status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(102).conflict_status == CONFLICT_STATUS_NONE


def test_post_deploy_scan_is_noop_for_conflict_status(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    shared = str((tmp_path / "game" / "a.pak").resolve())
    (tmp_path / "game").mkdir()
    for mid in ("201", "202"):
        folder = _seed(library, mid)
        _write_targets(folder, mid, [shared])
        create_steam_test_mod(db, external_id=mid, title=f"M{mid}")
    db.add_mod_relationship(201, 202, RELATIONSHIP_CONFLICT)
    _schedule_post_deploy_conflict_scan(library, db=db)
    assert db.get_mod_status(201).conflict_status == CONFLICT_STATUS_NONE


def test_user_mark_survives_detector_and_is_only_user_writer(
    tmp_path: Path, db: DatabaseManager
) -> None:
    from services.user_annotation import (
        clear_conflict_annotation,
        set_conflict_annotation,
    )

    library = tmp_path / "mod"
    folder = _seed(library, "301")
    _write_targets(folder, "301", [str((tmp_path / "t.pak").resolve())])
    create_steam_test_mod(db, external_id="301", title="U")
    set_conflict_annotation(301, note="user", db=db)
    ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert db.get_mod_status(301).conflict_status == CONFLICT_STATUS_CONFLICT
    clear_conflict_annotation(301, db=db)
    ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert db.get_mod_status(301).conflict_status == CONFLICT_STATUS_NONE


def test_migration_clears_polluted_conflict_status(tmp_path: Path) -> None:
    from services.user_annotation import set_conflict_annotation

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "migrate_conflict.db")
    create_steam_test_mod(db, external_id="401", title="Polluted")
    with db._lock:  # noqa: SLF001
        # Simulate upgrade: drop one-shot flag, plant polluted row, re-run.
        db._conn.execute(  # noqa: SLF001
            "DELETE FROM schema_flags WHERE flag = ?",
            ("cleared_system_conflict_v1",),
        )
        db._conn.execute(  # noqa: SLF001
            "UPDATE mods SET conflict_status = ?, conflict_note = ? WHERE mod_id = ?",
            ("conflict", "system inferred", 401),
        )
        db._clear_system_inferred_conflict_pollution()  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    assert db.get_mod_status(401).conflict_status == CONFLICT_STATUS_NONE
    assert db.get_mod_status(401).conflict_note == ""
    # Second run is a no-op (flag set) — user marks must survive.
    set_conflict_annotation(401, note="user", db=db)
    with db._lock:  # noqa: SLF001
        db._clear_system_inferred_conflict_pollution()  # noqa: SLF001
        db._conn.commit()  # noqa: SLF001
    assert db.get_mod_status(401).conflict_status == CONFLICT_STATUS_CONFLICT
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
    folder = _seed(library, "501")
    create_steam_test_mod(db, external_id="501", title="Card")
    db.update_mod_identity_fields(
        "501",
        last_known_path=str(folder.resolve()),
        folder_present=True,
    )
    set_conflict_annotation(501, note="u", db=db)
    cache = get_library_cache()
    snap = cache.load_snapshot(library, force=True)
    cards = [c for c in snap.cards if c.id == "501"]
    assert cards
    assert cards[0].conflict is True
    assert cards[0].conflict_status == CONFLICT_STATUS_CONFLICT
    assert cards[0].content_status != CONTENT_IDENTITY_CONFLICT
    assert cards[0].content_status == CONTENT_HEALTHY or not cards[0].missing_content
