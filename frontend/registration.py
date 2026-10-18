"""Explicit registration drafts and content association, using existing importers/authorities."""
import hashlib
import json
import shutil
import time
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import urlsplit

ACTIONS={'registration-save','content-add','content-rescan','content-open','content-cancel'}


def incoming(resources,value):
    path=resources.validate(value)
    if not resources.input_allowed(path) or path==resources.path('incoming'):
        raise PermissionError('仅允许当前 incoming 中的明确输入资源')
    if not path.exists():raise ValueError('输入资源不存在')
    for item in ([path] if path.is_file() else path.rglob('*')):resources.validate(item)
    return path


def fingerprint(path,*,bounded=True):
    files=[path] if path.is_file() else sorted(p for p in path.rglob('*') if p.is_file())
    if bounded and (len(files)>4096 or sum(p.stat().st_size for p in files)>256*1024*1024):
        raise ValueError('资源超过 4096 文件或 256 MiB 限制')
    entries=[]
    for p in files:
        digest=hashlib.sha256()
        with p.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
        entries.append((str(p),p.stat().st_size,digest.hexdigest()))
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def local_fingerprint(resources,source,archive_paths=None):
    paths=archive_paths or [str(source)]
    return hashlib.sha256(json.dumps([fingerprint(incoming(resources,p),bounded=not getattr(resources,'production',False)) for p in paths]).encode()).hexdigest()


