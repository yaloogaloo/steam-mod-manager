"""Admission for selected browser snapshots; materialization remains ImportWorker-owned."""
import hashlib
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, unquote

from bs4 import BeautifulSoup

# Explicit admission, not a guessed conversion from translated game names.
NEXUS_GAMES = {413150:'stardewvalley', 1623730:'palworld', 1086940:'baldursgate3',
               1091500:'cyberpunk2077', 489830:'skyrimspecialedition'}


def input_files(resources, value):
    source=resources.validate(value)
    if not resources.input_allowed(source) or not source.is_file():
        raise PermissionError('请选择当前隔离 incoming 内的离线网页')
    if source.suffix.lower() not in ('.html','.htm','.mhtml','.mht') or source.stat().st_size>32*1024*1024:
        raise ValueError('仅支持不超过 32 MiB 的 HTML/MHTML')
    from services.offline.html_rewriter import companion_files_dir
    companion=companion_files_dir(source) if source.suffix.lower() in ('.html','.htm') else None
    files=[source]
    if companion:
        for path in companion.rglob('*'):
            resources.validate(path)
            if path.is_file():files.append(path)
    if len(files)>4096 or sum(p.stat().st_size for p in files)>256*1024*1024:
        raise ValueError('页面资源超过 4096 文件或 256 MiB')
    digest=hashlib.sha256()
    for path in sorted(files):
        digest.update(str(path).encode());digest.update(path.read_bytes())
    return source,companion,digest.hexdigest()


def inspect_page(resources,value,game):
    source,companion,digest=input_files(resources,value)
    from services.offline.nexus_html_parser import parse_nexus_offline_html
    from services.offline.mhtml import extract_mhtml
    assets={};cid={}
    if source.suffix.lower() in ('.mhtml','.mht'):
        html,assets,cid=extract_mhtml(source)
        if len(assets)>4096 or sum(len(a) for a in assets.values())>256*1024*1024:
            raise ValueError('MHTML 内嵌资源超过限制')
    else:html=source.read_text(encoding='utf8',errors='replace')
    # Parsing never renders HTML. Only the selected page and its companion may be read.
    soup=BeautifulSoup(html,'html.parser')
    for node in soup.find_all(True):
        for attr in ('src','href','poster','srcset','data'):
            raw=str(node.get(attr) or '').strip()
            if not raw or raw in cid or raw.removeprefix('cid:') in cid:continue
            values=[part.strip().split()[0] for part in raw.split(',') if part.strip()] if attr=='srcset' and not raw.startswith('data:') else [raw]
            for value in values:
                parsed=urlsplit(value)
                if attr=='href' and node.name=='a' and not parsed.scheme and not value.startswith(('\\','file:')):continue
                if parsed.scheme.lower() in ('http','https','data','cid','mailto','javascript') or value.startswith(('#','//')):continue
                local=unquote(parsed.path).replace('\\','/')
                if source.suffix.lower() in ('.mhtml','.mht'):
                    if local in assets:continue
                    raise ValueError('MHTML 含未授权本地资源引用')
                candidate=Path(local[1:] if re.match(r'^/[A-Za-z]:/',local) else local)
                candidate=resources.validate(candidate if candidate.is_absolute() else source.parent/candidate)
                if companion is None or not candidate.is_relative_to(companion):
                    raise ValueError('HTML 资源引用不属于所选网页伴随目录')
    temp=resources.path('cache/temp');temp.mkdir(parents=True,exist_ok=True)
    # MHTML uses the original MIME extractor before the original Nexus metadata parser.
    if assets or source.suffix.lower() in ('.mhtml','.mht'):
        with tempfile.TemporaryDirectory(prefix='nexus_preview_',dir=temp) as root:
            index=Path(root)/'index.html';index.write_text(html,encoding='utf8')
            candidates=parse_nexus_offline_html(index)
    else:candidates=parse_nexus_offline_html(source)
    url=urlsplit(candidates.source_url or '')
    parts=url.path.strip('/').split('/')
    if (url.scheme!='https' or url.hostname not in ('www.nexusmods.com','nexusmods.com') or url.port or url.username
        or len(parts)!=3 or parts[1]!='mods' or not parts[2].isascii() or not parts[2].isdigit() or int(parts[2])<=0
        or candidates.external_id!=parts[2]):raise ValueError('不能确认可信 Nexus 游戏及 Mod 身份')
    expected=NEXUS_GAMES.get(int(game))
    if not expected:raise ValueError('当前游戏的 Nexus 离线来源对应关系尚未准入')
    if parts[0]!=expected:raise ValueError('网页 Nexus 游戏与当前游戏不匹配')
    if not candidates.title or len(candidates.title)>200:raise ValueError('页面缺少可用的 Mod 名称')
    return {'source':str(source),'fingerprint':digest,'name':candidates.title,'url':f'https://www.nexusmods.com/{parts[0]}/mods/{parts[2]}',
            'external_id':parts[2],'format':'MHTML' if source.suffix.lower() in ('.mhtml','.mht') else 'HTML',
            'description':bool(candidates.description),'resources':len(assets) if assets else sum(1 for p in companion.rglob('*') if p.is_file()) if companion else 0,
            'cover':bool(candidates.cover_asset_path) or any(Path(k).suffix.lower() in ('.png','.jpg','.jpeg','.webp') for k in assets)}


