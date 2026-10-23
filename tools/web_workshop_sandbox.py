"""Real local Workshop structure + real Steam-protocol HTTP fixture, original sync worker."""
import argparse
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def initialize(root):
    from tools.web_order_sandbox import initialize as previous
    from services.importers.steam import SteamImporter
    r=previous(root)
    with r.bind():
        source=r.path('games/steamapps/workshop/content/1142710')
        for wid in ('940001','940002','940003','940004'):
            (source/wid).mkdir();(source/wid/'payload.pack').write_bytes(('WORKSHOP CONTENT '+wid).encode())
        (source/'940099').mkdir();(source/'not-a-workshop-id').mkdir()
        stub=SteamImporter(db=r.db).import_mod(workshop_id='940010',title='Registered waiting for Workshop',library_root=r.path('mod'),game_name='Warhammer3',app_id=1142710)
        assert stub.success
        r.path('workshop-response.json').write_text(json.dumps({'failure':['940003'],'timestamps':{}}),encoding='utf8')
        r.path('.smm-workshop-sandbox').write_text('v1',encoding='utf8')
    return r


def source_server(resources):
    from services.business_resources import BusinessResources
    control=resources.path('workshop-response.json');requests=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            values=parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
            ids=[value[0] for key,value in sorted(values.items()) if key.startswith('publishedfileids[')]
            state=json.loads(control.read_text('utf8'))
            requests.append({'path':self.path,'ids':ids})
            time.sleep(.08)
            rows=[{'publishedfileid':wid,'result':9 if wid in state['failure'] else 1,'consumer_app_id':1142710,
                   'title':'Workshop Demo '+wid,'description':'[h1]Workshop metadata[/h1]Real HTTP fixture through the Steam provider.',
                   'time_updated':state['timestamps'].get(wid,1700000000),'tags':[]} for wid in ids]
            body=json.dumps({'response':{'publishedfiledetails':rows}}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
    return BusinessResources(resources.root,resources.db,steam_api_url=f'http://127.0.0.1:{server.server_port}/steam/details'),server,requests


def main():
    from tools.agent_workspace import require_workspace
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=require_workspace, required=True);parser.add_argument('--verify',action='store_true');args=parser.parse_args()
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QLockFile,QTimer
    from services.business_resources import checked_sandbox_root,BusinessResources
    from services.mutation_context import activate_test_context,reset_mutation_context
    from core.db_manager import DatabaseManager
    from frontend.library_adapter import LibraryAdapter
    from frontend.web_window import WebLibraryWindow
    root=checked_sandbox_root(args.root);token=activate_test_context([root]);app=QApplication(sys.argv[:1])
    if root.exists() and any(root.iterdir()):
        if args.verify or not (root/'.smm-workshop-sandbox').is_file():raise PermissionError('需要新的隔离验收目录或已有 Workshop 标记')
        r=BusinessResources(root,DatabaseManager.open_existing(root/'data/mod_manager.db',read_only=False))
    else:r=initialize(root)
    r,server,requests=source_server(r)
    lock=QLockFile(str(r.path('data/app_instance.lock')))
    if not lock.tryLock(0):raise RuntimeError('该隔离窗口正在运行')
    window=WebLibraryWindow(LibraryAdapter(r.db,writable=True),resources=r);window.setWindowTitle('SMM · Steam Workshop 原生同步 · 隔离验收');window.show();code=0
    if args.verify:
        def run():
            nonlocal code
            try:
                from tools.workshop_ui_validation import verify
                verify(window,r,requests)
            except Exception:
                import traceback;traceback.print_exc();code=1
            app.quit()
        QTimer.singleShot(100,run)
    else:
        timer=QTimer(window)
        def ready(value):
            if value:
                timer.stop();window.page.runJavaScript("$('game').value='1142710';load().then(()=>{showSpace('acquire');openRegistration();$('registration-kind').value='workshop';updateRegistrationFields();})")
        timer.timeout.connect(lambda:window.page.runJavaScript("typeof S!=='undefined' && !!S.snapshot",ready));timer.start(250)
    try:app.exec()
    finally:
        while not window.bridge.shutdown():app.processEvents();time.sleep(.05)
        r.db.close();lock.unlock();server.shutdown();server.server_close();reset_mutation_context(token)
    return code

if __name__=='__main__':raise SystemExit(main())
