"""Batch browser-save registration in the existing shared Add Mod flow."""
import json
import time
from secrets import token_urlsafe
from PySide6.QtCore import QObject, Slot, Qt
from services.importers.offline_admission import inspect_page,MissingNexusURL

ACTIONS={'offline-register','pick-offline-pages'}


def preview(bridge,args):
    bridge._command_game(args);bridge.adapter._write()
    r=bridge.resources
    if not r:raise PermissionError('需要明确的隔离资源')
    paths=args.get('paths')
    if not isinstance(paths,list) or not paths or len(paths)>200 or any(not isinstance(p,str) for p in paths):
        raise ValueError('请选择 1–200 个 HTML/MHTML 文件')
    supplied=args.get('source_urls',{})
    if not isinstance(supplied,dict) or any(k not in paths or not isinstance(v,str) or len(v)>2048 for k,v in supplied.items()):
        raise ValueError('补充来源链接必须对应本次选择的网页')
    rows=[];seen=set();expected={}
    from services.importers.duplicate_check import find_duplicate_mod
    with r.bind():
        for index,path in enumerate(dict.fromkeys(paths)):
            row={'id':str(index),'source':path,'file':path.replace('\\','/').split('/')[-1],'enabled':False,'state':'failed'}
            try:
                page=inspect_page(r,path,int(args['game']),source_url=supplied.get(path,''));duplicate=find_duplicate_mod(r.db,platform='Nexus',external_id=page['external_id'],source_url=page['url'],app_id=int(args['game']))
                repeated=page['url'] in seen;seen.add(page['url']);expected[str(index)]=page
                row.update(page,state='repeated' if repeated else 'existing' if duplicate else 'confirmation' if page['identity_confirmation']['required'] else 'new',enabled=True,
                           message='同来源有多个网页，请确认保留一个' if repeated else '已存在，提交仅跳过' if duplicate else '待确认归属 · 本体未提供' if page['identity_confirmation']['required'] else '可注册 · 本体未提供')
            except MissingNexusURL as exc:row.update(message=str(exc),can_supply_url=True)
            except Exception as exc:row['message']=str(exc)
            rows.append(row)
    token=token_urlsafe(24)
    bridge._offline_previews={token:{'game':str(args['game']),'snapshot':args['snapshot'],'created':time.monotonic(),'rows':rows,'expected':expected}}
    return {'preview':token,'rows':rows,'game':str(args['game'])}


def start(bridge,request_id,action,args):
    bridge._command_game(args);bridge.adapter._write();r=bridge.resources
    if not r:raise PermissionError('缺少隔离业务资源')
    if action=='pick-offline-pages':
        from PySide6.QtWidgets import QFileDialog,QApplication
        paths,_=QFileDialog.getOpenFileNames(QApplication.activeWindow(),'选择 Nexus 离线网页',str(r.path('incoming')),'HTML / MHTML (*.html *.htm *.mhtml *.mht)')
        bridge._command_game(args)
        for path in paths:
            r.select_input(path)
        bridge._busy.discard(args['_busy_key'])
        bridge.response.emit(json.dumps({'request':request_id,'result':{'paths':paths,'cancelled':not paths}},ensure_ascii=False));return
    draft=bridge._offline_previews.get(args.get('preview'));ids=args.get('ids')
    if not draft or draft['game']!=str(args['game']) or draft['snapshot']!=args['snapshot'] or time.monotonic()-draft['created']>600:
        raise ValueError('批量预览已过期，请重新解析')
    if not isinstance(ids,list) or not ids or len(ids)>200 or len(set(ids))!=len(ids) or any(not isinstance(i,str) for i in ids):raise ValueError('需要明确且不重复的页面选择')
    rows={row['id']:row for row in draft['rows']}
    if any(i not in rows or not rows[i]['enabled'] for i in ids):raise PermissionError('选择包含不允许注册的页面')
    confirmed=args.get('confirmed_ids',[])
    if not isinstance(confirmed,list) or any(i not in ids for i in confirmed):raise ValueError('来源确认必须属于本次选择')
    from services.importers.nexus_registration_identity import require_confirmation
    for i in ids:require_confirmation(rows[i]['identity_confirmation'],i in confirmed)
    urls=[draft['expected'][i]['url'] for i in ids]
    if len(set(urls))!=len(urls):raise ValueError('同一 Nexus 身份只能选择一个网页，请确认保留项')
    selected={draft['expected'][i]['source']:{**draft['expected'][i],'confirmed_identity':i in confirmed} for i in ids}
    def preflight():
        with bridge._lock:bridge._command_game(args)
    from ui.import_thread import ImportWorker
    worker=ImportWorker(platform='Nexus',library_root=r.path('mod'),resources=r,preflight=preflight,parent=bridge,
        params={'offline_html_paths':list(selected),'offline_expected':selected,'game_id':int(args['game']),
                'game_name':r.db.get_game(int(args['game'])).name,'offline_clean':True})
    job=_Batch(bridge,request_id,args,worker)
    worker.progress_changed.connect(job.progress,Qt.ConnectionType.QueuedConnection)
    worker.import_failed.connect(job.failure,Qt.ConnectionType.QueuedConnection)
    worker.finished.connect(job.finished,Qt.ConnectionType.QueuedConnection)
    bridge._offline_game=str(args['game']);bridge._jobs[request_id]=worker;bridge._acquisition[request_id]=job.row
    worker.start()


class _Batch(QObject):
    def __init__(self,bridge,request_id,args,worker):
        super().__init__(bridge);self.bridge=bridge;self.request_id=request_id;self.args=args;self.worker=worker;self.error=None
        self.row={'id':request_id,'game':str(args['game']),'game_name':bridge.adapter.db.get_game(int(args['game'])).name,
                  'action':'offline-register','name':'Nexus 离线网页批量注册','state':'running','message':'正在执行原生批量导入','items':[]}
    @Slot(str)
    def progress(self,message):self.row['message']=message
    @Slot(str)
    def failure(self,message):self.error=message
    @Slot()
    def finished(self):
        if not self.worker.wait(5000):
            from PySide6.QtCore import QTimer
            QTimer.singleShot(200,self.finished);return
        items=list(self.worker.batch_items)
        if self.error and any(i['state']=='success' for i in items):
            for item in items:
                if item['state']=='success':item.update(state='failed',message='Worker 最终校验失败，持久化可能已完成，请重新预览：'+self.error)
        successes=sum(i['state']=='success' for i in items);skipped=sum(i['state']=='skipped' for i in items);failed=sum(i['state']=='failed' for i in items)
        self.row.update(items=items,state='partial' if failed and successes+skipped else 'failed' if failed or self.error else 'success',
                        message=f'成功 {successes} · 跳过 {skipped} · 失败 {failed}'+(' · '+self.error if self.error and not items else ''))
        with self.bridge._lock:
            self.bridge._busy.discard(self.args['_busy_key']);self.bridge._offline_game=None;self.bridge._jobs.pop(self.request_id,None)
        self.bridge.response.emit(json.dumps({'request':self.request_id,'result':dict(self.row)},ensure_ascii=False))
        self.worker.deleteLater();self.deleteLater()
