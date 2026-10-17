"""Original mod.io HTTP parser/cover service under explicit business resources."""
import json,sqlite3,tempfile,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage
from frontend.library_adapter import LibraryAdapter
from frontend.web_bridge import LibraryBridge
from services.business_resources import BusinessResources
from services.modio_api import ModioClient
from tools.web_order_sandbox import initialize

@pytest.mark.parametrize('with_content,protect_user',[(False,False),(True,False),(True,True)])
def test_modio_real_http_cover_and_source_refresh_under_resource_guard(tmp_path,monkeypatch,with_content,protect_user):
    app=QApplication.instance() or QApplication([])
    initial=initialize(tmp_path/'resources');db=initial.db
    png=initial.path('incoming/logo.png');png.parent.mkdir(exist_ok=True)
    image=QImage(24,24,QImage.Format_RGB32);image.fill(0x447788);assert image.save(str(png),'PNG')
    state={'fail':False,'requests':[]}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state['requests'].append(self.path.split('?')[0])
            if state['fail']:
                self.send_response(503);self.end_headers();return
            if self.path.startswith('/logo.png'):
                data=png.read_bytes();kind='image/png'
            else:
                data=json.dumps({'id':99123,'game_id':991,'name':'Actual fixture title','name_id':'source-test','summary':'Source summary','description':'Real parsed source description','profile_url':'https://mod.io/g/fixture/m/source-test','logo':{'original':f'http://127.0.0.1:{self.server.server_port}/logo.png'},'submitted_by':{'username':'Source author'}}).encode();kind='application/json'
            self.send_response(200);self.send_header('Content-Type',kind);self.end_headers();self.wfile.write(data)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    endpoint=f'http://127.0.0.1:{server.server_port}'
    resources=BusinessResources(initial.root,db,steam_api_url=endpoint+'/steam/details')
    b=LibraryBridge(LibraryAdapter(db,writable=True),resources=resources)
    def client_factory():
        import requests
        session=requests.Session();session.trust_env=False
        return ModioClient(api_key='local-fixture-key',session=session,base_url=endpoint+'/v1',proxies={})
    monkeypatch.setattr('services.modio_metadata_refresh.ModioClient',client_factory)
    try:
        snapshot=b.execute('library',{'game':1142710});args={'game':'1142710','snapshot':snapshot['snapshot']}
        draft=b.execute('registration-preview',{**args,'kind':'url','platform':'mod.io','url':'https://mod.io/g/fixture/m/source-test','title':'Initial registered title'})
        created=b.execute_command('registration-save',{**args,'draft':draft['draft']});pk=created['mod_pk'];folder=Path(created['managed_path'])
        with resources.bind():
            meta=folder/'.info/metadata.json';data=json.loads(meta.read_text('utf8'));data.update(modio_game_id=991,modio_mod_id=99123);meta.write_text(json.dumps(data),'utf8')
            if with_content:(folder/'payload.txt').write_bytes(b'ORIGINAL PAYLOAD')
            if protect_user:
                db.update_mod_user_metadata(pk,{'display_name':'My display name','custom_description':'My description','user_notes':'My notes'})
                db.set_user_override_field(pk,'display_name',overridden=True)
                db.set_user_override_field(pk,'description',overridden=True)
                user_cover=resources.path('incoming/user.png');image.fill(0xbb8844);assert image.save(str(user_cover),'PNG')
                from services.importers.image_picker import apply_cover_to_mod
                apply_cover_to_mod(folder,user_cover,mod_id=pk)
                saved_user_cover=(folder/db.get_mod_display_info(pk).cover_path).read_bytes()

        def context():
            snap=b.execute('library',{'game':1142710});a=next(a for a in snap['assets'] if a['internal_id']==created['internal_id'])
            return {'game':'1142710','snapshot':snap['snapshot'],'internal_id':a['internal_id'],'token':a['token']}
        monkeypatch.setattr(tempfile,'tempdir',None)
        with pytest.raises(FileNotFoundError,match='No usable temporary directory'):
            with resources.bind() as denied_scope:tempfile.gettempdir()
        assert denied_scope.violations
        result=b.execute_command('refresh-metadata',context());assert result['success']
        row=db.get_mod_backup_row(pk);folder=Path(row['last_known_path']);info=db.get_mod_display_info(pk)
        assert info.steam_name=='Actual fixture title' and 'Real parsed' in info.steam_description
        assert info.cover_path and (folder/info.cover_path).is_file()
        assert '/v1/games/991/mods/99123' in state['requests']
        if protect_user:
            assert '/logo.png' not in state['requests']
            assert info.user_display_name=='My display name' and info.custom_description=='My description' and info.user_notes=='My notes'
            assert (folder/info.cover_path).read_bytes()==saved_user_cover
        else:assert '/logo.png' in state['requests']
        assert not resources.path('cache/modio_cover').exists() or not list(resources.path('cache/modio_cover').iterdir())
        if with_content:assert (folder/'payload.txt').read_bytes()==b'ORIGINAL PAYLOAD'
        with sqlite3.connect(db.db_path) as reopened:assert reopened.execute('select official_metadata_synced from mods where mod_id=?',(pk,)).fetchone()[0]==1
        # Retry a real failing HTTP response; preserve source fields and cover bytes.
        with resources.bind():db.set_official_metadata_synced(pk,False)
        cover=(folder/info.cover_path).read_bytes();before=info.steam_description;state['fail']=True
        with pytest.raises(RuntimeError,match='来源刷新未完成'):b.execute_command('refresh-metadata',context())
        assert db.get_mod_display_info(pk).steam_description==before
        assert (folder/info.cover_path).read_bytes()==cover
        assert not db.is_official_metadata_synced(pk)
    finally:b.shutdown();db.close();server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('platform,url',[('Nexus','https://www.nexusmods.com/test/mods/123'),('GitHub','https://github.com/test/source'),('Other','')])
def test_other_source_refresh_remains_local_only(tmp_path,monkeypatch,platform,url):
    app=QApplication.instance() or QApplication([])
    resources=initialize(tmp_path/'local-only');b=LibraryBridge(LibraryAdapter(resources.db,writable=True),resources=resources)
    monkeypatch.setattr('services.modio_metadata_refresh.ModioClient',lambda **kw:pytest.fail('Local-only source must not invoke mod.io'))
    try:
        snap=b.execute('library',{'game':1142710});args={'game':'1142710','snapshot':snap['snapshot']}
        draft=b.execute('registration-preview',{**args,'kind':'url','platform':platform,'url':url,'title':'Keep registered title'})
        result=b.execute_command('registration-save',{**args,'draft':draft['draft']})
        snap=b.execute('library',{'game':1142710});asset=next(a for a in snap['assets'] if a['internal_id']==result['internal_id'])
        args={'game':'1142710','snapshot':snap['snapshot'],'internal_id':asset['internal_id'],'token':asset['token']}
        before=resources.db.get_mod_display_info(result['mod_pk'])
        refreshed=b.execute_command('refresh-metadata',args)
        after=resources.db.get_mod_display_info(result['mod_pk'])
        assert refreshed['success'] and '本地' in refreshed['message']
        assert before.source_url==after.source_url and before.steam_name==after.steam_name
    finally:b.shutdown();resources.db.close()
