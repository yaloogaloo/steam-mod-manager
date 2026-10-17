"""Real LSPK metadata and original BG3 projector with temporary production paths."""
import json
import pytest
from PySide6.QtWidgets import QApplication
from core.db_manager import DatabaseManager
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from services.bg3_modsettings import parse_bg3_modsettings,GUSTAVX_UUID
from tests.test_web_paradox_projection import TemporaryProductionPaths
from tests.test_bg3_sort_stage2 import _configure_game,_seed_bg3_mod,_base_lsx,UUID_A,UUID_B,UUID_C,UUID_UNMANAGED


def test_bg3_real_pak_projection_and_missing_canonical_member(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    root=tmp_path/'application';(root/'data').mkdir(parents=True)
    db=DatabaseManager(root/'data/mod_manager.db')
    library=_configure_game(db,tmp_path)
    db.update_game_deploy_config(1086940,mod_path=str(tmp_path/'game_mods'))
    _,first,_=_seed_bg3_mod(library,db,tmp_path,folder='ModA',uuid=UUID_A,name='A')
    _,second,_=_seed_bg3_mod(library,db,tmp_path,folder='ModB',uuid=UUID_B,name='B')
    _,missing,_=_seed_bg3_mod(library,db,tmp_path,folder='Missing',uuid=UUID_C,name='Missing',write_pak=False)
    profile=tmp_path/'Profile/Public';profile.mkdir(parents=True)
    live=profile/'modsettings.lsx';live.write_text(_base_lsx(),encoding='utf8')
    monkeypatch.setenv('SMM_BG3_MODSETTINGS',str(live))
    order=root/'config/load_order/bg3.json';monkeypatch.setenv('SMM_BG3_ORDER',str(order))
    r=TemporaryProductionPaths(root,db,library);b=LibraryBridge(LibraryAdapter(db,writable=True),resources=r)
    try:
        snapshot=b.execute('library',{'game':1086940});args={'game':'1086940','snapshot':snapshot['snapshot']}
        state=b.execute('deployment',args)
        assert state['enabled'] and set(state['order'])=={first,second,missing}
        assert 'MissingPak' in next(row['warning'] for row in state['rows'] if row['internal_id']==missing)
        desired=[second,missing,first]
        result=b.execute_command('order-save',{**args,'version':state['version'],'order':desired})
        assert result['success'] and json.loads(order.read_text('utf8'))['order']==desired
        _,nodes=parse_bg3_modsettings(live.read_text('utf8'))
        uuids=[node.uuid for node in nodes]
        assert uuids[0]==GUSTAVX_UUID and uuids.index(UUID_B)<uuids.index(UUID_A)
        assert uuids.count(UUID_UNMANAGED)==2
        assert b.execute('deployment',args)['order']==desired
        assert list((root/'data/projection_recovery/1086940').glob('*/modsettings.lsx'))
        before=b.execute('deployment',args)
        from services import bg3_activation
        monkeypatch.setattr(bg3_activation,'project_bg3_modsettings',lambda *a,**k:(_ for _ in ()).throw(OSError('controlled LSX projection failure')))
        with pytest.raises(RuntimeError,match='投影失败'):b.execute_command('order-save',{**args,'version':before['version'],'order':desired[::-1]})
        assert b.execute('deployment',args)['retry_projection']
    finally:b.shutdown();db.close()
