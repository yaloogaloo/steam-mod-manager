"""Architecture: Content Missing lifecycle — illegal producers forbidden.

ARCHITECTURE RULE
-----------------
Content Missing is a **system derived state**. It may only be produced from
authoritative content validation (``services.content_status_eval``).

Refresh, Sync entity registration, Import materialize, and Reconcile may
*invoke* that API. They must **not** hardcode ``content_status='content_missing'``
or call ``apply_missing_content_marker``.

Import, Deploy, Archive, IdentityRepair must not stamp missing from shallow
probes.
"""
from __future__ import annotations
import ast
import inspect
from pathlib import Path
import pytest
from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.content_status_eval import evaluate_content_status, persist_evaluated_content_status
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_reconcile import reconcile_library
from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY
from services.mod_refresh import reconcile_local_state
from tests.helpers.identity import create_steam_test_mod
ROOT = Path(__file__).resolve().parents[1]
_FORBIDDEN_MISSING_WRITERS = (ROOT / 'services' / 'library_reconcile.py', ROOT / 'services' / 'importers' / 'materialize.py', ROOT / 'services' / 'importers' / 'archive.py', ROOT / 'services' / 'deploy.py', ROOT / 'services' / 'identity_repair.py')

def _source(path: Path) -> str:
    return path.read_text(encoding='utf-8')

@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / 'arch_missing.db')
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

def test_forbidden_modules_do_not_literal_write_content_missing() -> None:
    """Static: illegal lifecycle modules must not hardcode content_missing writes."""
    for path in _FORBIDDEN_MISSING_WRITERS:
        text = _source(path)
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg in {'content_status', 'library_status'} and isinstance(kw.value, ast.Constant):
                        assert kw.value.value != 'content_missing', f"{path.name} must not pass content_status='content_missing' (use content_status_eval)"
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == 'content_status' and isinstance(node.value, ast.Constant):
                        assert node.value.value != 'content_missing', path.name

def test_forbidden_modules_do_not_call_apply_missing_content_marker() -> None:
    for path in (ROOT / 'services' / 'importers' / 'materialize.py', ROOT / 'services' / 'importers' / 'archive.py', ROOT / 'services' / 'library_reconcile.py', ROOT / 'services' / 'deploy.py', ROOT / 'services' / 'identity_repair.py'):
        tree = ast.parse(_source(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = ''
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                assert name != 'apply_missing_content_marker', f'{path.name} must not call apply_missing_content_marker'

def test_reconcile_does_not_reintroduce_missing_after_refresh(db: DatabaseManager, tmp_path: Path) -> None:
    """Oscillation guard: refresh clears false missing; reconcile must not restore it."""
    library = tmp_path / 'mod'
    mid = '880001'
    folder = _seed(library, mid, with_payload=True)
    db.update_game_deploy_config(424242, name='Game')
    create_steam_test_mod(db, external_id=mid, title='M', app_id=424242)

    db.update_mod_identity_fields(mid, folder_present=True, last_known_path=str(folder))
    db.update_mod_content_status(mid, content_status=CONTENT_CONTENT_MISSING, library_status='missing')
    local = reconcile_local_state(mid, folder, db=db)
    assert local.content_status == CONTENT_HEALTHY
    assert str((db.get_mod_backup_row(mid) or {}).get('content_status') or '') == CONTENT_HEALTHY
    reconcile_library(library)
    assert str((db.get_mod_backup_row(mid) or {}).get('content_status') or '') == CONTENT_HEALTHY

def test_reconcile_absent_marks_content_missing(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / 'mod'
    library.mkdir()
    mid = '880002'
    db.update_game_deploy_config(424242, name='Game')
    create_steam_test_mod(db, external_id=mid, title='Gone', app_id=424242)

    db.update_mod_identity_fields(mid, folder_present=True, last_known_path=str(library / 'Game' / 'Gone'))
    db.update_mod_content_status(mid, content_status=CONTENT_HEALTHY)
    reconcile_library(library)
    row = db.get_mod_backup_row(mid) or {}
    assert str(row.get('content_status') or '') == CONTENT_CONTENT_MISSING
    assert int(row.get('folder_present') or 0) == 0

def test_evaluator_is_sole_content_missing_persist_api() -> None:
    """content_status_eval must expose the persist entrance Refresh uses."""
    assert callable(evaluate_content_status)
    assert callable(persist_evaluated_content_status)
    src = inspect.getsource(persist_evaluated_content_status)
    assert 'content_status' in src
    assert 'CONTENT_CONTENT_MISSING' in src or 'content_missing' in src

def test_migration_clears_polluted_present_content_missing(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    db_path = tmp_path / 'migrate_missing.db'
    db = DatabaseManager.instance(db_path)
    mid = '880003'
    db.update_game_deploy_config(1, name='G')
    create_steam_test_mod(db, external_id=mid, title='X', app_id=1)

    db._conn.execute('DELETE FROM schema_flags WHERE flag = ?', ('cleared_illegal_content_missing_v1',))
    db._conn.execute("\n        UPDATE mods\n        SET content_status = 'content_missing',\n            library_status = 'missing',\n            folder_present = 1\n        WHERE mod_id = ?\n        ", (int(mid),))
    db._conn.commit()
    db._clear_illegal_content_missing_pollution()
    row = db.get_mod_backup_row(mid) or {}
    assert str(row.get('content_status') or '') == CONTENT_HEALTHY
    DatabaseManager.reset_instance()