def preview(bridge,args):
    bridge._command_game(args);bridge.adapter._write()
    if int(args['game'])<=0:raise ValueError('注册前需要选择明确的目标游戏')
    r=bridge.resources
    if r is None:raise PermissionError('缺少明确的隔离资源上下文')
    with r.bind():
        kind=args.get('kind');platform=args.get('platform');title=str(args.get('title') or '').strip()
        candidates=None;source=None;source_hash='';url=str(args.get('url') or '').strip();ext=''
        if kind=='local':
            from .acquisition import validate_import
            args=dict(args);args['_zip']=args.get('format')=='zip'
            args['batch_directory']=args.get('format')=='batch'
            source=validate_import(bridge,{**args,'source':args.get('source'),'workshop_id':args.get('workshop_id')},preview=True)
            platform=args.get('platform') or 'Steam'
            if platform=='Steam':
                ext=args['workshop_id'];url='https://steamcommunity.com/sharedfiles/filedetails/?id='+ext
            else:
                from services.importers.identity_resolve import resolve_import_identity,ImportIdentity
                resolved=resolve_import_identity(platform,github_url=url if platform=='GitHub' else '',nexus_url=url if platform=='Nexus' else '',modio_url=url if platform=='mod.io' else '',source_url=url,allow_local_fallback=args.get('format')=='batch' or platform in ('Nexus','Other'))
                if not isinstance(resolved,ImportIdentity):raise ValueError(getattr(resolved,'error','来源身份无法确认'))
                ext=resolved.external_id;url=resolved.source_url
                if platform=='Nexus' and args.get('offline_html_path'):
                    from services.importers.offline_admission import inspect_page
                    page=inspect_page(r,incoming(r,args['offline_html_path']),int(args['game']))
                    url=page['url'];ext=page['external_id']
            source_hash=local_fingerprint(r,source,args.get('archive_paths'))
        elif kind=='offline':
            source=incoming(r,args.get('source',''))
            from services.importers.offline_admission import inspect_page,page_candidates
            page=inspect_page(r,source,int(args['game']))
            candidates=page_candidates(r,source)
            platform='Nexus';url=candidates.source_url;ext=candidates.external_id;title=title or candidates.title
            # Page parsing never installs arbitrary captured images or requests remote URLs.
            candidates.cover_asset_path=None
            source_hash=page['fingerprint']
        elif kind!='url':raise ValueError('不支持的注册输入方式')
        from core.mod_platform import get_available_sources,normalize_platform
        game=bridge.adapter.db.get_game(int(args['game']))
        if normalize_platform(platform) not in {p for p,label in get_available_sources(game.name,int(args['game']))}:
            raise ValueError('未识别的手工注册来源平台')
        if kind!='local' and (platform not in ('Steam','Nexus','mod.io','GitHub','Other') or kind not in ('url','offline')):
            raise ValueError('该来源注册方式尚未准入')
        if kind in ('url','offline'):
            from services.importers.identity_resolve import resolve_import_identity,ImportIdentity
            raw=url
            if platform=='Steam':
                if not raw.isascii() or not raw.isdigit():
                    parsed=urlsplit(raw)
                    if parsed.scheme!='https' or parsed.hostname!='steamcommunity.com' or parsed.username or parsed.password or parsed.port:
                        raise ValueError('请输入 Steam Workshop 数字 ID 或官方 HTTPS 链接')
                from services.importers.steam import parse_workshop_id
                ext=parse_workshop_id(raw)
                if not ext.isascii() or not ext.isdigit() or int(ext)<=0:raise ValueError('请输入有效 Steam Workshop 数字 ID')
            resolved=resolve_import_identity(platform,workshop_id=ext if platform=='Steam' else '',nexus_url=raw if platform=='Nexus' else '',github_url=raw if platform=='GitHub' else '',modio_url=raw if platform=='mod.io' else '',source_url=raw,allow_local_fallback=platform=='Other')
            if not isinstance(resolved,ImportIdentity):raise ValueError(getattr(resolved,'error','来源身份无法确认'))
            ext=resolved.external_id;url=resolved.source_url
        # The original mod.io importer also accepts a numeric API identity.
        # It is stored as identity input, never treated as a fetchable URL.
        if url and not (platform=='mod.io' and url.isascii() and url.isdigit()):
            parsed=urlsplit(url)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or any(ord(c)<32 for c in url):raise ValueError('来源地址必须是合法 HTTP/HTTPS URL')
            hosts={'Steam':('steamcommunity.com',),'Nexus':('www.nexusmods.com','nexusmods.com'),'mod.io':('mod.io',),'GitHub':('github.com','www.github.com')}
            if platform in hosts and (parsed.scheme!='https' or parsed.hostname not in hosts[platform] or parsed.port):raise ValueError('来源地址与所选平台不匹配')
            if platform in ('Nexus','mod.io'):
                from urllib.parse import parse_qs
                if set(parse_qs(parsed.query))-{'tab','file_id','nmm'}:raise ValueError('来源地址包含不支持的跳转参数')
        if len(title)>200 or any(ord(c)<32 for c in title):raise ValueError('名称最多 200 字符，不得包含控制字符；留空时使用原来源命名规则')
        from services.importers.duplicate_check import find_duplicate_mod
        duplicate=find_duplicate_mod(bridge.adapter.db,platform=platform,external_id=ext,source_url=url,app_id=int(args['game']))
        confirmation={}
        if platform=='Nexus' and not (kind=='local' and args.get('format')=='batch'):
            from services.importers.nexus_registration_identity import check_game
            if not url:raise ValueError('请提供 Nexus Mod 链接或数字 ID，也可附加包含身份的离线网页')
            confirmation=check_game(r.db,int(args['game']),url)
        if platform=='mod.io' and url:
            if url.isascii() and url.isdigit():
                if int(url)<=0:raise ValueError('mod.io Mod ID 必须是有效的正整数')
            else:
                from re import fullmatch
                parts=urlsplit(url).path.strip('/').split('/')
                if len(parts)!=4 or parts[0]!='g' or parts[2]!='m' or not all(fullmatch(r'[a-zA-Z0-9_-]+',p) for p in (parts[1],parts[3])):
                    raise ValueError('请输入完整的 mod.io Mod 链接或数字 ID')
        draft={'kind':kind,'platform':platform,'title':title,'url':url,'external_id':ext,'source':str(source or ''),'fingerprint':source_hash,
               'identity_confirmation':confirmation,
               'game':str(args['game']),'snapshot':args['snapshot'],'created':time.monotonic(),'candidates':candidates,'format':args.get('format')}
        draft['archive_paths']=args.get('archive_paths') or []
        draft['input_url']=str(args.get('url') or '').strip()
        draft['attachments']={field:{'path':str(incoming(r,args[field])),'fingerprint':fingerprint(incoming(r,args[field]))} for field in ('cover_source','offline_html_path') if args.get(field)}
        candidates_batch=[]
        if kind=='local' and args.get('format')=='batch':
            from services.importers.directory_batch import discover_mod_directories
            candidates_batch=[{'name':p.name,'path':str(p)} for p in discover_mod_directories(source)]
            if not candidates_batch:raise ValueError('未发现可导入的 Mod 子目录')
        token=token_urlsafe(24)
        if len(bridge._registration)>=20:bridge._registration.pop(next(iter(bridge._registration)))
        bridge._registration[token]=draft
        return {'draft':token,'game':draft['game'],'platform':platform,'identity_confirmation':confirmation,'title':title or (f'{len(candidates_batch)} 个 Mod · 使用各目录名称' if candidates_batch else ''),'source_url':url,'external_id':ext,
            'batch_candidates':candidates_batch,
            'attachments':{field:Path(value['path']).name for field,value in draft['attachments'].items()},
            'description':candidates.description if candidates else '', 'has_content':kind=='local',
            'duplicate':bool(duplicate),'duplicate_pk':str(duplicate.mod_id) if duplicate else None,
            'operation':'关联已有对象，不覆盖归档' if duplicate else '注册并归档本体' if kind=='local' else '注册身份及空托管目录 · 待补齐内容',
            'metadata_note':('纯本地 Other 无来源身份：每次导入都会创建新的 Mod，不按名称或目录合并。' if kind=='local' and platform=='Other' and not url else
                '仅解析本地页面；不执行脚本、不加载远端资源；离线页面归档另待迁移' if candidates else '仅使用来源身份和填写的名称；未联网获取元数据或下载本体')}


