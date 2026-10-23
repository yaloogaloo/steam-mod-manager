"""Read-only production path/admission inspection through the actual application."""
import sys,json,traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));sys.dont_write_bytecode=True
from PySide6.QtCore import QTimer
from frontend.web_window import WebLibraryWindow
out=Path(__file__).resolve().parents[1] / '_tmp/agent_runs/production-entry'
out.mkdir(parents=True,exist_ok=True)
report={};original=WebLibraryWindow.__init__
def init(window,*a,**kw):
 original(window,*a,**kw);QTimer.singleShot(500,lambda:check(window))
WebLibraryWindow.__init__=init
def check(window):
 try:
  r=window.bridge.resources
  from core.db_manager import get_db
  from services.mutation_context import current_mutation_context
  with r.bind():assert get_db() is r.db
  report.update(database=str(r.db.db_path),library=str(r.path('mod')),context=current_mutation_context().mode,games=[])
  for g in r.db.list_games():
   a=window.bridge.adapter.assets(g.app_id);config=r.db.get_game_deploy_config(g.app_id)
   samples=[]
   for flag in (False,True):
    sample=next((v for v in a if v.deployed==flag and not v.status),None)
    if sample:
     policy=window.bridge.inspector_actions(sample,'');samples.append({'id':sample.internal_id,'deployed':sample.deployed,'primary':policy['primary'],'undeploy':policy['undeploy']})
   report['games'].append({'game':g.app_id,'count':len(a),'samples':samples})
  if (c:=r.db.get_game_deploy_config(1142710)) and c.install_path:
   report['wh3_projection']=str(r.validate_write(Path(c.install_path)/'used_mods.txt'))
   report['wh3_order']=str(r.validate_write(r.path('config/load_order/wh3.json')))
  report['success']=True
 except Exception:report['error']=traceback.format_exc()
 finally:window.close()
import main
report['exit']=main.main([])
(out / 'production-paths.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps({'success':report.get('success'), 'error':report.get('error'),'exit':report['exit']}));raise SystemExit(0 if report.get('success') else 1)
