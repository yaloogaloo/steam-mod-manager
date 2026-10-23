"""Interactive tags/collections window using the existing isolated context."""
import argparse
import sys
import time
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser()
    from tools.agent_workspace import require_workspace
    parser.add_argument('--root',type=require_workspace, required=True)
    parser.add_argument('--init',action='store_true')
    parser.add_argument('--verify',action='store_true')
    parser.add_argument('--failure',choices=('tag-db','cover-cleanup'))
    args=parser.parse_args()
    if args.verify and not args.init:parser.error('--verify requires a new --init root')
    if args.failure and not args.verify:parser.error('--failure requires --verify')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QLockFile, QTimer
    from core.db_manager import DatabaseManager
    from services.business_resources import BusinessResources, checked_sandbox_root, checked_resource_path
    from services.mutation_context import activate_test_context, reset_mutation_context
    from frontend.library_adapter import LibraryAdapter
    from frontend.web_window import WebLibraryWindow
    from tools.organization_validation import initialize, verify_window
    root=checked_sandbox_root(args.root)
    token=activate_test_context([root])
    app=QApplication(sys.argv[:1])
    window=db=lock=None
    exit_code=0
    try:
        if args.init:
            resources,_=initialize(root)
            db=resources.db
        else:
            marker=checked_resource_path(root,root/'.smm-organization-sandbox')
            if not marker.is_file():raise PermissionError('缺少标签与合集隔离标记')
            database=checked_resource_path(root,root/'data/mod_manager.db')
            db=DatabaseManager.open_existing(database,read_only=False)
            resources=BusinessResources(root,db)
        lock=QLockFile(str(root/'data/app_instance.lock'))
        if not lock.tryLock(0):raise RuntimeError('隔离窗口已运行')
        window=WebLibraryWindow(LibraryAdapter(db,writable=True),resources=resources)
        window.show()
        if not args.verify:
            startup=QTimer(window)
            def selected(ready):
                if ready:
                    startup.stop()
                    window.page.runJavaScript("$('game').value='1142710';load();")
            startup.timeout.connect(lambda:window.page.runJavaScript(
                "typeof S!=='undefined' && !!S.snapshot && !!document.querySelector('#game option[value=\"1142710\"]')",selected))
            startup.start(250)
        if args.verify:
            def verify():
                nonlocal exit_code
                try:verify_window(window,resources,args.failure)
                except Exception:
                    import traceback
                    traceback.print_exc()
                    exit_code=1
                app.quit()
            QTimer.singleShot(100,verify)
        app.exec()
        return exit_code
    finally:
        while window is not None and not window.bridge.shutdown():
            time.sleep(.05)
            app.processEvents()
        if db is not None:db.close()
        if lock is not None:lock.unlock()
        reset_mutation_context(token)


if __name__=='__main__':raise SystemExit(main())
