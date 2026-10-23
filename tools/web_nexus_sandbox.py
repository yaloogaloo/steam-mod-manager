"""Nexus batch registration against copies of actual browser-saved pages."""
import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def initialize(root):
    from tools.web_order_sandbox import initialize as base
    from core.db_manager import GameInfo
    r=base(root);r.db.upsert_game(GameInfo(app_id=413150,name='Stardew Valley',folder_name='StardewValley'))
    incoming=r.path('incoming');incoming.mkdir(exist_ok=True)
    source=Path('D:/Temp/zspace/星露谷')
    pages=[next(source.rglob(pattern)) for pattern in ('*Piano*.mhtml','*Better-Butterfly-Hutch*.mhtml','*Weapons-on-Display*.mhtml')]
    origins=[]
    for index,page in enumerate(pages):
        origins.append({'path':str(page),'sha256':hashlib.sha256(page.read_bytes()).hexdigest()})
        if index<2:shutil.copy2(page,incoming/f'{index+1:02d}-{page.name}')
    # A genuine browser page converted to HTML, retaining its actual metadata and resources.
    from services.offline.mhtml import store_mhtml_snapshot
    converted=r.path('cache/fixture-html');index,_=store_mhtml_snapshot(pages[2],converted)
    (incoming/'03-Weapons-on-Display.html').write_text(index.read_text('utf8').replace('./assets/','./03-Weapons-on-Display_files/'),encoding='utf8')
    shutil.copytree(converted/'assets',incoming/'03-Weapons-on-Display_files')
    shutil.copy2(next(incoming.glob('01-*.mhtml')),incoming/'04-same-source.mhtml')
    (incoming/'05-invalid.html').write_text('<html><h1>No Nexus identity</h1></html>',encoding='utf8')
    (incoming/'06-wrong-game.html').write_text((incoming/'03-Weapons-on-Display.html').read_text('utf8').replace('stardewvalley/mods/','palworld/mods/').replace('03-Weapons-on-Display_files','06-wrong-game_files'),encoding='utf8')
    shutil.copytree(incoming/'03-Weapons-on-Display_files',incoming/'06-wrong-game_files')
    (incoming/'07-local-reference.html').write_text('<html><img src="file:///C:/Windows/win.ini"></html>',encoding='utf8')
    r.path('sample-origins.json').write_text(json.dumps(origins,ensure_ascii=False,indent=2),encoding='utf8')
    r.path('.smm-nexus-sandbox').write_text('isolated Nexus browser-save registration',encoding='utf8')
    return r


def main():
    from tools.agent_workspace import require_workspace
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=require_workspace, required=True);parser.add_argument('--verify',action='store_true');args=parser.parse_args()
    from services.business_resources import checked_sandbox_root,checked_resource_path,BusinessResources
    from services.mutation_context import activate_test_context,reset_mutation_context
    from core.db_manager import DatabaseManager
    from PySide6.QtCore import QTimer,QLockFile
    from PySide6.QtWidgets import QApplication
    from frontend.library_adapter import LibraryAdapter
    from frontend.web_window import WebLibraryWindow
    root=checked_sandbox_root(args.root);token=activate_test_context([root]);app=QApplication(sys.argv[:1])
    if args.verify:
        from PySide6.QtCore import Qt
        app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs,True)
    if root.exists() and any(root.iterdir()):
        if args.verify or not (root/'.smm-nexus-sandbox').is_file():raise PermissionError('需要新的隔离根或已有 Nexus 标记')
        r=BusinessResources(root,DatabaseManager.open_existing(checked_resource_path(root,root/'data/mod_manager.db'),read_only=False))
    else:r=initialize(root)
    lock=QLockFile(str(r.path('data/app_instance.lock')))
    if not lock.tryLock(0):raise RuntimeError('该隔离窗口已运行')
    window=WebLibraryWindow(LibraryAdapter(r.db,writable=True),resources=r);window.setWindowTitle('SMM · Nexus 批量离线网页注册 · 隔离验收');window.show();code=0
    timer=QTimer(window)
    def ready(value):
        if not value:return
        timer.stop()
        if args.verify:
            def run():
                nonlocal code
                try:
                    from tools.nexus_batch_validation import verify
                    verify(window,r)
                except Exception:
                    import traceback;traceback.print_exc();code=1
                window.close();app.quit()
            QTimer.singleShot(0,run)
        else:window.page.runJavaScript("$('game').value='413150';load().then(()=>{openRegistration();$('registration-kind').value='offline';updateRegistrationFields();})")
    timer.timeout.connect(lambda:window.page.runJavaScript("typeof S!=='undefined'&&!!S.snapshot",ready));timer.start(200)
    try:app.exec()
    finally:
        while not window.bridge.shutdown():app.processEvents();time.sleep(.02)
        r.db.close();lock.unlock();reset_mutation_context(token)
    return code

if __name__=='__main__':raise SystemExit(main())
