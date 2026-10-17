"""Selected local Workshop sync admission; original SyncWorker/ModSyncService own writes."""
import hashlib
import json
import time
from pathlib import Path
from secrets import token_urlsafe
from PySide6.QtCore import QObject,Slot,Qt

ACTIONS={'workshop-sync','workshop-sync-all'}


def source_root(bridge,game):
    r=bridge.resources
    if r is None:raise PermissionError('Workshop 同步需要明确隔离资源')
    config=bridge.adapter.db.get_game_deploy_config(int(game))
    if not config or not config.workshop_path:raise ValueError('当前游戏没有配置 Workshop 目录')
    path=r.validate(config.workshop_path)
    if path.name=='content':path=r.validate(path/str(game))
    if path.name!=str(game) or path.parent.name!='content' or not path.is_dir():
        raise ValueError('需要当前 AppID 的 steamapps/workshop/content/AppID 目录')
    return path


def source_fact(r,path):
    from services.local_file_index import has_local_mod_payload
    paths=[]
    for p in path.rglob('*'):
        r.validate(p)
        if p.name.casefold() in ('.info','smm_info'):
            raise ValueError('Workshop 来源包含身份侧车，拒绝当作原始订阅内容')
        if p.is_file():paths.append((str(p.relative_to(path)),p.stat().st_size,p.stat().st_mtime_ns))
        if len(paths)>100000:raise ValueError('单项目资源超过当前扫描限额')
    if not has_local_mod_payload(path,db=r.db):raise ValueError('没有本地 Workshop 本体')
    return hashlib.sha256(json.dumps(paths,sort_keys=True).encode()).hexdigest(),len(paths)


def existing_policy(bridge,asset):
    from services.path_lifecycle import resolve_managed_folder
    from services.local_file_index import has_local_mod_payload
    r=bridge.resources;db=bridge.adapter.db
    raw=db._conn.execute('SELECT deploy_status FROM mods WHERE mod_id=?',(asset.mod_pk,)).fetchone()
    path=resolve_managed_folder(asset.internal_id,library_root=r.path('mod'),db=db).path
    if raw is None or raw[0]!='not_deployed':return 'protected','已部署或状态未确认，当前隔离准入禁止改写归档',path
    if path is not None:
        path=r.validate(path)
        if not path.is_relative_to(r.path('mod')):raise PermissionError('既有归档超出当前资源库')
        overrides=db.get_user_override_fields(asset.mod_pk)
        if overrides.get('cover') and asset.cover and Path(asset.cover).stem.lower() not in ('cover','preview'):
            return 'protected','用户封面路径尚未验证覆盖恢复，当前拒绝改写',path
        if (path/'.info/offline').exists() or (path/'.info/index.html').exists():
            return 'protected','已有离线资产尚未完成本批次覆盖恢复准入',path
        for p in path.rglob('*'):
            r.validate(p)
            if p.name=='deploy_manifest.json':return 'protected','归档具有部署 manifest，拒绝同步覆盖',path
    return ('existing','已存在，可按旧版时间戳规则检查更新',path) if path and has_local_mod_payload(path,mod_id=asset.mod_pk,db=db) else ('content_missing','已注册但内容缺失，可按原同步流程补齐',path)


def scan(bridge,args):
    bridge._command_game(args);bridge.adapter._write()
    r=bridge.resources
    if not r:raise PermissionError('缺少隔离资源')
    if bridge._workshop_game is not None:raise RuntimeError('已有 Workshop 同步任务，请等待完成后扫描')
    game=str(args['game'])
    with r.bind():
        root=source_root(bridge,game)
        from core.scanner import WorkshopScanner
        found=WorkshopScanner(root).scan(recursive=False)
        if len(found)>5000:raise ValueError('当前批次最多支持 5000 个 Workshop 项目')
        assets={a.workspace_id:a for a in bridge.adapter.assets(int(game)) if a.platform.lower()=='steam'}
        rows=[];facts={}
        for item in found:
            wid=item.published_file_id;asset=assets.get(wid);status='new';reason='尚未注册';name=asset.name if asset else 'Workshop '+wid
            try:
                if not wid.isascii() or not wid.isdigit() or int(wid)<=0 or len(wid)>24:raise ValueError('来源 ID 无效')
                digest,count=source_fact(r,item.path)
                if asset:status,reason,_=existing_policy(bridge,asset)
                facts[wid]=digest
                allowed=status!='protected'
            except Exception as exc:status='invalid';reason=str(exc);count=0;allowed=False
            rows.append({'id':wid,'name':name,'status':status,'reason':reason,'files':count,'enabled':allowed})
        present={item.published_file_id for item in found}
        for wid,asset in assets.items():
            if wid not in present:rows.append({'id':wid,'name':asset.name,'status':'source_missing','reason':'已注册；本地 Workshop 目录缺失，不能同步归档','files':0,'enabled':False})
        for child in root.iterdir():
            r.validate(child)
            if child.is_dir() and not child.name.isdigit():rows.append({'id':'','name':child.name,'status':'invalid','reason':'目录名不是 Workshop 来源 ID，不按名称注册或合并','files':0,'enabled':False})
        token=token_urlsafe(24)
        bridge._workshop_scans={token:{'game':game,'snapshot':args['snapshot'],'created':time.monotonic(),'root':root,'facts':facts,
                                     'rows':{row['id']:row for row in rows if row['id']}}}
        return {'game':game,'scan':token,'source':str(root),'rows':rows,
                'enabled':bool(r.steam_public or r.steam_api_url),'reason':'' if r.steam_public or r.steam_api_url else '未配置已准入的 Steam 元数据服务'}


