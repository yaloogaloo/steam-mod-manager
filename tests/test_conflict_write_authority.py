"""Architecture: Conflict write authority — user annotation only.

ARCHITECTURE RULE
-----------------
``mods.conflict_status`` is a pure user mark.

Legal lifecycle::

    Detail Panel → services.user_annotation → mods.conflict_status

Sync / Refresh / Deploy / Reconcile / Identity / Repair / ConflictDetector
must not call conflict write APIs. Content Missing must never produce Conflict.
"""
from __future__ import annotations
import ast
import inspect
from pathlib import Path
import pytest
from core.db_manager import DatabaseManager
from core.mod_status import CONFLICT_STATUS_CONFLICT, CONFLICT_STATUS_NONE
from core.models import ModMetadata
from services.content_status_eval import evaluate_content_status, persist_evaluated_content_status
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_reconcile import reconcile_library
from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY, CONTENT_IDENTITY_CONFLICT
from services.user_annotation import clear_conflict_annotation, set_conflict_annotation
from tests.helpers.identity import bind_managed_path, create_steam_test_mod
ROOT = Path(__file__).resolve().parents[1]
_FORBIDDEN_CONFLICT_WRITERS = (ROOT / 'services' / 'conflict.py', ROOT / 'services' / 'deploy.py', ROOT / 'services' / 'library_reconcile.py', ROOT / 'services' / 'identity_repair.py', ROOT / 'services' / 'identity_repair_service.py', ROOT / 'services' / 'identity_service.py', ROOT / 'services' / 'mod_refresh.py', ROOT / 'services' / 'sync.py', ROOT / 'services' / 'content_status_eval.py', ROOT / 'services' / 'library_status.py', ROOT / 'services' / 'mod_library_cache.py', ROOT / 'ui' / 'library_view.py', ROOT / 'ui' / 'mod_card.py', ROOT / 'ui' / 'sync_thread.py', ROOT / 'ui' / 'import_thread.py')
_BANNED_CALL_NAMES = frozenset({'update_mod_conflict_annotation', 'set_conflict_annotation', 'clear_conflict_annotation', 'apply_conflict_annotation'})
_ALLOWED_WRITE_MODULES = frozenset({'services/user_annotation.py', 'ui/mod_detail_panel.py', 'core/db_manager.py'})

def _source(path: Path) -> str:
    return path.read_text(encoding='utf-8')

def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()

@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / 'conflict_auth.db')
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