def managed(bridge,args):
    asset,info=bridge._command_asset(args)
    from services.path_lifecycle import resolve_managed_folder
    resolved=resolve_managed_folder(asset.internal_id,library_root=bridge.resources.path('mod'),db=bridge.adapter.db)
    if resolved.path is None:raise ValueError('无法通过 Frozen UUID 证明托管归档位置')
    path=bridge.resources.validate(resolved.path)
    if not path.is_relative_to(bridge.resources.path('mod')):raise PermissionError('托管归档不属于当前资源库')
    for item in path.rglob('*'):bridge.resources.validate(item)
    return asset,path


def rescan(bridge,asset,path):
    from services.local_file_index import reconcile_local_files
    from services.content_status_eval import persist_evaluated_content_status
    if not bridge.adapter.db.get_mod_files(asset.mod_pk).files:
        from core.mod_platform import normalize_platform
        from services.importers.source_files import (build_nexus_mod_files,build_modio_mod_files,
            build_other_mod_files,build_github_mod_files,build_steam_workshop_bundle)
        build={'nexus':build_nexus_mod_files,'modio':build_modio_mod_files,
            'other':build_other_mod_files,'github':build_github_mod_files,
            'steam':build_steam_workshop_bundle}.get(normalize_platform(asset.platform))
        if build is None:raise ValueError('该来源没有现有内容索引服务')
        bridge.adapter.db.set_mod_files(asset.mod_pk,build(path))
    result=reconcile_local_files(asset.mod_pk,managed_path=path,db=bridge.adapter.db)
    if result.replacement_candidates:raise ValueError('存在内容替换冲突，未自动替换已有资源')
    status=persist_evaluated_content_status(asset.mod_pk,path,db=bridge.adapter.db,notify_projection=False)
    actual=bridge.adapter.db._conn.execute('SELECT content_status FROM mods WHERE mod_id=?',(asset.mod_pk,)).fetchone()[0]
    if actual!=status:raise RuntimeError('内容识别结果未成功持久化')
    return status