def start(bridge,request_id,args):
    if args.get('_full_sync'):return start_all(bridge,request_id,args)
    from ui.sync_thread import SyncWorker
    from services.sync import SyncOptions
    bridge._command_game(args);bridge.adapter._write()
    r=bridge.resources
    scan=bridge._workshop_scans.get(args.get('scan'))
    ids=args.get('ids')
    if not scan or scan['game']!=str(args['game']) or scan['snapshot']!=args['snapshot'] or time.monotonic()-scan['created']>600:
        raise ValueError('Workshop 预览已过期，请重新扫描')
    if not isinstance(ids,list) or not ids or len(ids)>5000 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):raise ValueError('需要明确且不重复的 Workshop 成员选择')
    if any(i not in scan['rows'] or not scan['rows'][i]['enabled'] for i in ids):raise ValueError('选择包含未获准入的项目')
    if not (r.steam_public or r.steam_api_url):raise PermissionError('未配置已准入 Steam 元数据服务')
    game=str(args['game'])
    def preflight():
        with bridge._lock,r.bind():
            bridge._command_game(args)
            if source_root(bridge,game)!=scan['root']:raise ValueError('Workshop 配置已变化')
            assets={a.workspace_id:a for a in bridge.adapter.assets(int(game)) if a.platform.lower()=='steam'}
            for wid in ids:
                path=r.validate(scan['root']/wid)
                if path.exists():
                    digest,_=source_fact(r,path)
                    if digest!=scan['facts'][wid]:raise ValueError('Workshop 输入已变化，请重新扫描：'+wid)
                if wid in assets and existing_policy(bridge,assets[wid])[0]=='protected':raise PermissionError('既有对象已变为受保护状态：'+wid)
    options=SyncOptions(selected_ids=ids,app_id=int(game),recursive_scan=False,download_covers=False,archive_pages=False,proxy_url='',io_workers=2,net_workers=2)
    worker=SyncWorker(scan['root'],r.path('mod'),options,parent=bridge,resources=r,preflight=preflight)
    controller=_Job(bridge,request_id,args,ids,worker)
    worker.progress_changed.connect(controller.progress,Qt.ConnectionType.QueuedConnection)
    worker.sync_finished.connect(controller.result,Qt.ConnectionType.QueuedConnection)
    worker.sync_failed.connect(controller.failure,Qt.ConnectionType.QueuedConnection)
    worker.finished.connect(controller.finished,Qt.ConnectionType.QueuedConnection)
    bridge._workshop_game=game;bridge._jobs[request_id]=worker
    bridge._acquisition[request_id]=controller.job
    worker.start()