def test_forbidden_modules_do_not_call_conflict_write_apis() -> None:
    """Static call-graph scan: illegal modules must not invoke conflict writers."""
    for path in _FORBIDDEN_CONFLICT_WRITERS:
        assert path.is_file(), f'missing module under scan: {path}'
        tree = ast.parse(_source(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = ''
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            assert name not in _BANNED_CALL_NAMES, f'{_rel(path)} must not call {name} (Conflict write is user_annotation only)'
            if name in {'update_mod_status', 'update_mod_identity_fields', 'execute'}:
                for kw in node.keywords:
                    assert kw.arg != 'conflict_status', f'{_rel(path)} must not pass conflict_status= to {name}'

def test_update_mod_status_has_no_conflict_parameters() -> None:
    """update_mod_status must not accept conflict_status / conflict_note."""
    sig = inspect.signature(DatabaseManager.update_mod_status)
    assert 'conflict_status' not in sig.parameters
    assert 'conflict_note' not in sig.parameters
    sig2 = inspect.signature(DatabaseManager.update_mod_conflict_annotation)
    assert 'conflict' in sig2.parameters

def test_only_allowed_modules_call_conflict_annotation_writer() -> None:
    """Repo AST scan: conflict annotation writer *calls* stay in allow-list."""
    offenders: list[str] = []
    for path in ROOT.rglob('*.py'):
        rel = _rel(path)
        if rel.startswith('tests/') or rel.startswith('tools/'):
            continue
        if rel in _ALLOWED_WRITE_MODULES:
            continue
        tree = ast.parse(_source(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = ''
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in _BANNED_CALL_NAMES:
                offenders.append(f'{rel}:{name}')
    assert offenders == [], 'Conflict write call sites outside User Annotation allow-list: ' + ', '.join(sorted(set(offenders)))

def test_content_missing_does_not_produce_conflict(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '701', with_payload=False)
    db.update_game_deploy_config(424242, name='Game')
    create_steam_test_mod(db, external_id='701', title='Empty', app_id=424242, game_name='Game')
    bind_managed_path(db, '701', folder)
    assert db.get_mod_status(701).conflict_status == CONFLICT_STATUS_NONE
    status = evaluate_content_status(folder_present=True, managed_path=folder, metadata_missing=False)
    assert status == CONTENT_CONTENT_MISSING
    persist_evaluated_content_status('701', folder, db=db, folder_present=True, metadata_missing=False, sync_sticky_marker=True)
    row = db.get_mod_backup_row('701')
    assert row is not None
    assert str(row.get('content_status') or '') == CONTENT_CONTENT_MISSING
    assert db.get_mod_status(701).conflict_status == CONFLICT_STATUS_NONE

def test_missing_preserves_user_conflict_annotation(tmp_path: Path, db: DatabaseManager) -> None:
    """Regression: content_status=missing must keep user conflict_status."""
    library = tmp_path / 'mod'
    folder = _seed(library, '702', with_payload=True)
    db.update_game_deploy_config(424242, name='Game')
    create_steam_test_mod(db, external_id='702', title='KeepMark', app_id=424242, game_name='Game')
    bind_managed_path(db, '702', folder)
    set_conflict_annotation(702, note='user kept', db=db)
    assert db.get_mod_status(702).conflict_status == CONFLICT_STATUS_CONFLICT
    (folder / 'mod.pak').unlink()
    persist_evaluated_content_status('702', folder, db=db, folder_present=True, metadata_missing=False, sync_sticky_marker=True)
    row = db.get_mod_backup_row('702')
    assert str(row.get('content_status') or '') == CONTENT_CONTENT_MISSING
    st = db.get_mod_status(702)
    assert st.conflict_status == CONFLICT_STATUS_CONFLICT
    assert st.conflict_note == 'user kept'
    reconcile_library(library)
    st2 = db.get_mod_status(702)
    assert st2.conflict_status == CONFLICT_STATUS_CONFLICT
    assert st2.conflict_note == 'user kept'

def test_identity_conflict_does_not_write_user_conflict(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '703')
    db.update_game_deploy_config(424242, name='Game')
    create_steam_test_mod(db, external_id='703', title='Id', app_id=424242, game_name='Game')
    bind_managed_path(db, '703', folder)
    db.update_mod_identity_fields('703', identity_status=CONTENT_IDENTITY_CONFLICT)
    db.update_mod_content_status('703', content_status=CONTENT_HEALTHY)
    assert db.get_mod_status(703).conflict_status == CONFLICT_STATUS_NONE
    row = db.get_mod_backup_row('703')
    assert str(row.get('identity_status') or '') == CONTENT_IDENTITY_CONFLICT
    assert str(row.get('content_status') or '') == CONTENT_HEALTHY

def test_user_annotation_is_only_writer_roundtrip(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '704')
    create_steam_test_mod(db, external_id='704', title='User')
    bind_managed_path(db, '704', folder)
    db.update_mod_status(704, invalid=False, touch_check_time=True)
    assert db.get_mod_status(704).conflict_status == CONFLICT_STATUS_NONE
    set_conflict_annotation(704, note='from detail', db=db)
    assert db.get_mod_status(704).conflict_status == CONFLICT_STATUS_CONFLICT
    clear_conflict_annotation(704, db=db)
    assert db.get_mod_status(704).conflict_status == CONFLICT_STATUS_NONE

def test_filter_conflict_ignores_identity_and_content_missing() -> None:
    from ui.library_query import FILTER_CONFLICT, ModFilterIndex, matches_status_filter
    identity = ModFilterIndex(mod_id='1', display_name='I', steam_name='', notes='', game_name='G', favorite=False, deployed=False, has_offline=False, mtime=1.0, sort_name='I', content_status=CONTENT_HEALTHY, identity_status=CONTENT_IDENTITY_CONFLICT, conflict=False, conflict_status='none')
    missing = ModFilterIndex(mod_id='2', display_name='M', steam_name='', notes='', game_name='G', favorite=False, deployed=False, has_offline=False, mtime=1.0, sort_name='M', content_status=CONTENT_CONTENT_MISSING, conflict=False, conflict_status='none')
    user = ModFilterIndex(mod_id='3', display_name='U', steam_name='', notes='', game_name='G', favorite=False, deployed=False, has_offline=False, mtime=1.0, sort_name='U', content_status=CONTENT_HEALTHY, conflict=True, conflict_status=CONFLICT_STATUS_CONFLICT)
    assert not matches_status_filter(identity, FILTER_CONFLICT)
    assert not matches_status_filter(identity, 'identity_conflict')
    assert not matches_status_filter(missing, FILTER_CONFLICT)
    assert matches_status_filter(user, FILTER_CONFLICT)