def _execute(bridge,action,args):
    bridge.adapter._write()
    if bridge.resources is None:raise PermissionError('缺少隔离业务资源')
    r=bridge.resources;db=bridge.adapter.db
    if action=='content-add':
        return complete_content(bridge,args)
    with bridge._lock,db._lock,r.bind():
        bridge._command_game(args)
        if action=='registration-save':
            draft=bridge._registration.get(args.get('draft'))
            if not draft or draft['game']!=str(args['game']) or draft['snapshot']!=args['snapshot'] or time.monotonic()-draft['created']>600:
                raise ValueError('注册预览已过期，请重新预览')
            from services.importers.nexus_registration_identity import require_confirmation
            require_confirmation(draft.get('identity_confirmation',{}),args.get('confirm_nexus_identity'))
            if draft['kind']=='local':raise ValueError('本地内容确认须调用已准入 ImportWorker')
            if draft['kind']=='offline':
                from services.importers.offline_admission import inspect_page
                current=inspect_page(r,incoming(r,draft['source']),int(args['game']))
                if current['fingerprint']!=draft['fingerprint']:raise ValueError('输入页面或伴随资源已变化，请重新预览')
                require_confirmation(current['identity_confirmation'],args.get('confirm_nexus_identity'))
            from services.importers.duplicate_check import find_duplicate_mod
            duplicate=find_duplicate_mod(db,platform=draft['platform'],external_id=draft['external_id'],source_url=draft['url'],app_id=int(args['game']))
            if duplicate:
                info=db.get_mod_backup_row(duplicate.mod_id)
                return {'success':True,'duplicate':True,'internal_id':info['internal_id'],'message':'已关联既有 Mod，未覆盖资源或用户字段'}
            title=str(args.get('title') or draft['title']).strip()
            if len(title)>200 or any(ord(c)<32 for c in title):raise ValueError('名称不符合要求')
            from core.sanitize import sanitize_folder_name
            # Same optional-name stub semantics as the retired import dialog;
            # the original Importer remains responsible for the final title.
            folder=r.path('cache/registration')/args['draft']/sanitize_folder_name(title or draft['external_id'],fallback='Empty_Mod')
            folder.mkdir(parents=True,exist_ok=True)
            game=db.get_game(int(args['game']))
            before={row[0] for row in db._conn.execute('SELECT mod_id FROM mods')}
            try:
                if draft['platform']=='Steam':
                    from services.importers.steam import SteamImporter
                    result=SteamImporter(db=db).import_mod(workshop_id=draft['external_id'],source_folder=folder,title=title,app_id=int(args['game']),game_name=game.name,library_root=r.path('mod'))
                elif draft['platform']=='Nexus':
                    from services.importers.nexus import NexusImporter
                    result=NexusImporter(db=db).import_mod(source_folder=folder,title=title,nexus_url=draft['url'],nexus_id=draft['external_id'],app_id=int(args['game']),game_name=game.name,library_root=r.path('mod'))
                elif draft['platform']=='mod.io':
                    from services.importers.modio import ModioImporter
                    result=ModioImporter(db=db).import_mod(source_folder=folder,title=title,modio_url=draft['url'],app_id=int(args['game']),game_name=game.name,library_root=r.path('mod'))
                else:
                    from services.importers.github import GithubImporter
                    from services.importers.other import OtherImporter
                    importer=GithubImporter if draft['platform']=='GitHub' else OtherImporter
                    result=importer(db=db).import_mod(source_folder=folder,title=title,github_url=draft['url'],source_url=draft['url'],app_id=int(args['game']),game_name=game.name,library_root=r.path('mod'))
                if not result.success:raise RuntimeError(result.error or '来源注册失败')
                row=db.get_mod_backup_row(result.mod_id)
                path=r.validate(row['last_known_path'])
                if draft['candidates']:
                    from services.offline.nexus_html_parser import apply_nexus_offline_candidates
                    apply_nexus_offline_candidates(result.mod_id,path,draft['candidates'],db=db)
                    from services.file_ops import read_info_metadata_dict
                    if draft['candidates'].description and (read_info_metadata_dict(path) or {}).get('description')!=draft['candidates'].description:
                        raise RuntimeError('离线来源描述未成功持久化')
                    from services.offline.manager import attach_nexus_offline_page
                    attached=attach_nexus_offline_page(result.mod_id,draft['source'],managed_path=path,
                        library_root=r.path('mod'),clean=True,merge_mode='import_overwrite')
                    if attached.status!='archived':raise RuntimeError(attached.error or '离线页面未完整归档')
                from services.content_status_eval import persist_evaluated_content_status
                status=persist_evaluated_content_status(result.mod_id,path,db=db,notify_projection=False)
                if status!='content_missing':raise RuntimeError('无本体注册的内容状态不符合合同')
                if db._conn.execute('SELECT content_status FROM mods WHERE mod_id=?',(result.mod_id,)).fetchone()[0]!=status:raise RuntimeError('注册内容状态保存失败')
            except Exception as exc:
                db._conn.rollback()
                # Importers commit in phases. Only newly allocated, UUID-proven entities are ours.
                created=[row[0] for row in db._conn.execute('SELECT mod_id FROM mods') if row[0] not in before]
                retained=[]
                for pk in created:
                    row=db.get_mod_backup_row(pk)
                    from services.path_lifecycle import resolve_managed_folder
                    owned=resolve_managed_folder(row['internal_id'],library_root=r.path('mod'),db=db).path
                    if owned is not None:
                        owned=r.validate(owned)
                        quarantine=r.path('cache/registration_failed')/args['draft']/row['internal_id']
                        quarantine.parent.mkdir(parents=True,exist_ok=True)
                        if not owned.is_relative_to(r.path('mod')) or quarantine.exists():
                            raise RuntimeError('注册失败；无法安全隔离资源，保留新增 PK '+str(pk)) from exc
                        owned.rename(quarantine);retained.append(str(quarantine))
                    if not db.delete_mod_record(pk):raise RuntimeError('新增记录撤回失败，保留待恢复 PK '+str(pk))
                if retained:
                    raise RuntimeError('注册失败，已撤回新增记录；失败资源保留在 '+', '.join(retained)+'；原因：'+str(exc)) from exc
                raise
            if folder.exists() and not any(folder.iterdir()):folder.rmdir()
            bridge._registration.pop(args['draft'],None)
            return {'success':True,'internal_id':row['internal_id'],'mod_pk':str(result.mod_id),'managed_path':str(path),'content_status':status,'message':'注册成功 · 待补齐内容；未下载或部署'}
        asset,path=managed(bridge,args)
        if action=='content-rescan':
            status=rescan(bridge,asset,path)
            return {'success':True,'internal_id':asset.internal_id,'content_status':status,'message':'内容已识别；部署仍按原策略检查' if status=='healthy' else '待补齐内容；未找到有效本体'}
        raise PermissionError('不支持的内容操作')


