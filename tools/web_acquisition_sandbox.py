"""Real WebEngine import and Steam protocol fixture; never opens production data."""
import argparse
import json
import sys
import time
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--verify',action='store_true')
    parser.add_argument('--steam-public',action='store_true',help='Allow only the fixed public Steam metadata endpoint')
    parser.add_argument('--verify-public',action='store_true')
    args=parser.parse_args()
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer, QLockFile
    from services.mutation_context import activate_test_context, reset_mutation_context
    from services.business_resources import BusinessResources, checked_sandbox_root, checked_resource_path
    from tools.isolated_deploy_validation import initialize
    from frontend.library_adapter import LibraryAdapter
    from frontend.web_window import WebLibraryWindow
    from core.db_manager import DatabaseManager
    root=checked_sandbox_root(args.root)
    token=activate_test_context([root])
    app=QApplication(sys.argv[:1])
    marker=checked_resource_path(root,root/'.smm-acquisition-sandbox')
    if root.exists() and any(root.iterdir()):
        if args.verify or args.verify_public or not marker.is_file():raise PermissionError('验证需要全新目录；重开需要获取中心隔离标记')
        database=checked_resource_path(root,root/'data/mod_manager.db')
        resources=BusinessResources(root,DatabaseManager.open_existing(database,read_only=False))
    else:
        resources,_,_,_=initialize(root)
        marker.write_text('v1',encoding='utf8')
    lock=QLockFile(str(resources.path('data/app_instance.lock')))
    if not lock.tryLock(0):
        resources.db.close()
        reset_mutation_context(token)
        raise RuntimeError('隔离窗口已运行')
    source=resources.path('incoming/LocalWorkshopMod')
    if not source.exists():
        source.mkdir(parents=True)
        for index in range(700):
            (source/f'payload-{index:04}.txt').write_text('REAL LOCAL MOD CONTENT\n'*32,encoding='utf8')
    control=resources.path('source-response.json')
    if not control.exists():control.write_text(json.dumps({'mode':'success'}),encoding='utf8')
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body=self.rfile.read(int(self.headers.get('Content-Length',0))).decode()
            requests.append({'path':self.path,'body':body})
            mode=json.loads(control.read_text('utf8'))['mode']
            if mode=='timeout':
                time.sleep(3)
                self.send_response(504)
                self.end_headers()
                return
            time.sleep(.6)
            wid=parse_qs(body).get('publishedfileids[0]',[''])[0]
            payload={'response':{'publishedfiledetails':[{'publishedfileid':wid,'result':9 if mode=='missing' else 1,
                'consumer_app_id':43 if mode=='wrong-game' else 42,'title':'Official Source Updated Title',
                'description':'[h1]Source metadata[/h1]\nActual HTTP protocol response persisted by the existing Steam service.',
                'preview_url':'https://example.invalid/must-not-download.png','time_updated':1700000000,'tags':[]}]}}
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            self.wfile.write(b'{invalid-json' if mode=='malformed' else json.dumps(payload).encode())
        def log_message(self,*args):pass
    server=None
    if args.steam_public or args.verify_public:
        resources=BusinessResources(root,resources.db,steam_public=True)
        if args.verify_public:
            from tools.public_acquisition_validation import prepare_source
            prepare_source(resources)
    else:
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        resources=BusinessResources(root,resources.db,steam_api_url=f'http://127.0.0.1:{server.server_port}/steam/details')
    window=WebLibraryWindow(LibraryAdapter(resources.db,writable=True),resources=resources)
    window.show()
    if not (args.verify or args.verify_public):
        startup=QTimer(window)
        def ready(value):
            if value:
                startup.stop()
                game='1142710' if args.steam_public else '42'
                window.page.runJavaScript(f"$('game').value='{game}';load().then(()=>showSpace('acquire'));")
        startup.timeout.connect(lambda:window.page.runJavaScript("typeof S!=='undefined' && !!S.snapshot",ready))
        startup.start(250)
    code=0
    if args.verify or args.verify_public:
        def verify():
            nonlocal code
            try:
                if args.verify_public:
                    from tools.public_acquisition_validation import verify_window
                    verify_window(window,resources)
                else:
                    from tools.acquisition_validation import verify_window
                    verify_window(window,resources,source,control,requests)
            except Exception:
                import traceback
                traceback.print_exc()
                code=1
            app.quit()
        QTimer.singleShot(100,verify)
    try:app.exec()
    finally:
        while not window.bridge.shutdown():
            time.sleep(.05)
            app.processEvents()
        resources.path('http-requests.json').write_text(json.dumps(requests,indent=2),encoding='utf8')
        if server is not None:server.shutdown()
        resources.db.close()
        lock.unlock()
        reset_mutation_context(token)
    return code


if __name__=='__main__':raise SystemExit(main())
