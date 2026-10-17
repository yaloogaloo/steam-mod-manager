"""Bounded real-data launch through main.py --read-only, no production mutations."""
import json
import sys
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QTimer
from frontend.web_window import WebLibraryWindow

report={};original=WebLibraryWindow.__init__
def initialize(window,*args,**kwargs):
    original(window,*args,**kwargs)
    timer=QTimer(window);step=[0]
    def ready(value):
        if not value:return
        if step[0]==0:
            step[0]=1;window.page.runJavaScript("$('game').value='916440';load()")
        else:
            timer.stop()
            from services.mutation_context import current_mutation_context
            replies=[];window.bridge.response.connect(lambda raw:replies.append(json.loads(raw)) if json.loads(raw).get('request')=='readonly-proof' else None)
            window.bridge.command('readonly-proof','favorite-set','{}')
            report.update(native_readonly=window.bridge.adapter.db._read_only,mutation_context=str(current_mutation_context()),readonly_response=replies,
                          width=window.width(),height=window.height(),dpr=window.devicePixelRatioF())
            window.grab().save('D:/smm-release-20261015/main-readonly.png')
            window.close()
    timer.timeout.connect(lambda:window.page.runJavaScript("typeof S!=='undefined'&&!!S.snapshot" if step[0]==0 else "S.assets.length===104&&!$('refresh').disabled",ready))
    timer.start(200)
WebLibraryWindow.__init__=initialize
import main
report['main_exit']=main.main(['--read-only'])
print(json.dumps(report,ensure_ascii=True))