def source_options(bridge):
    from core.mod_platform import get_available_sources
    return {g.key:[p for p,label in get_available_sources(g.name,int(g.key))] for g in bridge.adapter.games()}


def confirm_local(bridge,args):
    token=args.get('registration_draft')
    if not token:return
    draft=bridge._registration.get(token)
    if not draft or draft['kind']!='local' or draft['game']!=str(args['game']) or draft['snapshot']!=args['snapshot'] or time.monotonic()-draft['created']>600:
        raise ValueError('本地导入预览已过期')
    from services.importers.nexus_registration_identity import require_confirmation
    require_confirmation(draft.get('identity_confirmation',{}),args.get('confirm_nexus_identity'))
    for field in ('cover_source','offline_html_path'):
        attachment=draft.get('attachments',{}).get(field)
        value=args.get(field)
        if bool(attachment)!=bool(value):raise ValueError('附加资源与预览不一致')
        if attachment and (str(incoming(bridge.resources,value))!=attachment['path'] or fingerprint(incoming(bridge.resources,value))!=attachment['fingerprint']):raise ValueError('附加资源已变化，请重新预览')
    if (incoming(bridge.resources,draft['source']).resolve()!=incoming(bridge.resources,args.get('source','')).resolve()
        or draft['title']!=str(args.get('title') or '').strip()
        or draft['platform']!=args.get('platform','Steam')
        or (draft.get('format')=='batch')!=bool(args.get('batch_directory'))
        or (draft['platform']=='Steam' and draft['external_id']!=str(args.get('workshop_id') or '').strip())
        or (draft['platform']!='Steam' and draft.get('input_url',draft['url'])!=str(args.get('url') or '').strip())
        or [str(incoming(bridge.resources,p)) for p in draft['archive_paths']]!=[str(incoming(bridge.resources,p)) for p in (args.get('archive_paths') or [])]
        or local_fingerprint(bridge.resources,draft['source'],draft['archive_paths'])!=draft['fingerprint']):
        raise ValueError('本地输入与预览不一致，请重新预览')


