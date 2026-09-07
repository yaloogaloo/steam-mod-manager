"""Final Mod status authority — cleanup lock (no new status model).

Permanent Mod status whitelist::

    User: conflict / abandoned / invalid
    System: content_missing (via content_status)
    Identity: identity_status (separate)
    Deploy: deploy_status + record overlays (separate)

No bridge layers. No UI hide patches.
"""
from __future__ import annotations
import ast
import inspect
from pathlib import Path
import pytest
from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager
from core.mod_status import CONFLICT_STATUS_CONFLICT, CONFLICT_STATUS_NONE
from core.models import ModMetadata
from services.content_status_eval import persist_evaluated_content_status
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY
from services.status_authority import FORBIDDEN_RECORD_STATUS_COLUMNS, IDENTITY_STATUS_CONFLICT, POLLUTION_CONTENT_TOKENS, STATUS_RECOVERY_FLAG, SUPPORTED_CONTENT_STATUSES, SUPPORTED_DEPLOY_STATUSES, SUPPORTED_IDENTITY_STATUSES
from services.status_recovery import run_status_recovery
from services.user_annotation import set_conflict_annotation
from ui.library_query import FILTER_CONFLICT, ModFilterIndex, RECORD_STATUS_LABEL_EXTRA, RECORD_STATUS_LABEL_MISSING, compute_record_relative_status, matches_status_filter, record_relative_badge_label
ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / 'services'
UI = ROOT / 'ui'
_SYSTEM_FOR_CONFLICT = (SERVICES / 'conflict.py', SERVICES / 'deploy.py', SERVICES / 'library_reconcile.py', SERVICES / 'identity_repair.py', SERVICES / 'identity_service.py', SERVICES / 'content_status_eval.py', SERVICES / 'mod_refresh.py', SERVICES / 'sync.py', SERVICES / 'path_lifecycle.py', SERVICES / 'status_recovery.py', SERVICES / 'mod_library_cache.py')
_FORBIDDEN_CONTENT_DIRECT = (SERVICES / 'library_reconcile.py', SERVICES / 'identity_repair.py', SERVICES / 'identity_service.py', SERVICES / 'mod_refresh.py', SERVICES / 'sync.py', SERVICES / 'path_lifecycle.py', SERVICES / 'deploy.py', SERVICES / 'conflict.py')

@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / 'final_status.db')
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

def _pollute_content(db: DatabaseManager, mid: int, content: str, library: str) -> None:
    with db._lock:
        db._conn.execute('UPDATE mods SET content_status = ?, library_status = ? WHERE mod_id = ?', (content, library, mid))
        db._conn.commit()

def test_only_user_annotation_writes_conflict_status() -> None:
    sig = inspect.signature(DatabaseManager.update_mod_status)
    assert 'conflict_status' not in sig.parameters
    banned_funcs = {'update_mod_status', 'update_mod_identity_fields', 'update_mod_content_status', 'update_mod_deploy_status', 'persist_evaluated_content_status'}
    for path in _SYSTEM_FOR_CONFLICT:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ''
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name not in banned_funcs and name != 'update_mod_conflict_annotation':
                continue
            for kw in node.keywords:
                assert kw.arg != 'conflict_status', f'{path.name}:{name}'
    detail = (UI / 'mod_detail_panel.py').read_text(encoding='utf-8')
    assert 'update_mod_conflict_annotation(' not in detail
    assert 'apply_conflict_annotation' in detail or 'set_conflict_annotation' in detail

def test_content_status_only_via_content_status_eval() -> None:
    for path in _FORBIDDEN_CONTENT_DIRECT:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ''
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name == 'update_mod_content_status':
                raise AssertionError(f'{path.name} must not call update_mod_content_status')
            if name != 'update_mod_identity_fields':
                continue
            for kw in node.keywords:
                if kw.arg in {'content_status', 'library_status'}:
                    raise AssertionError(f'{path.name} must not pass {kw.arg}= to update_mod_identity_fields')
    eval_src = (SERVICES / 'content_status_eval.py').read_text(encoding='utf-8')
    assert 'update_mod_content_status(' in eval_src

def test_identity_status_not_user_conflict_badge() -> None:
    idx = ModFilterIndex(mod_id='1', display_name='I', steam_name='', notes='', game_name='G', favorite=False, deployed=False, has_offline=False, mtime=1.0, sort_name='I', content_status=CONTENT_HEALTHY, identity_status=IDENTITY_STATUS_CONFLICT, conflict_status='none')
    assert not matches_status_filter(idx, FILTER_CONFLICT)
    assert not matches_status_filter(idx, 'identity_conflict')

