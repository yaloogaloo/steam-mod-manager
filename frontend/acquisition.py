"""Restricted local-folder import and one-shot Steam metadata tasks."""
import json
from uuid import UUID

ACTIONS = {'import-folder', 'import-zip', 'source-sync', 'pick-import-folder', 'pick-import-zip','pick-import-cover','pick-import-offline'}


def validate_import(bridge, args):
    from core.mod_platform import normalize_platform,get_available_sources
    resources=bridge.resources
    platform=normalize_platform(args.get('platform','Steam'))
    game=bridge.adapter.db.get_game(int(args['game']))
    if platform not in {p for p,label in get_available_sources(game.name,int(args['game']))}:
        raise ValueError('该游戏不支持所选来源')
    source=resources.validate(args.get('source',''))
    zipped=args.get('_zip',False);batch=bool(args.get('batch_directory'))
    if batch and platform=='steam':raise ValueError('Steam 批量身份请使用 Workshop 发现与同步；普通目录名称不能当作 Workshop ID')
    if not resources.input_allowed(source) or not (source.is_file() if zipped else source.is_dir()):
        raise ValueError('请选择当前隔离环境 incoming 中的 Mod 目录')
    if source == resources.path('incoming'):
        raise ValueError('请选择 incoming 内的具体 Mod 目录')
    files=[]
    if zipped:
        if platform=='steam':validate_zip(source)
        else:
            from services.importers.archive import is_archive_path
            if not is_archive_path(str(source)):raise ValueError('请选择旧版支持的 ZIP / 7z / RAR 压缩包')
        files.append(source)
        paths=args.get('archive_paths')
        if paths is not None:
            if not isinstance(paths,list) or not paths or len(paths)>500 or not all(isinstance(p,str) for p in paths):raise ValueError('压缩包列表无效')
            checked=[resources.validate(p) for p in paths]
            if checked[0]!=source or len(set(checked))!=len(checked):raise ValueError('压缩包输入不一致或重复')
            for path in checked[1:]:
                if not path.is_file() or not resources.input_allowed(path):raise ValueError('压缩包未经选择器授权')
                if platform=='steam':validate_zip(path)
                else:
                    from services.importers.archive import is_archive_path
                    if not is_archive_path(str(path)):raise ValueError('不支持的压缩包格式')
    for path in (() if zipped else source.rglob('*')):
        resources.validate(path)
        if not batch and (path.name.casefold() in ('.info','smm_info') or (platform=='steam' and path.suffix.lower() in ('.zip','.7z','.rar','.mhtml','.html','.mht','.htm'))):
            raise ValueError('当前仅准入原始 Mod 目录；压缩包、离线页面及已注册归档重新导入尚未准入')
        if path.is_file():files.append(path)
    if not files:raise ValueError('Mod 目录没有文件')
    if platform=='steam' and not batch:
        wid=args.get('workshop_id')
        if not isinstance(wid,str) or not wid.isascii() or not wid.isdigit() or len(wid)>24 or int(wid)==0:
            raise ValueError('请输入有效 Steam Workshop 数字 ID')
        from services.identity_service import has_official_platform_identity
        if not has_official_platform_identity(platform='Steam',workshop_id=wid):
            raise ValueError('该标识不符合旧版 Steam 来源身份合同')
    else:
        from services.importers.identity_resolve import resolve_import_identity,ImportIdentity
        url=str(args.get('url') or '').strip()
        identity=resolve_import_identity(platform,github_url=url if platform=='github' else '',nexus_url=url if platform=='nexus' else '',modio_url=url if platform=='modio' else '',source_url=url,allow_local_fallback=batch or platform in ('nexus','other'))
        if not isinstance(identity,ImportIdentity):raise ValueError(getattr(identity,'error','来源身份无效'))
    title=args.get('title')
    if not isinstance(title,str) or len(title)>200 or any(ord(c)<32 for c in title):
        raise ValueError('Mod 名称最多 200 字符，不得包含控制字符')
    for key in ('cover_source','offline_html_path'):
        value=args.get(key)
        if not value:continue
        if batch:raise ValueError('批量目录使用各自侧车，不接受共享封面或网页')
        path=resources.validate(value)
        if not path.is_file() or not resources.input_allowed(path):raise ValueError('附加资源未经选择器授权')
        if key=='cover_source':
            from services.importers.image_picker import validate_cover_image
            validate_cover_image(path)
        else:
            if platform!='nexus':raise ValueError('手动附加离线页面仅适用于 Nexus')
            from services.importers.offline_admission import inspect_page
            page=inspect_page(resources,path,int(args['game']))
            if args.get('url'):
                from services.importers.nexus import parse_nexus_id
                if page['external_id']!=parse_nexus_id(args['url']):raise ValueError('附加网页与填写的 Nexus 来源身份不一致')
    return source


