"""Reversible real production UI acceptance; no deployment or payload mutation."""
import sys,json,time,sqlite3,traceback
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));sys.dont_write_bytecode=True
from PySide6.QtCore import QTimer,qInstallMessageHandler
from PySide6.QtTest import QTest
from frontend.web_window import WebLibraryWindow
out=Path('D:/smm-production-entry-evidence');out.mkdir(exist_ok=True)
stage=sys.argv[1];report={'stage':stage,'qt':[]};code=0
qInstallMessageHandler(lambda kind,ctx,msg:report['qt'].append(str(kind)+': '+msg))
original=WebLibraryWindow.__init__
def init(window,*args,**kwargs):
 original(window,*args,**kwargs);QTimer.singleShot(200,lambda:verify(window))
WebLibraryWindow.__init__=init
def verify(window):
 global code
 def js(script):
  values=[];window.page.runJavaScript(script,values.append)
  for _ in range(500):
   if values:return values[0]
   QTest.qWait(10);time.sleep(.005)
  raise TimeoutError(script)
 def wait(script):
  for _ in range(500):
   if js('!!('+script+')'):return
   QTest.qWait(20);time.sleep(.005)
  raise TimeoutError(script+' status='+str(js("$('results').textContent")))
 try:
  wait('typeof S!=="undefined"&&S.snapshot');js("$('game').value='916440';load()")
  wait("$('game').value==='916440'&&S.assets.length===104&&!$('refresh').disabled")
  db=window.bridge.adapter.db
  if stage=='write':
   asset=next(a for a in window.bridge.adapter.assets(916440) if not a.deployed)
   info=db.get_mod_display_info(asset.mod_pk)
   baseline={'id':asset.internal_id,'pk':asset.mod_pk,'favorite':bool(info.favorite),'display_name':info.user_display_name or '', 'custom_description':info.custom_description or '', 'user_notes':info.user_notes or '', 'tag':'SMM正式集成验证20261015','collection':'SMM正式集成验证20261015'}
   (out/'baseline.json').write_text(json.dumps(baseline,ensure_ascii=False),encoding='utf8')
  else:baseline=json.loads((out/'baseline.json').read_text('utf8'))
  iid=json.dumps(baseline['id']);js('inspect(S.assets.find(a=>a.internal_id==='+iid+'))');wait('S.details&&S.current?.internal_id==='+iid)
  if stage=='restore':
   info=db.get_mod_display_info(baseline['pk']);assert bool(info.favorite)!=baseline['favorite'];assert info.user_notes==baseline['user_notes']+'\nSMM正式集成验证20261015'
   assert baseline['tag'] in db.get_category_tags(baseline['pk'])
   assert any(c.name==baseline['collection'] for c in db.list_collections(916440))
   report['reopened_persistence']=True
  js("$('favorite-action').click()");wait('!S.busy&&S.details&&S.current.favorite==='+str(baseline['favorite'] if stage=='restore' else not baseline['favorite']).lower())
  js('openEditor()');wait("$('editor').open")
  notes=baseline['user_notes'] if stage=='restore' else baseline['user_notes']+'\nSMM正式集成验证20261015'
  js("$('edit-notes').value="+json.dumps(notes)+";$('edit-save').click()")
  wait("!$('editor').open&&!S.busy")
  assert db.get_mod_display_info(baseline['pk']).user_notes==notes
  js('openTags()');wait("$('organization').open&&!!O?.tags")
  if stage=='write':js("$('tag-query').value="+json.dumps(baseline['tag'])+";$('tag-add').click()")
  else:js('changeTag("tag-remove",'+json.dumps(baseline['tag'])+')')
  wait('!S.busy&&O.tags.tags.includes('+json.dumps(baseline['tag'])+')==='+str(stage=='write').lower())
  window.grab().save(str(out/(stage+'-tags.png')))
  js("$('organization-close').click();document.querySelector('[data-browse=collections]').click()")
  wait("!$('collection-tools').hidden")
  if stage=='write':
   js('openCollectionName()');wait("$('organization').open")
   js("$('collection-name').value="+json.dumps(baseline['collection'])+";$('organization-save').click()")
   wait("!$('organization').open&&!S.busy&&S.collections.some(c=>c.name==="+json.dumps(baseline['collection'])+")")
  else:
   js("$('collection').value=S.collections.find(c=>c.name==="+json.dumps(baseline['collection'])+").id;filter();$('collection-delete').click()")
   wait("$('organization').open");window.grab().save(str(out/'delete-confirm.png'));js("$('organization-save').click()")
   wait("!$('organization').open&&!S.busy&&!S.collections.some(c=>c.name==="+json.dumps(baseline['collection'])+")")
  window.grab().save(str(out/(stage+'-collections.png')))
  js("document.querySelector('[data-browse=all]').click();$('game-settings').click()")
  wait("$('game-config-dialog').open")
  window.grab().save(str(out/'game-configuration.png'))
  js("$('game-config-save').click()")
  wait("$('game-config-status').textContent==='游戏路径已保存'")
  js("$('game-config-close').click()")
  wait("!$('game-config-dialog').open");QTest.qWait(500)
  report.update(success=True,native_readonly=db._read_only,library=str(window.bridge.resources.path('mod')),width=window.width(),height=window.height(),dpr=window.devicePixelRatioF())
  window.grab().save(str(out/(stage+'-library.png')))
 except Exception:
  traceback.print_exc();report['error']=traceback.format_exc();code=1
 finally:window.close()
import main
report['exit']=main.main([])
(out/(stage+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps(report,ensure_ascii=True));raise SystemExit(code)
