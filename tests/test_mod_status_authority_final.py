"""Final Mod status authority contract (Phase 2).

Permanent user-facing Mod status::

    停更 (abandoned tag) / 失效 (is_invalid) / 冲突 (conflict_status)
    / 内容缺失 (content_status=content_missing)

Identity fact: identity_status (never user Conflict).
Deploy outcome: mods.deploy_status (deploy service only).
Deploy Record overlays: 记录缺失 / 额外部署 (memory-only).
"""
from __future__ import annotations
import ast
import inspect
from pathlib import Path
import pytest
from core.db_manager import DEPLOY_STATUS_DEPLOYED, DEPLOY_STATUS_NOT_DEPLOYED, DatabaseManager
from core.mod_status import CONFLICT_STATUS_CONFLICT, CONFLICT_STATUS_NONE
from core.models import ModMetadata
from services.content_status_eval import persist_evaluated_content_status
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY
from services.status_authority import FORBIDDEN_RECORD_STATUS_COLUMNS, IDENTITY_STATUS_CONFLICT, POLLUTION_CONTENT_TOKENS, POLLUTION_LIBRARY_TOKENS, RECORD_OVERLAY_EXTRA_LABEL, RECORD_OVERLAY_MISSING_LABEL, STATUS_RECOVERY_FLAG, SUPPORTED_CONTENT_STATUSES, SUPPORTED_DEPLOY_STATUSES, SUPPORTED_IDENTITY_STATUSES
from services.status_recovery import run_status_recovery
from services.user_annotation import set_conflict_annotation
from ui.library_query import FILTER_CONFLICT, ModFilterIndex, RECORD_STATUS_LABEL_EXTRA, RECORD_STATUS_LABEL_MISSING, RecordRelativeStatus, compute_record_relative_status, matches_status_filter, record_relative_badge_label
ROOT = Path(__file__).resolve().parents[1]
_SYSTEM_MODULES = (ROOT / 'services' / 'conflict.py', ROOT / 'services' / 'deploy.py', ROOT / 'services' / 'library_reconcile.py', ROOT / 'services' / 'identity_repair.py', ROOT / 'services' / 'content_status_eval.py', ROOT / 'services' / 'mod_refresh.py', ROOT / 'services' / 'sync.py', ROOT / 'services' / 'path_lifecycle.py', ROOT / 'services' / 'status_recovery.py')
_NON_DEPLOY_MODULES = (ROOT / 'services' / 'library_reconcile.py', ROOT / 'services' / 'mod_refresh.py', ROOT / 'services' / 'sync.py', ROOT / 'services' / 'status_recovery.py', ROOT / 'services' / 'content_status_eval.py', ROOT / 'services' / 'identity_repair.py', ROOT / 'services' / 'conflict.py')

@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / 'status_final.db')
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

def test_system_flows_cannot_write_conflict_status() -> None:
    sig = inspect.signature(DatabaseManager.update_mod_status)
    assert 'conflict_status' not in sig.parameters
    for path in _SYSTEM_MODULES:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                assert kw.arg != 'conflict_status', path.name

def test_non_deploy_modules_cannot_call_update_mod_deploy_status() -> None:
    for path in _NON_DEPLOY_MODULES:
        src = path.read_text(encoding='utf-8')
        assert 'update_mod_deploy_status(' not in src, path.name

def test_content_missing_does_not_produce_user_conflict(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '901', with_payload=False)
    db.update_game_deploy_config(424242, name='Game')
    db.upsert_mod(ModMetadata(published_file_id='901', title='Empty', managed_path=str(folder), app_id=424242))
    persist_evaluated_content_status('901', folder, db=db, folder_present=True, sync_sticky_marker=True)
    row = db.get_mod_backup_row('901')
    assert str(row.get('content_status') or '') == CONTENT_CONTENT_MISSING
    assert db.get_mod_status(901).conflict_status == CONFLICT_STATUS_NONE

def test_identity_status_not_shown_as_user_conflict() -> None:
    idx = ModFilterIndex(mod_id='1', display_name='I', steam_name='', notes='', game_name='G', favorite=False, deployed=False, has_offline=False, mtime=1.0, sort_name='I', content_status=CONTENT_HEALTHY, identity_status=IDENTITY_STATUS_CONFLICT, conflict_status='none')
    assert not matches_status_filter(idx, FILTER_CONFLICT)
    assert not matches_status_filter(idx, 'identity_conflict')

