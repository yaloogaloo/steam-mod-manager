"""Original Backend projection through production path composition on temporary data."""
import json
import sqlite3
import pytest
from PySide6.QtWidgets import QApplication
from core.db_manager import DatabaseManager
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from services.production_resources import ProductionResources
from tests.test_stellaris_activation import _configure_stellaris,_seed_stellaris_mod,_write_ugc_descriptor


class TemporaryProductionPaths(ProductionResources):
    def check_context(self,context):
        assert context.mode=='test'
        assert any(self.root.is_relative_to(root) and self.library.is_relative_to(root) for root in context.roots)


@pytest.mark.parametrize('game,order_file',[(281990,'stellaris.json'),(1158310,'ck3.json')])
def test_original_paradox_order_json_launcher_and_backup(tmp_path,monkeypatch,game,order_file):
    app=QApplication.instance() or QApplication([])
    root=tmp_path/'application';(root/'data').mkdir(parents=True)
    db=DatabaseManager(root/'data/mod_manager.db');library=tmp_path/'library';library.mkdir()
    docs,workshop=_configure_stellaris(db,tmp_path)
    if game!=281990:
        db.update_game_deploy_config(game,name='Crusader Kings III',install_path=str(tmp_path/'CK3Install'),mod_path=str(docs),workshop_path=str(workshop))
    def seed(wid,deployed):
        if game==281990:return _seed_stellaris_mod(library,db,folder='Test '+wid,workshop_id=wid,deployed=deployed)
        from services.identity_service import create_mod_identity,identity_create_scope
        from tests.helpers.identity import write_info_sidecar
        with identity_create_scope():identity=create_mod_identity(db,platform='Steam',external_id=wid,workshop_id=wid,title='Test '+wid,app_id=game,game_name='Crusader Kings III',operation='import')
        folder=library/'CK3'/wid;folder.mkdir(parents=True)
        write_info_sidecar(folder,internal_id=identity.internal_id,title='Test '+wid,external_id=wid,workspace_id=identity.workspace_id,app_id=game,game_name='Crusader Kings III')
        db.update_mod_identity_fields(identity.mod_id,last_known_path=str(folder),folder_present=True)
        if deployed:db.update_mod_deploy_status(identity.mod_id,deploy_status='deployed',deploy_path=str(folder),app_id=game)
    for wid in ('770001','770002'):
        seed(wid,True)
        _write_ugc_descriptor(docs,wid)
    seed('770003',False)
    r=TemporaryProductionPaths(root,db,library);bridge=LibraryBridge(LibraryAdapter(db,writable=True),resources=r)
    try:
        snapshot=bridge.execute('library',{'game':game});args={'game':str(game),'snapshot':snapshot['snapshot']}
        with r.bind(),r.game_projection(game):
            with pytest.raises(PermissionError):r.validate(docs/'unrelated-private.txt')
        state=bridge.execute('deployment',args)
        assert state['enabled'] and len(state['rows'])==2
        desired=state['order'][::-1]
        result=bridge.execute_command('order-save',{**args,'version':state['version'],'order':desired})
        assert result['success']
        assert bridge.execute('deployment',args)['order']==desired
        order=json.loads((root/'config/load_order'/order_file).read_text('utf8'))
        assert (order['order'] if isinstance(order,dict) else order)==desired
        text=json.loads((docs/'dlc_load.json').read_text('utf8'))
        wanted=[f'mod/ugc_{db.get_mod_display_info(db.find_mod_by_internal_id(iid)).workspace_id}.mod' for iid in desired]
        assert text['enabled_mods']==wanted and text['disabled_dlcs']==['fake_dlc']
        assert list((root/'data/projection_recovery'/str(game)).glob('*/launcher-v2.sqlite'))
        with pytest.raises(PermissionError):
            with r.bind():sqlite3.connect(docs/'launcher-v2.sqlite')
        before=bridge.execute('deployment',args)
        from services import paradox_activation
        monkeypatch.setattr(paradox_activation,'_write_json_object',lambda *a:(_ for _ in ()).throw(OSError('controlled projection failure')))
        with pytest.raises(RuntimeError,match='投影失败'):
            bridge.execute_command('order-save',{**args,'version':before['version'],'order':desired[::-1]})
        assert bridge.execute('deployment',args)['retry_projection']
    finally:bridge.shutdown();db.close()


def test_real_paradox_deploy_worker_reports_partial_projection_failure(tmp_path,monkeypatch):
    from ui.deploy_thread import DeployWorker
    from services.deploy import ModDeployer
    from services import paradox_activation
    app=QApplication.instance() or QApplication([])
    root=tmp_path/'application';(root/'data').mkdir(parents=True)
    db=DatabaseManager(root/'data/mod_manager.db');library=tmp_path/'library';library.mkdir()
    docs,workshop=_configure_stellaris(db,tmp_path)
    pk=_seed_stellaris_mod(library,db,folder='Actual activation',workshop_id='770004',deployed=False)
    _write_ugc_descriptor(docs,'770004');(workshop/'770004').mkdir()
    iid=db.get_mod_backup_row(pk)['internal_id']
    resources=TemporaryProductionPaths(root,db,library)
    try:
        deployer=ModDeployer(library_root=library,db=db,resources=resources)
        def run(action):
            result=[]
            worker=DeployWorker(iid,library,action=action,deployer=deployer,resources=resources)
            worker.deploy_finished.connect(result.append);worker.deploy_failed.connect(result.append)
            worker.run()
            assert len(result)==1
            return result[0]
        deployed=run('deploy');assert deployed['success'],deployed.get('error')
        assert db.get_mod_deploy_info(pk).deploy_status=='deployed'
        assert json.loads((docs/'dlc_load.json').read_text('utf8'))['enabled_mods']==['mod/ugc_770004.mod']
        assert run('redeploy')['success']
        assert run('undeploy')['success']
        assert db.get_mod_deploy_info(pk).deploy_status=='not_deployed'
        monkeypatch.setattr(paradox_activation,'_write_json_object',lambda *a:(_ for _ in ()).throw(OSError('controlled projection failure')))
        failure=run('deploy')
        assert not failure['success'] and failure['error_code']=='projection_failed'
        assert db.get_mod_deploy_info(pk).deploy_status=='deployed'
        assert '状态已保存' in failure['error']
    finally:db.close()
