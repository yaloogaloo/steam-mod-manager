"""Continuous real UI operations through main.py, using existing approved fixtures."""
import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);args=parser.parse_args()
    from services.business_resources import checked_sandbox_root
    from services.mutation_context import activate_test_context,reset_mutation_context
    from tools.isolated_deploy_validation import initialize
    root=checked_sandbox_root(args.root);token=activate_test_context([root])
    resources,source,target,identity=initialize(root)
    incoming=root/'incoming/ReleaseImport';incoming.mkdir(parents=True)
    for index in range(20):(incoming/f'payload-{index}.txt').write_bytes(b'Actual import payload\n'*100)
    resources.db.close();reset_mutation_context(token)
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body=parse_qs(self.rfile.read(int(self.headers.get('Content-Length',0))).decode());wid=body['publishedfileids[0]'][0];requests.append(wid)
            payload={'response':{'publishedfiledetails':[{'publishedfileid':wid,'result':1,'consumer_app_id':42,'title':'Release source '+wid,'description':'[h1]Source result[/h1] Existing Steam protocol service persisted this response.','time_updated':1700000000,'tags':[]}]}}
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(payload).encode())
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    os.environ['SMM_VALIDATION_STEAM_API']=f'http://127.0.0.1:{server.server_port}/steam/details'
    from PySide6.QtCore import QTimer,qInstallMessageHandler
    from PySide6.QtTest import QTest
    from frontend.web_window import WebLibraryWindow
    report={'cycles':[],'qt':[]};code=0
    qInstallMessageHandler(lambda kind,context,message:report['qt'].append({'kind':str(kind),'message':message}))
    original=WebLibraryWindow.__init__
    def init(window,*values,**kwargs):
        original(window,*values,**kwargs)
        QTimer.singleShot(200,lambda:verify(window))
    WebLibraryWindow.__init__=init
    def verify(window):
        nonlocal code
        def js(text):
            values=[];window.page.runJavaScript(text,values.append)
            for _ in range(400):
                if values:return values[0]
                QTest.qWait(10);time.sleep(.005)
            raise TimeoutError(text)
        def wait(text):
            for _ in range(1200):
                if js(text):return
                QTest.qWait(15);time.sleep(.01)
            raise TimeoutError(text+' / '+str(js("$('acquisition-message').textContent+' '+$('registration-error').textContent")))
        try:
            wait("typeof S!=='undefined'&&!!S.snapshot")
            js("$('game').value='42';load()")
            wait("S.assets.length===1&&!$('refresh').disabled")
            for cycle in range(3):
                js('inspect(S.assets.find(a=>a.internal_id==='+json.dumps(identity.internal_id)+'))')
                wait('!!S.details&&S.current?.internal_id==='+json.dumps(identity.internal_id))
                js("document.querySelector('[data-command=edit]').click();$('edit-name').value='Release edit "+str(cycle)+"';$('edit-form').requestSubmit()")
                wait("!S.busy&&!$('editor').open&&S.current?.name==='Release edit "+str(cycle)+"'")
                for action in ('deploy','redeploy','undeploy'):
                    js("document.querySelector('[data-command="+action+"]').click()")
                    wait("!S.busy&&$('results').textContent==='操作成功'")
                    assert target.read_text('utf8')==('ORIGINAL' if action=='undeploy' else 'MOD-v1')
                js("$('acquisition-nav').click();$('acquisition-add').click();$('registration-kind').value='local';updateRegistrationFields()")
                wid=str(990810+cycle)
                js("$('import-source').value="+json.dumps(str(incoming))+";$('import-workshop').value="+json.dumps(wid)+";$('import-title').value='Release import "+str(cycle)+"';$('import-start').click()")
                wait('!registrationBusy&&!!registrationDraft')
                js("$('registration-confirm').click()")
                wait("!registrationBusy&&!$('registration-dialog').open")
                asset=next(a for a in window.bridge.adapter.assets(42) if a.workspace_id==wid)
                assert len(list(Path(asset.managed_path).glob('payload-*.txt')))==20
                js("$('acquisition-nav').click()")
                wait("!!acquisitionData?.candidates.some(a=>a.internal_id==="+json.dumps(asset.internal_id)+")")
                js("$('sync-mod').value=acquisitionData.candidates.find(a=>a.internal_id==="+json.dumps(asset.internal_id)+").token;$('sync-start').click()")
                wait('!acquisitionSubmitting')
                wait("!!acquisitionData?.tasks.some(t=>t.action==='source-sync'&&t.internal_id==="+json.dumps(asset.internal_id)+"&&t.state==='success')")
                assert window.bridge.adapter.db.is_official_metadata_synced(asset.mod_pk)
                report['cycles'].append({'edit':cycle,'import':asset.internal_id,'workspace_id':wid,'target':target.read_text('utf8'),'source_synced':True})
            window.grab().save(str(root/'main-continuous.png'))
            report['window']={'width':window.width(),'height':window.height(),'dpr':window.devicePixelRatioF()}
            report['success']=True
        except Exception:
            import traceback;traceback.print_exc();code=1
        finally:window.close()
    try:
        import main as entry
        result=entry.main(['--validation-root',str(root)])
        report['main_exit']=result;report['http_requests']=requests
        assert result==0 and report.get('success'),report
    finally:
        server.shutdown();server.server_close()
        (root/'main-runtime.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return code

if __name__=='__main__':raise SystemExit(main())
