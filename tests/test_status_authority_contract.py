"""Architecture: Status Authority + Recovery contract.

Three independent axes::

    User Annotation  → conflict_status   (user_annotation only)
    System Fact      → content_status    (content_status_eval only)
    Identity Fact    → identity_status   (identity / reconcile only)

Status Recovery migrates historical pollution without touching user marks.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from core.mod_status import CONFLICT_STATUS_CONFLICT, CONFLICT_STATUS_NONE
from services.content_status_eval import persist_evaluated_content_status
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity, identity_create_scope
from services.library_reconcile import reconcile_library
from services.library_status import (
    CONTENT_CONTENT_MISSING,
    CONTENT_HEALTHY,
    CONTENT_IDENTITY_CONFLICT,
)
from services.status_authority import (
    IDENTITY_STATUS_CONFLICT,
    IDENTITY_STATUS_OK,
    STATUS_RECOVERY_FLAG,
)
from services.status_recovery import run_status_recovery
from services.user_annotation import set_conflict_annotation
from ui.library_query import FILTER_CONFLICT, ModFilterIndex, matches_status_filter

ROOT = Path(__file__).resolve().parents[1]
APP_ID = 424242
_FORBIDDEN_CONFLICT_WRITERS = (
    ROOT / "services" / "conflict.py",
    ROOT / "services" / "deploy.py",
    ROOT / "services" / "library_reconcile.py",
    ROOT / "services" / "identity_repair.py",
    ROOT / "services" / "content_status_eval.py",
    ROOT / "services" / "mod_refresh.py",
    ROOT / "services" / "sync.py",
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "status_auth.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name="Game", folder_name="Game"))
    yield manager
    DatabaseManager.reset_instance()


def _seed(library: Path, mid: str, *, with_payload: bool = True) -> Path:
    folder = library / "Game" / f"Mod{mid}"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        f'{{"published_file_id":"{mid}","title":"M{mid}","app_id":{APP_ID}}}',
        encoding="utf-8",
    )
    if with_payload:
        (folder / "mod.pak").write_bytes(b"x")
    return folder


def _register(db: DatabaseManager, mid: str, title: str, folder: Path) -> str:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(mid),
            workshop_id=str(mid),
            title=title,
            app_id=APP_ID,
            game_name="Game",
        )
    entity_id = str(created.mod_id)
    db.update_mod_identity_fields(
        entity_id,
        last_known_path=str(folder.resolve()),
        folder_present=True,
    )
    return entity_id


def test_system_modules_cannot_write_conflict_status() -> None:
    sig = inspect.signature(DatabaseManager.update_mod_status)
    assert "conflict_status" not in sig.parameters
    for path in _FORBIDDEN_CONFLICT_WRITERS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                assert kw.arg != "conflict_status", path.name


def test_identity_conflict_not_user_conflict_filter() -> None:
    idx = ModFilterIndex(
        mod_id="1",
        display_name="I",
        steam_name="",
        notes="",
        game_name="G",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=1.0,
        sort_name="I",
        content_status=CONTENT_HEALTHY,
        identity_status=IDENTITY_STATUS_CONFLICT,
        conflict_status="none",
    )
    assert not matches_status_filter(idx, FILTER_CONFLICT)
    assert not matches_status_filter(idx, "identity_conflict")


def test_content_missing_does_not_produce_conflict(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    folder = _seed(library, "801", with_payload=False)
    entity_id = _register(db, "801", "Empty", folder)
    persist_evaluated_content_status(
        entity_id, folder, db=db, folder_present=True, sync_sticky_marker=True
    )
    row = db.get_mod_backup_row(entity_id)
    assert str(row.get("content_status") or "") == CONTENT_CONTENT_MISSING
    assert db.get_mod_status(entity_id).conflict_status == CONFLICT_STATUS_NONE


def test_migration_preserves_user_conflict_and_moves_identity(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    folder = _seed(library, "802")
    entity_id = _register(db, "802", "Polluted", folder)
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET content_status = ?, library_status = ? WHERE mod_id = ?",
            (CONTENT_IDENTITY_CONFLICT, "conflict", int(entity_id)),
        )
        db._conn.commit()
    set_conflict_annotation(entity_id, note="user kept", db=db)
    assert db.get_mod_status(entity_id).conflict_status == CONFLICT_STATUS_CONFLICT
    with db._lock:
        db._conn.execute(
            "DELETE FROM schema_flags WHERE flag = ?", (STATUS_RECOVERY_FLAG,)
        )
        db._conn.commit()
    result = run_status_recovery(db, library, force=True)
    assert result.scanned >= 1
    st = db.get_mod_status(entity_id)
    assert st.conflict_status == CONFLICT_STATUS_CONFLICT
    assert st.conflict_note == "user kept"
    row = db.get_mod_backup_row(entity_id)
    assert str(row.get("content_status") or "") != CONTENT_IDENTITY_CONFLICT
    assert str(row.get("identity_status") or "") in {
        IDENTITY_STATUS_OK,
        IDENTITY_STATUS_CONFLICT,
    }
    assert str(row.get("library_status") or "") not in {
        "conflict",
        "identity_conflict",
    }
    assert str(row.get("content_status") or "") in {
        CONTENT_HEALTHY,
        CONTENT_CONTENT_MISSING,
    }


def test_delete_and_restore_payload_reevaluates_content(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    folder = _seed(library, "803", with_payload=True)
    entity_id = _register(db, "803", "Flip", folder)
    persist_evaluated_content_status(
        entity_id, folder, db=db, folder_present=True, sync_sticky_marker=True
    )
    assert (
        str((db.get_mod_backup_row(entity_id) or {}).get("content_status") or "")
        == CONTENT_HEALTHY
    )
    (folder / "mod.pak").unlink()
    persist_evaluated_content_status(
        entity_id, folder, db=db, folder_present=True, sync_sticky_marker=True
    )
    assert (
        str((db.get_mod_backup_row(entity_id) or {}).get("content_status") or "")
        == CONTENT_CONTENT_MISSING
    )
    (folder / "mod.pak").write_bytes(b"restored")
    persist_evaluated_content_status(
        entity_id, folder, db=db, folder_present=True, sync_sticky_marker=True
    )
    assert (
        str((db.get_mod_backup_row(entity_id) or {}).get("content_status") or "")
        == CONTENT_HEALTHY
    )


def test_reconcile_keeps_axes_consistent(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    folder = _seed(library, "804", with_payload=False)
    entity_id = _register(db, "804", "Rec", folder)
    set_conflict_annotation(entity_id, note="mark", db=db)
    reconcile_library(library)
    row = db.get_mod_backup_row(entity_id)
    assert db.get_mod_status(entity_id).conflict_status == CONFLICT_STATUS_CONFLICT
    assert str(row.get("content_status") or "") == CONTENT_CONTENT_MISSING
    assert str(row.get("identity_status") or "") in {
        IDENTITY_STATUS_OK,
        IDENTITY_STATUS_CONFLICT,
        "unresolved",
        "",
    }


def test_reconcile_writes_identity_status_not_content(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    a = _seed(library, "805")
    entity_id = _register(db, "805", "Dup", a)
    # Stamp Internal ID into both sidecars so reconcile sees one entity, two folders.
    for folder in (a,):
        meta = folder / INFO_DIR_NAME / METADATA_FILENAME
        meta.write_text(
            f'{{"internal_id":"{entity_id}","published_file_id":"805",'
            f'"title":"Dup","app_id":{APP_ID}}}',
            encoding="utf-8",
        )
    b = library / "Game" / "Dup805"
    info = b / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        f'{{"internal_id":"{entity_id}","published_file_id":"805",'
        f'"title":"Dup","app_id":{APP_ID}}}',
        encoding="utf-8",
    )
    (b / "mod.pak").write_bytes(b"y")
    reconcile_library(library)
    row = db.get_mod_backup_row(entity_id)
    assert str(row.get("identity_status") or "") == IDENTITY_STATUS_CONFLICT
    assert str(row.get("content_status") or "") != CONTENT_IDENTITY_CONFLICT
    assert db.get_mod_status(entity_id).conflict_status == CONFLICT_STATUS_NONE