class _Job(QObject):
    def __init__(self,bridge,request_id,args,ids,worker):
        super().__init__(bridge)
        self.bridge,self.request_id,self.args,self.ids,self.worker=bridge,request_id,args,ids,worker
        self.value=None;self.error=None
        self.job={'id':request_id,'game':str(args['game']),'game_name':bridge.adapter.db.get_game(int(args['game'])).name,'action':'workshop-sync','name':f'Workshop · {len(ids)} 个项目','state':'running','message':'按原生同步流程注册与归档','items':[]}

    @Slot(int,str)
    def progress(self,percent,message):self.job.update(percent=percent,message=message)

    @Slot(object)
    def result(self,value):self.value=value

    @Slot(str)
    def failure(self,message):self.error=message

    @Slot()
    def finished(self):
        if not self.worker.wait(5000):
            self.job.update(state='failed',message='Worker 退出尚未完成，保持资源占用直到安全退出')
            from PySide6.QtCore import QTimer
            QTimer.singleShot(200,self.finished)
            return
        bridge=self.bridge
        try:payload=self._terminal()
        except Exception as exc:
            self.job.update(state='failed',message=str(exc))
            payload={'request':self.request_id,'error':str(exc)}
        with bridge._lock:
            bridge._busy.discard(self.args['_busy_key']);bridge._workshop_game=None;bridge._jobs.pop(self.request_id,None)
        bridge.response.emit(json.dumps(payload,ensure_ascii=False))
        self.worker.deleteLater();self.deleteLater()

    def _terminal(self):
        bridge=self.bridge
        if self.value is None:self.error=self.error or 'Worker 未返回业务结果'
        if self.error:
            self.job.update(state='failed',message=self.error)
            payload={'request':self.request_id,'error':self.error}
        else:
            if not self.ids:
                self.ids=list(dict.fromkeys(str(m.published_file_id) for m in self.value.success+self.value.updated+self.value.skipped+self.value.baselined+[m for m,e in self.value.failed if m is not None]))
            failed={str(meta.published_file_id):error for meta,error in self.value.failed if meta is not None}
            failed.update(dict(self.value.registration_failed))
            assets={a.workspace_id:a for a in bridge.adapter.assets(int(self.args['game'])) if a.platform.lower()=='steam'}
            updated={str(m.published_file_id) for m in self.value.updated}
            skipped={str(m.published_file_id) for m in self.value.skipped+self.value.baselined}
            items=[]
            with bridge.resources.bind():
                from services.local_file_index import has_local_mod_payload
                for wid in self.ids:
                    asset=assets.get(wid);error=failed.get(wid)
                    if not error and (not asset or not has_local_mod_payload(bridge.resources.validate(asset.managed_path),mod_id=asset.mod_pk,db=bridge.adapter.db)):
                        error='未形成已持久化的 Mod 身份与真实本体'
                    items.append({'id':wid,'name':asset.name if asset else 'Workshop '+wid,'state':'failed' if error else 'updated' if wid in updated else 'skipped' if wid in skipped else 'success','message':error or '身份与实际归档已重新读取确认','internal_id':asset.internal_id if asset and not error else None})
            failures=sum(i['state']=='failed' for i in items)
            self.job.update(items=items,state='failed' if failures and failures==len(items) else 'partial' if failures else 'success',message=f'新增 {sum(i["state"]=="success" for i in items)} · 更新 {sum(i["state"]=="updated" for i in items)} · 跳过 {sum(i["state"]=="skipped" for i in items)} · 失败 {failures}',percent=100)
            payload={'request':self.request_id,'result':dict(self.job)}
        return payload


def start_all(bridge,request_id,args):
    from ui.sync_thread import SyncWorker
    from services.sync import SyncOptions
    from PySide6.QtCore import QSettings
    from services.proxy_resolver import resolve_proxy
    bridge._command_game(args);bridge.adapter._write()
    r=bridge.resources;game=str(args['game'])
    if not r:raise PermissionError('缺少业务资源上下文')
    with r.bind():root=source_root(bridge,game)
    def preflight():
        with bridge._lock,r.bind():
            bridge._command_game(args)
            if source_root(bridge,game)!=root:raise RuntimeError('Workshop 配置已变化')
    settings=QSettings('SteamModManager','WorkshopLibrary')
    options=SyncOptions(full_sync=True,app_id=int(game),recursive_scan=False,download_covers=True,
        archive_pages=False,overwrite_files=False,skip_existing=True,
        proxy_url=resolve_proxy().url,steam_cookie=settings.value('network/steam_cookie','',str))
    worker=SyncWorker(root,r.path('mod'),options,parent=bridge,resources=r,preflight=preflight)
    controller=_Job(bridge,request_id,args,[],worker)
    controller.job.update(name='Steam 创意工坊 · 全部 Mod',message='发现、时间戳检查、注册与内容归档')
    worker.progress_changed.connect(controller.progress,Qt.ConnectionType.QueuedConnection)
    worker.sync_finished.connect(controller.result,Qt.ConnectionType.QueuedConnection)
    worker.sync_failed.connect(controller.failure,Qt.ConnectionType.QueuedConnection)
    worker.finished.connect(controller.finished,Qt.ConnectionType.QueuedConnection)
    bridge._workshop_game=game;bridge._jobs[request_id]=worker;bridge._acquisition[request_id]=controller.job
    worker.start()
