"""Real record DAL and rollback, canonical membership, context and readonly."""
import pytest
from PySide6.QtWidgets import QApplication
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from tools.web_order_sandbox import initialize
from services import deployment_record

@pytest.fixture
def fixture(tmp_path):
    app=QApplication.instance() or QApplication([])
    r=initialize(tmp_path/'snapshots')
    b=LibraryBridge(LibraryAdapter(r.db,writable=True),resources=r)
    snap=b.execute('library',{'game':1142710})
    args={'game':'1142710','snapshot':snap['snapshot']}
    yield app,r,b,args
    b.shutdown();r.db.close()

def query(b,args,**extra):return b.execute('deployment-snapshots',{**args,**extra})
def command(b,args,action,**extra):return b.execute_command(action,{**args,'version':query(b,args)['version'],**extra})

def test_crud_and_real_membership(fixture):
    _,r,b,a=fixture
    before=[tuple(x) for x in r.db._conn.execute('SELECT mod_id,deploy_status FROM mods')]
    files={p:p.read_bytes() for p in r.root.rglob('*') if p.is_file() and 'data' not in p.relative_to(r.root).parts}
    created=command(b,a,'snapshot-save',name='Two members')
    rid=created['record_id'];state=query(b,a,record_id=rid)
    assert len(state['comparison']['common'])==2
    assert not state['comparison']['snapshot_only'] and not state['comparison']['current_only']
    with pytest.raises(ValueError,match='覆盖'):command(b,a,'snapshot-save',name='Two members')
    old=state['version'];command(b,a,'snapshot-rename',record_id=rid,name='Renamed')
    with pytest.raises(RuntimeError,match='变化'):b.execute_command('snapshot-delete',{**a,'version':old,'record_id':rid})
    command(b,a,'snapshot-delete',record_id=rid)
    assert not query(b,a)['records']
    assert before==[tuple(x) for x in r.db._conn.execute('SELECT mod_id,deploy_status FROM mods')]
    assert all(p.read_bytes()==data for p,data in files.items())

def test_database_failure_rolls_back(fixture):
    _,r,b,a=fixture
    r.db._conn.execute("CREATE TEMP TRIGGER fail_snapshot BEFORE INSERT ON deployment_record_items BEGIN SELECT RAISE(ABORT,'snapshot injected failure'); END")
    with pytest.raises(Exception,match='injected'):command(b,a,'snapshot-save',name='Must rollback')
    assert not query(b,a)['records'] and not r.db._conn.in_transaction
    r.db._conn.execute('DROP TRIGGER fail_snapshot')
    rid=command(b,a,'snapshot-save',name='Keep members')['record_id']
    members=deployment_record.get_record_mod_ids(rid,db=r.db)
    r.db._conn.execute("CREATE TEMP TRIGGER fail_delete BEFORE DELETE ON deployment_records BEGIN SELECT RAISE(ABORT,'delete injected failure'); END")
    with pytest.raises(Exception,match='injected'):command(b,a,'snapshot-delete',record_id=rid)
    assert deployment_record.get_record_mod_ids(rid,db=r.db)==members
    assert r.db.get_deployment_record(rid) and not r.db._conn.in_transaction

def test_readonly_cross_game_and_empty(fixture):
    _,r,b,a=fixture
    rid=command(b,a,'snapshot-save',name='Protected')['record_id']
    other=b.execute('library',{'game':43});otherargs={'game':'43','snapshot':other['snapshot']}
    with pytest.raises(RuntimeError):query(b,a)
    with pytest.raises(ValueError):query(b,otherargs,record_id=rid)
    empty=command(b,otherargs,'snapshot-save',name='Empty')['record_id']
    assert query(b,otherargs,record_id=empty)['records'][0]['count']==0
    from core.db_manager import DatabaseManager
    db=DatabaseManager.open_existing(r.db.db_path,read_only=True)
    readonly=LibraryBridge(LibraryAdapter(db))
    snap=readonly.execute('library',{'game':1142710});args={'game':'1142710','snapshot':snap['snapshot']}
    assert not query(readonly,args)['enabled']
    for action in ('snapshot-save','snapshot-rename','snapshot-delete'):
        with pytest.raises(PermissionError):readonly.execute_command(action,args)
    readonly.shutdown();db.close()

def test_unresolved_historical_pk_is_visible(fixture):
    _,r,b,a=fixture
    rid=command(b,a,'snapshot-save',name='Historical')['record_id']
    r.db._conn.execute('PRAGMA foreign_keys=OFF')
    r.db._conn.execute('INSERT INTO deployment_record_items(record_id,mod_id) VALUES (?,999999)',(rid,));r.db._conn.commit()
    r.db._conn.execute('PRAGMA foreign_keys=ON')
    rows=query(b,a,record_id=rid)['comparison']['snapshot_only']
    assert len(rows)==1 and rows[0]['missing'] and rows[0]['mod_pk']=='999999'