def test_status_recovery_preserves_deploy_and_record_schema(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '911')
    db.update_game_deploy_config(424242, name='Game')
    db.upsert_mod(ModMetadata(published_file_id='911', title='D', managed_path=str(folder), app_id=424242))
    _pollute_content(db, 911, 'identity_conflict', 'conflict')
    db.update_mod_deploy_status(911, deploy_status=DEPLOY_STATUS_DEPLOYED, deploy_path='/g/911')
    set_conflict_annotation(911, note='user', db=db)
    record = db.create_deployment_record(424242, 'slot-a', [911])
    before_ids = db.get_deployment_record_mod_ids(record.id)
    with db._lock:
        db._conn.execute('DELETE FROM schema_flags WHERE flag = ?', (STATUS_RECOVERY_FLAG,))
        db._conn.commit()
    run_status_recovery(db, library, force=True)
    info = db.get_mod_deploy_info(911)
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED
    assert info.deploy_path == '/g/911'
    assert db.get_mod_status(911).conflict_status == CONFLICT_STATUS_CONFLICT
    assert db.get_deployment_record_mod_ids(record.id) == before_ids
    for table in ('mods', 'deployment_records', 'deployment_record_items'):
        cols = {str(r[1]) for r in db._conn.execute(f'PRAGMA table_info({table})').fetchall()}
        assert not cols & FORBIDDEN_RECORD_STATUS_COLUMNS

def test_deployment_record_overlays_unchanged() -> None:
    recorded = frozenset({'1'})
    missing = compute_record_relative_status(ModFilterIndex(mod_id='1', display_name='A', steam_name='', notes='', game_name='G', favorite=False, deployed=False, has_offline=False, mtime=0.0, sort_name='a'), recorded)
    extra = compute_record_relative_status(ModFilterIndex(mod_id='2', display_name='B', steam_name='', notes='', game_name='G', favorite=False, deployed=True, has_offline=False, mtime=0.0, sort_name='b'), recorded)
    assert record_relative_badge_label(missing) == RECORD_STATUS_LABEL_MISSING == '记录缺失'
    assert record_relative_badge_label(extra) == RECORD_STATUS_LABEL_EXTRA == '额外部署'

def test_no_unknown_permanent_tokens_after_recovery(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '912')
    db.update_game_deploy_config(424242, name='Game')
    db.upsert_mod(ModMetadata(published_file_id='912', title='T', managed_path=str(folder), app_id=424242))
    _pollute_content(db, 912, 'file_missing', 'conflict')
    with db._lock:
        db._conn.execute('DELETE FROM schema_flags WHERE flag = ?', (STATUS_RECOVERY_FLAG,))
        db._conn.commit()
    run_status_recovery(db, library, force=True)
    row = db.get_mod_backup_row('912')
    cs = str(row.get('content_status') or '')
    assert cs in SUPPORTED_CONTENT_STATUSES
    assert cs.lower() not in {t.lower() for t in POLLUTION_CONTENT_TOKENS}
    assert str(row.get('identity_status') or '') in SUPPORTED_IDENTITY_STATUSES
    assert db.get_mod_deploy_info(912).deploy_status in SUPPORTED_DEPLOY_STATUSES

def test_content_missing_auto_detect_not_conflict(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '913', with_payload=False)
    db.update_game_deploy_config(424242, name='Game')
    db.upsert_mod(ModMetadata(published_file_id='913', title='E', managed_path=str(folder), app_id=424242))
    persist_evaluated_content_status('913', folder, db=db, folder_present=True, sync_sticky_marker=True)
    assert str((db.get_mod_backup_row('913') or {}).get('content_status') or '') == CONTENT_CONTENT_MISSING
    assert db.get_mod_status(913).conflict_status == CONFLICT_STATUS_NONE

def test_card_does_not_infer_status_without_projection() -> None:
    src = (UI / 'mod_card.py').read_text(encoding='utf-8')
    assert '文件覆盖冲突' not in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in {'get_mod_status', 'get_mods_tag_flags', 'get_mod_backup_row'}:
                raise AssertionError(f'mod_card must not call {node.func.attr} for status display')
