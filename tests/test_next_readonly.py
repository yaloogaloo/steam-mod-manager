"""Readonly preview gates use only isolated databases and synthetic rows."""
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest
from core.db_manager import DatabaseManager
from frontend.library_adapter import LibraryAdapter


@pytest.fixture
def readonly_db(tmp_path):
    path = tmp_path / 'readonly.db'
    DatabaseManager(path).close()
    db = DatabaseManager.open_existing(path)
    yield db
    db.close()


@pytest.mark.parametrize('sql', [
    'DELETE FROM mods', "UPDATE mods SET favorite=1", 'PRAGMA query_only=OFF',
    "ATTACH DATABASE ':memory:' AS other", 'PRAGMA writable_schema=ON',
    'PRAGMA wal_checkpoint', 'VACUUM', 'CREATE TABLE bad(id)', 'PRAGMA journal_mode=DELETE',
])
def test_engine_rejects_writes_and_escape(readonly_db, sql):
    with pytest.raises(sqlite3.DatabaseError):
        readonly_db._conn.execute(sql)


def test_readonly_connection_cannot_gain_writable_adapter(readonly_db):
    with pytest.raises(PermissionError):
        LibraryAdapter(readonly_db, writable=True)
    adapter = LibraryAdapter(readonly_db)
    for operation in [lambda: adapter.favorite(None),
                      lambda: adapter.create_collection(1, 'bad'),
                      lambda: adapter.add_members(1, [])]:
        with pytest.raises(PermissionError):
            operation()


def test_write_admission_stays_blocked():
    from frontend.admission import require_production_admission
    with pytest.raises(RuntimeError):
        require_production_admission(read_only=False)


def test_runtime_refuses_other_connections_and_io(tmp_path):
    path = tmp_path / 'allow.db'
    sqlite3.connect(path).close()
    code = '''
import sys,sqlite3,socket
from pathlib import Path
from frontend.readonly_runtime import install_readonly_runtime
p=Path(sys.argv[1]);install_readonly_runtime(p)
c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True);c.close()
blocked=0
for action in [lambda:sqlite3.connect(str(p)), lambda:sqlite3.connect(':memory:'),
               lambda:p.write_text('bad'), lambda:socket.getaddrinfo('example.com',443)]:
    try:action()
    except PermissionError:blocked+=1
assert blocked==4,blocked
print('4 capability escapes refused')
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, str(path)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
