"""Focused same-process Qt/ImportWorker diagnostic; no test lifecycle overrides."""
import argparse
import faulthandler
import json
import sys
import threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--log',required=True);parser.add_argument('--repeat',type=int,default=3);parser.add_argument('--cases',nargs='+');parser.add_argument('--native-stack',action='store_true');args=parser.parse_args()
    stream=open(args.log,'w',encoding='utf8',buffering=1)
    faulthandler.enable(file=stream,all_threads=True)
    from PySide6.QtCore import qInstallMessageHandler,QThread
    def record(kind,**values):
        stream.write(json.dumps({'kind':kind,'python_thread':threading.get_ident(),**values},ensure_ascii=False)+'\n');stream.flush()
    from PySide6.QtWidgets import QApplication
    native=None
    if args.native_stack:
        from tools.windows_native_trace import NativeTrace
        native=NativeTrace()
    def qt_message(kind,context,message):
        record('qt-message',severity=str(kind),message=message,application=id(QApplication.instance()) if QApplication.instance() else None)
        if native and ('QDxgi' in message or 'QThreadStorage' in message or str(kind)=='QtMsgType.QtFatalMsg'):
            record('native-stack',message=message,frames=native.capture())
    qInstallMessageHandler(qt_message)
    application_init=QApplication.__init__
    def init_application(application,*values,**kwargs):
        application_init(application,*values,**kwargs)
        identity=id(application);record('application-created',object_id=identity)
        application.destroyed.connect(lambda *_:record('application-destroyed',object_id=identity))
    QApplication.__init__=init_application
    from ui.import_thread import ImportWorker
    from services.mutation_context import current_mutation_context
    from ui.sync_thread import SyncWorker
    def instrument(worker_type):
        original=worker_type.run
        def run(worker):
            context=current_mutation_context();resources=worker.resources
            record('worker-start',worker_type=worker_type.__name__,object_id=id(worker),parent=type(worker.parent()).__name__,affinity_is_execution=worker.thread()==QThread.currentThread(),database=str(resources.db.db_path) if resources else None,mutation_mode=context.mode if context else None,mutation_roots=[str(p) for p in context.roots] if context else [])
            try:return original(worker)
            finally:record('worker-run-return',worker_type=worker_type.__name__,object_id=id(worker))
        worker_type.run=run
    instrument(ImportWorker);instrument(SyncWorker)
    from frontend.web_window import WebLibraryWindow
    window_init=WebLibraryWindow.__init__
    def init_window(window,*values,**kwargs):
        window_init(window,*values,**kwargs)
        for label in ('view','page','profile'):
            child=getattr(window,label);identity=id(child)
            record('web-object-created',label=label,object_id=identity,window=id(window),affinity_is_gui=child.thread()==QThread.currentThread(),parent=type(child.parent()).__name__)
            child.destroyed.connect(lambda *_,label=label,identity=identity:record('web-object-destroyed',label=label,object_id=identity))
    WebLibraryWindow.__init__=init_window
    import pytest
    cases=['tests/test_web_offline_batch.py','tests/test_web_workshop.py','tests/test_web_deploy.py','tests/test_web_business.py','tests/test_web_registration.py','tests/test_has_local_mod_payload.py','tests/test_missing_content_import_deploy.py','tests/test_web_acquisition.py']
    return pytest.main(['-v','--keep-duplicates',*((args.cases or cases)*args.repeat)])

if __name__=='__main__':raise SystemExit(main())