def test_status_recovery_preserves_deploy_status(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '902')
    db.update_game_deploy_config(424242, name='Game')
    db.upsert_mod(ModMetadata(published_file_id='902', title='Deployed', managed_path=str(folder), app_id=424242))
    db.update_mod_identity_fields('902', last_known_path=str(folder.resolve()), folder_present=True)
    with db._lock:
        db._conn.execute('UPDATE mods SET content_status = ?, library_status = ? WHERE mod_id = ?', ('identity_conflict', 'conflict', 902))
        db._conn.commit()
    db.update_mod_deploy_status(902, deploy_status=DEPLOY_STATUS_DEPLOYED, deploy_path='/game/mods/902', deploy_error='')
    set_conflict_annotation(902, note='user', db=db)
    with db._lock:
        db._conn.execute('DELETE FROM schema_flags WHERE flag = ?', (STATUS_RECOVERY_FLAG,))
        db._conn.commit()
    run_status_recovery(db, library, force=True)
    info = db.get_mod_deploy_info(902)
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED
    assert info.deploy_path == '/game/mods/902'
    assert db.get_mod_status(902).conflict_status == CONFLICT_STATUS_CONFLICT
    row = db.get_mod_backup_row('902')
    assert str(row.get('content_status') or '') != 'identity_conflict'
    assert str(row.get('library_status') or '') != 'conflict'

def test_deploy_status_lifecycle_values(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '903')
    db.update_game_deploy_config(424242, name='Game')
    db.upsert_mod(ModMetadata(published_file_id='903', title='D', managed_path=str(folder), app_id=424242))
    assert db.get_mod_deploy_info(903).deploy_status == DEPLOY_STATUS_NOT_DEPLOYED
    db.update_mod_deploy_status(903, deploy_status=DEPLOY_STATUS_DEPLOYED, deploy_path='/x')
    assert db.get_mod_deploy_info(903).deploy_status == DEPLOY_STATUS_DEPLOYED
    db.update_mod_deploy_status(903, deploy_status=DEPLOY_STATUS_NOT_DEPLOYED)
    assert db.get_mod_deploy_info(903).deploy_status == DEPLOY_STATUS_NOT_DEPLOYED

def test_record_overlays_are_memory_only_and_match_existing_labels(db: DatabaseManager) -> None:
    assert RECORD_OVERLAY_MISSING_LABEL == RECORD_STATUS_LABEL_MISSING == '记录缺失'
    assert RECORD_OVERLAY_EXTRA_LABEL == RECORD_STATUS_LABEL_EXTRA == '额外部署'
    for table in ('mods', 'deployment_records', 'deployment_record_items'):
        cols = {str(r[1]) for r in db._conn.execute(f'PRAGMA table_info({table})').fetchall()}
        assert not cols & FORBIDDEN_RECORD_STATUS_COLUMNS, table
    recorded = frozenset({'1', '2'})
    missing = compute_record_relative_status(ModFilterIndex(mod_id='1', display_name='A', steam_name='', notes='', game_name='G', favorite=False, deployed=False, has_offline=False, mtime=0.0, sort_name='a'), recorded)
    extra = compute_record_relative_status(ModFilterIndex(mod_id='9', display_name='B', steam_name='', notes='', game_name='G', favorite=False, deployed=True, has_offline=False, mtime=0.0, sort_name='b'), recorded)
    assert isinstance(missing, RecordRelativeStatus)
    assert record_relative_badge_label(missing) == '记录缺失'
    assert record_relative_badge_label(extra) == '额外部署'
    assert compute_record_relative_status(ModFilterIndex(mod_id='1', display_name='A', steam_name='', notes='', game_name='G', favorite=False, deployed=True, has_offline=False, mtime=0.0, sort_name='a'), None) is None

def test_no_unknown_permanent_status_tokens(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / 'mod'
    folder = _seed(library, '904')
    db.update_game_deploy_config(424242, name='Game')
    db.upsert_mod(ModMetadata(published_file_id='904', title='T', managed_path=str(folder), app_id=424242))
    db.update_mod_identity_fields('904', last_known_path=str(folder.resolve()), folder_present=True)
    with db._lock:
        db._conn.execute('UPDATE mods SET content_status = ?, library_status = ? WHERE mod_id = ?', ('file_missing', 'conflict', 904))
        db._conn.commit()
    with db._lock:
        db._conn.execute('DELETE FROM schema_flags WHERE flag = ?', (STATUS_RECOVERY_FLAG,))
        db._conn.commit()
    run_status_recovery(db, library, force=True)
    row = db.get_mod_backup_row('904')
    cs = str(row.get('content_status') or '')
    ls = str(row.get('library_status') or '')
    ids = str(row.get('identity_status') or '')
    ds = str(db.get_mod_deploy_info(904).deploy_status or '')
    assert cs in SUPPORTED_CONTENT_STATUSES
    assert cs.lower() not in {t.lower() for t in POLLUTION_CONTENT_TOKENS}
    assert ls.lower() not in {t.lower() for t in POLLUTION_LIBRARY_TOKENS}
    assert ids in SUPPORTED_IDENTITY_STATUSES
    assert ds in SUPPORTED_DEPLOY_STATUSES