def execute(bridge,action,args):
    key=args.get('_request_id')
    job=None
    if key:
        with bridge._lock:
            game=bridge.adapter.db.get_game(int(args['game']))
            draft=bridge._registration.get(args.get('draft'),{})
            job={'id':key,'game':str(args['game']),'game_name':game.name,'action':action,
                 'name':draft.get('title') or next((a.name for a in bridge._assets.values() if a.internal_id==args.get('internal_id')),'Mod 内容'),
                 'state':'running','message':'正在执行；离开此页面不会取消任务'}
            bridge._acquisition[key]=job
            while len(bridge._acquisition)>50:
                old=next((k for k,v in bridge._acquisition.items() if v['state']!='running'),None)
                if old is None:break
                del bridge._acquisition[old]
    try:
        result=_execute(bridge,action,args)
        if job:job.update(result,state='duplicate' if result.get('duplicate') else 'success')
        return result
    except Exception as exc:
        if job:job.update(state='failed',message=str(exc))
        raise


def complete_content(bridge,args):
    """One entity, one bounded stage, then exclusive publish through the old index."""
    from threading import Event
    from services.content_completion import prepare
    from services.local_file_index import has_local_mod_payload
    r=bridge.resources;db=bridge.adapter.db
    def admitted(asset,path):
        status=db.get_mod_deploy_info(asset.mod_pk).deploy_status
        if status!='not_deployed' or has_local_mod_payload(path,mod_id=asset.mod_pk,db=db):
            raise ValueError('仅允许为未部署且没有本体的 Mod 补齐；已有内容不能覆盖')
    with bridge._lock,db._lock,r.bind():
        bridge._command_game(args);asset,path=managed(bridge,args);admitted(asset,path)
        raw=args.get('archive_paths') or [args.get('source','')]
        if not isinstance(raw,list) or not all(isinstance(p,str) for p in raw):raise ValueError('输入包列表无效')
        sources=[incoming(r,p) for p in raw]
        cancel=Event();bridge._content_cancellations[asset.internal_id]=cancel
    def check_cancel():
        if cancel.is_set():raise RuntimeError('补齐任务已取消，未提交内容')
    try:
        with r.bind(),prepare(r,sources,platform=asset.platform,cancel_check=check_cancel) as files:
            with bridge._lock,db._lock:
                bridge._command_game(args);bridge._command_asset(args);admitted(asset,path);check_cancel()
                old=db.get_mod_files(asset.mod_pk)
                copied=[];created_dirs=[]
                try:
                    for item,relative in files:
                        target=r.validate(path/relative)
                        if target.exists():raise ValueError('目标资源重名，未覆盖：'+str(relative))
                    for item,relative in files:
                        check_cancel();target=r.validate(path/relative)
                        missing=[];parent=target.parent
                        while not parent.exists():missing.append(parent);parent=parent.parent
                        for parent in reversed(missing):parent.mkdir();created_dirs.append(parent)
                        with item.open('rb') as src,target.open('xb') as dest:
                            copied.append(target)
                            while block:=src.read(1024*1024):check_cancel();dest.write(block)
                    check_cancel()
                    status=rescan(bridge,asset,path)
                    check_cancel()
                    if status!='healthy':raise ValueError('没有可识别本体，补齐未提交')
                except Exception:
                    for target in reversed(copied):target.unlink(missing_ok=True)
                    for parent in reversed(created_dirs):
                        if parent.exists() and not any(parent.iterdir()):parent.rmdir()
                    db._conn.rollback();db.set_mod_files(asset.mod_pk,old)
                    from services.content_status_eval import persist_evaluated_content_status
                    persist_evaluated_content_status(asset.mod_pk,path,db=db,notify_projection=False)
                    raise
        return {'success':True,'internal_id':asset.internal_id,'mod_pk':str(asset.mod_pk),'managed_path':str(path),
                'content_status':status,'files':len(copied),'message':'已一次性补齐同一 Mod；身份、类型、标签和部署状态保持不变'}
    finally:
        with bridge._lock:bridge._content_cancellations.pop(asset.internal_id,None)