def compensate(worker, before, url):
    """Only this item's newly allocated, UUID-proven entity may be removed."""
    r=worker.resources;db=r.db
    from services.path_lifecycle import resolve_managed_folder
    for row in db._conn.execute('SELECT mod_id,internal_id,source_url FROM mods').fetchall():
        if row[0] in before or row[2]!=url or row[0]!=getattr(worker,'_offline_created_pk',None):continue
        path=resolve_managed_folder(row[1],library_root=r.path('mod'),db=db).path
        if path:
            path=r.validate(path)
            if not path.is_relative_to(r.path('mod')):raise PermissionError('失败资源无法证明归属')
            target=r.path('cache/offline_failed')/row[1]
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists():raise RuntimeError('失败资源已存在，保留记录等待恢复')
            path.rename(target)
        if not db.delete_mod_record(row[0]):raise RuntimeError('新增记录补偿失败，保留待恢复记录')


def import_scoped_batch(worker, paths):
    from services.importers.importer_base import ImportResult
    from services.importers.duplicate_check import find_duplicate_mod
    from services.importers.offline_html_batch import make_empty_mod_stub
    r=worker.resources;db=r.db;game=int(worker.params['game_id']);expected=worker.params.get('offline_expected',{})
    temp=r.path('cache/temp');temp.mkdir(parents=True,exist_ok=True)
    for index,path in enumerate(paths):
        worker._emit_progress(f'正在注册 {index+1}/{len(paths)} · {path.name}')
        item={'id':path.name,'name':path.name,'file':path.name,'source':str(path),'state':'failed','message':'未完成'}
        worker._offline_created_pk=None
        before={row[0] for row in db._conn.execute('SELECT mod_id FROM mods')};page=None
        try:
            page=inspect_page(r,path,game)
            if not expected.get(str(path)) or page['fingerprint']!=expected[str(path)]['fingerprint'] or page['url']!=expected[str(path)]['url']:
                raise ValueError('页面或来源身份已变化，请重新预览此项')
            duplicate=find_duplicate_mod(db,platform='Nexus',external_id=page['external_id'],source_url=page['url'],app_id=game)
            if duplicate:
                item.update(state='skipped',message='已存在；未覆盖资源或用户字段',internal_id=db.get_mod_backup_row(duplicate.mod_id)['internal_id'])
            else:
                stub=make_empty_mod_stub(ident=path.stem,temp_root=temp)
                try:result=worker._import_one_folder(stub,title='',cover_source='',offline_html=str(path),batch=False)
                finally:
                    if stub.exists() and not any(stub.iterdir()):stub.rmdir();stub.parent.rmdir()
                if result.is_duplicate:
                    row=db.get_mod_backup_row(result.mod_id)
                    item.update(state='skipped',message='已由其他操作注册，未覆盖资源',internal_id=row['internal_id'])
                else:
                    if not result.success:raise RuntimeError(result.error or '注册失败')
                    row=db.get_mod_backup_row(result.mod_id);managed=r.validate(row['last_known_path'])
                    from services.offline.paths import resolve_offline_page
                    offline=resolve_offline_page(managed)
                    if not offline or not Path(offline).is_file():raise RuntimeError('离线页面未归档')
                    from services.info_asset_runtime import load_usable_live_manifest
                    if load_usable_live_manifest(offline) is None:raise RuntimeError('离线 manifest/CAS 不完整')
                    from services.content_status_eval import persist_evaluated_content_status
                    status=persist_evaluated_content_status(result.mod_id,managed,db=db,notify_projection=False)
                    item.update(state='success',message='身份、离线页面及资源已归档 · '+status,internal_id=row['internal_id'])
            item.update(name=page['name'],url=page['url'])
        except Exception as exc:
            db._conn.rollback()
            try:
                if page:compensate(worker,before,page['url'])
                item['message']=str(exc)
            except Exception as recovery:item['message']=f'{exc}；补偿未完成：{recovery}'
        worker.batch_items.append(item);worker.item_finished.emit(dict(item))
    success=sum(i['state']=='success' for i in worker.batch_items);skipped=sum(i['state']=='skipped' for i in worker.batch_items)
    return ImportResult(success=bool(success or skipped),platform='Nexus',imported_count=success,skipped_count=skipped,
                        failed_count=len(worker.batch_items)-success-skipped,error=f'成功 {success} · 跳过 {skipped} · 失败 {len(worker.batch_items)-success-skipped}')
