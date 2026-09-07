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
from core.mod_status import CONFLICT_STATUS_CONFLICT, CONFLICT_STATUS_NONE
from core.models import ModMetadata
from services.content_status_eval import persist_evaluated_content_status
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_reconcile import reconcile_library
from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY, CONTENT_IDENTITY_CONFLICT
from services.status_authority import IDENTITY_STATUS_CONFLICT, IDENTITY_STATUS_OK, STATUS_RECOVERY_FLAG
from services.status_recovery import run_status_recovery
from services.user_annotation import set_conflict_annotation
from ui.library_query import FILTER_CONFLICT, ModFilterIndex, matches_status_filter
ROOT = Path(__file__).resolve().parents[1]
_FORBIDDEN_CONFLICT_WRITERS = (ROOT / 'services' / 'conflict.py', ROOT / 'services' / 'deploy.py', ROOT / 'services' / 'library_reconcile.py', ROOT / 'services' / 'identity_repair.py', ROOT / 'services' / 'content_status_eval.py', ROOT / 'services' / 'mod_refresh.py', ROOT / 'services' / 'sync.py')

@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / 'status_auth.db')
    yield manager
    DatabaseManager.reset_instance()

def _seed(library: Path, mid: str, *, with_payload: bool=True) -> Path:
    folder = library / 'Game' / f'Mod{mid}'
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(f'{{"published_file_id":"{mid}","title":"M{mid}","app_id":424242}}', encoding='utf-8')
    if with_payload:
        (folder / 'mod.pak').write_bytes(b'x')
    return folder

def test_system_modules_cannot_write_conflict_status() -> None:
    sig = inspect.signature(DatabaseManager.update_mod_status)
    assert 'conflict_status' not in sig.parameters
    for path in _FORBIDDEN_CONFLICT_WRITERS:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                assert kw.arg != 'conflict_status', path.name

def test_identity_conflict_not_user_conflict_filter() -> None:
    idx = ModFilterIndex(mod_id='1', display_name='I', steam_name='', notes='', game_name='G', favorite=False, deployed=False, has_offline=False, mtime=1.0, sort_name='I', content_status=CONTENT_HEALTHY, identity_status=IDENTITY_STATUS_CONFLICT, conflict_status='none')
    assert not matches_status_filter(idx, FILTER_CONFLICT)
    # Identity is not a Library filter — never matches.
    assert not matches_status_filter(idx, 'identity_conflict')

def test_content_missing_does_not_produce_conflict(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '801', with_payload=False)
    db.update_game_deploy_config(424242, name='Game')
    db.upsert_mod(ModMetadata(published_file_id='801', title='Empty', managed_path=str(folder), app_id=424242))
    persist_evaluated_content_status('801', folder, db=db, folder_present=True, sync_sticky_marker=True)
    row = db.get_mod_backup_row('801')
    assert str(row.get('content_status') or '') == CONTENT_CONTENT_MISSING
    assert db.get_mod_status(801).conflict_status == CONFLICT_STATUS_NONE

def test_migration_preserves_user_conflict_and_moves_identity(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '802')
    db.update_game_deploy_config(424242, name='Game')
    db.upsert_mod(ModMetadata(published_file_id='802', title='Polluted', managed_path=str(folder), app_id=424242))
    db.update_mod_identity_fields('802', last_known_path=str(folder.resolve()), folder_present=True)
    with db._lock:
        db._conn.execute('UPDATE mods SET content_status = ?, library_status = ? WHERE mod_id = ?', (CONTENT_IDENTITY_CONFLICT, 'conflict', 802))
        db._conn.commit()
    set_conflict_annotation(802, note='user kept', db=db)
    assert db.get_mod_status(802).conflict_status == CONFLICT_STATUS_CONFLICT
    with db._lock:
        db._conn.execute('DELETE FROM schema_flags WHERE flag = ?', (STATUS_RECOVERY_FLAG,))
        db._conn.commit()
    result = run_status_recovery(db, library, force=True)
    assert result.scanned >= 1
    st = db.get_mod_status(802)
    assert st.conflict_status == CONFLICT_STATUS_CONFLICT
    assert st.conflict_note == 'user kept'
    row = db.get_mod_backup_row('802')
    assert str(row.get('content_status') or '') != CONTENT_IDENTITY_CONFLICT
    # Pollution moved off content; may stay on identity until multi-folder recompute.
    assert str(row.get('identity_status') or '') in {
        IDENTITY_STATUS_OK,
        IDENTITY_STATUS_CONFLICT,
    }
    assert str(row.get('library_status') or '') not in {'conflict', 'identity_conflict'}
    assert str(row.get('content_status') or '') in {CONTENT_HEALTHY, CONTENT_CONTENT_MISSING}

def test_delete_and_restore_payload_reevaluates_content(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '803', with_payload=True)
    db.update_game_deploy_config(424242, name='Game')
    db.upsert_mod(ModMetadata(published_file_id='803', title='Flip', managed_path=str(folder), app_id=424242))
    db.update_mod_identity_fields('803', last_known_path=str(folder.resolve()), folder_present=True)
    persist_evaluated_content_status('803', folder, db=db, folder_present=True, sync_sticky_marker=True)
    assert str((db.get_mod_backup_row('803') or {}).get('content_status') or '') == CONTENT_HEALTHY
    (folder / 'mod.pak').unlink()
    persist_evaluated_content_status('803', folder, db=db, folder_present=True, sync_sticky_marker=True)
    assert str((db.get_mod_backup_row('803') or {}).get('content_status') or '') == CONTENT_CONTENT_MISSING
    (folder / 'mod.pak').write_bytes(b'restored')
    persist_evaluated_content_status('803', folder, db=db, folder_present=True, sync_sticky_marker=True)
    assert str((db.get_mod_backup_row('803') or {}).get('content_status') or '') == CONTENT_HEALTHY

def test_reconcile_keeps_axes_consistent(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '804', with_payload=False)
    db.update_game_deploy_config(424242, name='Game')
    db.upsert_mod(ModMetadata(published_file_id='804', title='Rec', managed_path=str(folder), app_id=424242))
    db.update_mod_identity_fields('804', last_known_path=str(folder.resolve()), folder_present=True)
    set_conflict_annotation(804, note='mark', db=db)
    reconcile_library(library)
    row = db.get_mod_backup_row('804')
    assert db.get_mod_status(804).conflict_status == CONFLICT_STATUS_CONFLICT
    assert str(row.get('content_status') or '') == CONTENT_CONTENT_MISSING
    assert str(row.get('identity_status') or '') in {IDENTITY_STATUS_OK, IDENTITY_STATUS_CONFLICT, 'unresolved', ''}

def test_reconcile_writes_identity_status_not_content(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    a = _seed(library, '805')
    b = library / 'Game' / 'Dup805'
    info = b / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text('{"published_file_id":"805","title":"Dup","app_id":424242}', encoding='utf-8')
    (b / 'mod.pak').write_bytes(b'y')
    db.update_game_deploy_config(424242, name='Game')
    db.upsert_mod(ModMetadata(published_file_id='805', title='Dup', managed_path=str(a), app_id=424242))
    reconcile_library(library)
    row = db.get_mod_backup_row('805')
    assert str(row.get('identity_status') or '') == IDENTITY_STATUS_CONFLICT
    assert str(row.get('content_status') or '') != CONTENT_IDENTITY_CONFLICT
    assert db.get_mod_status(805).conflict_status == CONFLICT_STATUS_NONE