def validate_zip(source):
    """Admission only; extraction and materialization remain ArchiveImporter-owned."""
    import zipfile
    import stat
    from pathlib import PurePosixPath, PureWindowsPath
    if source.suffix.lower()!='.zip' or source.stat().st_size>64*1024*1024:
        raise ValueError('仅准入不超过 64 MiB 的 ZIP')
    with zipfile.ZipFile(source) as archive:
        entries=archive.infolist()
        if not entries or len(entries)>4096 or sum(e.file_size for e in entries)>256*1024*1024:
            raise ValueError('ZIP 为空或解包内容超过限制')
        names=set()
        for entry in entries:
            name=entry.filename.replace('\\','/')
            parts=PurePosixPath(name).parts
            reserved={'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}
            if (not parts or name.startswith('/') or PureWindowsPath(name).drive or '..' in parts
                or any(':' in p or p.rstrip(' .')!=p or p.casefold() in ('.info','smm_info') or p.split('.')[0].upper() in reserved for p in parts)
                or stat.S_ISLNK(entry.external_attr>>16) or entry.flag_bits & 1):
                raise ValueError('不安全的 ZIP 路径或文件类型：'+name)
            key='/'.join(parts).casefold()
            if key in names:raise ValueError('ZIP 存在重名路径：'+name)
            names.add(key)
            if entry.file_size>128*1024*1024 or entry.file_size>max(entry.compress_size,1)*1000:
                raise ValueError('ZIP 文件体积或压缩比例超过限制')
        if not any(not e.is_dir() and e.file_size for e in entries):raise ValueError('ZIP 没有内容文件')


def start(bridge, request_id, action, args):
    resources=bridge.resources
    bridge._command_game(args)
    if resources is None:raise PermissionError('获取与同步需要明确的隔离业务上下文')
    if action.startswith('pick-import-'):
        from PySide6.QtWidgets import QApplication, QFileDialog
        from services.importers.import_settings import get_last_import_directory,set_last_import_directory
        start_directory=get_last_import_directory(fallback=resources.path('incoming')) if getattr(resources,'production',False) else str(resources.path('incoming'))
        if action in ('pick-import-cover','pick-import-offline'):
            selected,_=QFileDialog.getOpenFileName(QApplication.activeWindow(),'选择导入附加资源',start_directory,'Images (*.png *.jpg *.jpeg *.webp)' if action=='pick-import-cover' else 'HTML / MHTML (*.html *.htm *.mhtml *.mht)')
        elif action=='pick-import-zip':
            filter='Archives (*.zip *.7z *.rar)' if args.get('platform','Steam')!='Steam' else 'ZIP (*.zip)'
            if args.get('multiple'):
                selections,_=QFileDialog.getOpenFileNames(QApplication.activeWindow(),'选择同一 Mod 的压缩包',start_directory,filter)
                for choice in selections:resources.select_input(choice)
                selected=selections[0] if selections else ''
            else:selected,_=QFileDialog.getOpenFileName(QApplication.activeWindow(),'选择 Mod 压缩包',start_directory,filter)
        else:
            selected=QFileDialog.getExistingDirectory(QApplication.activeWindow(),'选择 Mod 目录',start_directory)
        if selected:
            path=resources.select_input(selected)
            if getattr(resources,'production',False):set_last_import_directory(selected)
        bridge._busy.discard(args['_busy_key'])
        bridge.response.emit(json.dumps({'request':request_id,'result':{'source':selected,'sources':selections if action=='pick-import-zip' and args.get('multiple') else [selected] if selected else [],'cancelled':not bool(selected),'game':str(args['game'])}}))
        return
    game=str(args['game'])
    game_info=bridge.adapter.db.get_game(int(game))
    importing=action in ('import-folder','import-zip')
    if importing:
        from ui.import_thread import ImportWorker
        args['_zip']=action=='import-zip'
        from .registration import confirm_local
        confirm_local(bridge,args)
        source=validate_import(bridge,args)
        def preflight():
            with bridge._lock:
                bridge._command_game(args)
                validate_import(bridge,args)
                confirm_local(bridge,args)
        platform=args.get('platform','Steam');url=str(args.get('url') or '').strip()
        worker=ImportWorker(platform=platform,library_root=resources.path('mod'),parent=bridge,
            params={'source_path':str(source),'title':args['title'].strip(),'workshop_id':args.get('workshop_id',''),
                    'github_url':url,'nexus_url':url,'modio_url':url,'source_url':url,
                    'archive_paths':args.get('archive_paths',[]),
                    'is_batch_mode':bool(args.get('batch_directory')),
                    'cover_source':args.get('cover_source',''),'offline_html_path':args.get('offline_html_path',''),
                    'game_id':int(game),'app_id':int(game),'game_name':game_info.name},
            resources=resources,preflight=preflight)
        label=('批量目录导入 · '+source.name) if args.get('batch_directory') else args['title'] or source.name
    else:
        from ui.metadata_refresh_thread import ModRefreshWorker
        asset,info=bridge._command_asset(args)
        if info.platform.lower()!='steam' or not (resources.steam_api_url or resources.steam_public):
            raise PermissionError('当前仅准入已配置本地 HTTP 来源的 Steam 元数据首次同步')
        if bridge.adapter.db.is_official_metadata_synced(asset.mod_pk):
            raise ValueError('已完成首次来源同步；重复来源更新尚未迁移')
        def preflight():
            with bridge._lock:
                bridge._command_game(args)
                bridge._command_asset(args)
                resources.validate(asset.managed_path)
        worker=ModRefreshWorker(asset.managed_path,mod_id=asset.mod_pk,platform='Steam',
            library_root=resources.path('mod'),source_url=info.source_url,parent=bridge,
            resources=resources,preflight=preflight)
        label=asset.name
        with bridge._lock:
            if asset.internal_id in bridge._busy:
                worker.deleteLater()
                raise RuntimeError('该 Mod 正在执行其他写操作')
            bridge._busy.add(asset.internal_id)
    job={'id':request_id,'game':game,'game_name':game_info.name,'action':action,'name':label,'state':'running',
         'message':'正在按来源规则归档压缩包…' if action=='import-zip' else '正在导入目录…' if importing else '正在请求 Steam 来源元数据…'}
    bridge._acquisition[request_id]=job
    while len(bridge._acquisition)>50:
        old=next((k for k,v in bridge._acquisition.items() if v['state']!='running'),None)
        if old is None:break
        del bridge._acquisition[old]
    bridge._jobs[request_id]=worker
    if importing and args.get('batch_directory'):
        job['items']=[]
        def item_done(item):
            pk=item.get('mod_id');row=bridge.adapter.db.get_mod_backup_row(pk) if pk else None
            job['items'].append({'name':item.get('title') or __import__('pathlib').Path(item['source']).name,'id':item['source'],
                'state':'skipped' if item.get('status')=='duplicate' else 'success' if item.get('success') else 'failed',
                'message':item.get('error') or '归档与身份已持久化','internal_id':row['internal_id'] if row else None})
        worker.item_finished.connect(item_done)
    def progress(message):job['message']=str(message)
    def done(result=None,error=None):
        bridge._busy.discard(args['_busy_key'])
        if action=='source-sync':bridge._busy.discard(asset.internal_id)
        if error is None:
            try:
                pk=str(result.mod_id)
                row=bridge.adapter.db.get_mod_backup_row(pk)
                info=bridge.adapter.db.get_mod_deploy_info(pk)
                if row is None or info is None or str(info.app_id)!=game:
                    raise ValueError('持久化身份或目标游戏不匹配')
                UUID(row['internal_id'])
                if str(bridge.adapter.db.find_mod_by_internal_id(row['internal_id']))!=pk:
                    raise ValueError('持久化 UUID 与 DAL PK 不匹配')
                job['internal_id']=row['internal_id']
                job['mod_pk']=pk
                job['managed_path']=str(resources.validate(row['last_known_path']))
                duplicate=bool(getattr(result,'is_duplicate',False))
                job['state']='duplicate' if duplicate else 'success'
                job['message']='该 Mod 已存在，未覆盖归档' if duplicate else (('Steam ZIP 已按原规则解包归档' if str(platform).lower()=='steam' else '来源压缩包已原样归档，未解包') if action=='import-zip' else '目录与身份已持久化' if importing else '来源元数据已同步；保留用户覆盖，未下载内容或封面')
                if importing and args.get('batch_directory'):
                    job['state']='partial' if result.failed_count else 'success'
                    job['message']=f'新增 {result.imported_count} · 跳过 {result.skipped_count} · 失败 {result.failed_count}'+('；'+result.error if result.failed_count else '')
            except Exception as exc:
                error=str(exc)
        if error is not None:
            job['state']='failed';job['message']=str(error)
        message={'request':request_id}
        if error is None:message['result']=dict(job)
        else:message['error']=str(error)
        bridge.response.emit(json.dumps(message,ensure_ascii=False))
    if importing:
        worker.progress_changed.connect(progress)
        worker.import_finished.connect(lambda result:done(result))
        worker.import_failed.connect(lambda error:done(error=error))
    else:
        worker.refresh_finished.connect(lambda result:done(result))
        worker.refresh_failed.connect(lambda error:done(error=error))
    worker.finished.connect(lambda:(bridge._jobs.pop(request_id,None),worker.deleteLater()))
    worker.start()
