"""Inspector entry points into existing cover, offline and metadata services."""
import json
from pathlib import Path
from urllib.parse import urlsplit


ACTIONS={'open-source','open-offline','import-offline','cover','refresh-metadata','save-offline','custom-deploy-pick'}


def source_url(bridge,asset):
    info=bridge.adapter.verify(asset)
    url=str(info.source_url or '').strip()
    if not url and info.platform.lower()=='steam':
        wid=str(info.external_id or info.workspace_id or '')
        if wid.isascii() and wid.isdigit():url='https://steamcommunity.com/sharedfiles/filedetails/?id='+wid
    parsed=urlsplit(url)
    hosts={'steamcommunity.com','www.nexusmods.com','nexusmods.com','mod.io','github.com','www.moddb.com'}
    custom_source=info.platform.lower()=='other'
    if (parsed.scheme not in (('http','https') if custom_source else ('https',)) or not parsed.hostname
        or (not custom_source and parsed.hostname not in hosts and not parsed.hostname.endswith('.mod.io'))
        or parsed.username or parsed.password or parsed.port not in (None,443)
        or any(ord(c)<32 or ord(c)==127 for c in url)):
        raise ValueError('没有合法的官方来源地址')
    return url


def pick(bridge,request_id,action,args):
    """Called on the GUI thread; only native-selected files gain read permission."""
    from PySide6.QtWidgets import QFileDialog,QApplication
    asset,info=bridge._command_asset(args)
    if action=='custom-deploy-pick':
        from services.business_resources import checked_resource_path
        from services.deploy_status import resolve_game_install_path
        with bridge.resources.bind():start=resolve_game_install_path(mod_pk=asset.mod_pk,app_id=info.app_id)
        chosen=QFileDialog.getExistingDirectory(QApplication.activeWindow(),'选择自定义部署目录',start or '')
        bridge._command_asset(args)
        if chosen:
            path=Path(chosen);checked_resource_path(path.resolve(),path)
            import os
            forbidden=(bridge.resources.root,bridge.resources.path('mod'),Path(os.environ.get('WINDIR','C:/Windows')))
            if len(path.resolve().parts)<3 or any(path.resolve().is_relative_to(p.resolve()) or p.resolve().is_relative_to(path.resolve()) for p in forbidden):raise ValueError('不能将系统、程序或归档库设为部署目录')
            bridge._custom_paths[asset.internal_id]=str(path.resolve())
        bridge._busy.discard(args['_busy_key'])
        bridge.response.emit(json.dumps({'request':request_id,'result':{'path':str(Path(chosen).resolve()) if chosen else '', 'cancelled':not bool(chosen)}}))
        return False
    title,filter=('更换封面','Images (*.png *.jpg *.jpeg *.jfif *.webp)') if action=='cover' else ('关联已有 Mod 的离线页面','HTML / MHTML (*.html *.htm *.mhtml *.mht)')
    from services.importers.import_settings import resource_picker_directory,remember_resource_directory
    chosen,_=QFileDialog.getOpenFileName(QApplication.activeWindow(),title,resource_picker_directory(bridge.resources),filter)
    bridge._command_asset(args)
    if not chosen:
        bridge._busy.discard(args['_busy_key'])
        bridge.response.emit(json.dumps({'request':request_id,'result':{'cancelled':True,'success':False}}))
        return False
    args['_selected_source']=str(bridge.resources.select_input(chosen))
    remember_resource_directory(bridge.resources,chosen)
    return True


def execute(bridge,action,args):
    r=bridge.resources
    if action=='save-offline':return save_offline(bridge,args)
    with bridge._lock,r.bind():
        asset,info=bridge._command_asset(args)
        managed=None
        if action!='cover' or Path(asset.managed_path).is_dir():
            managed=bridge.adapter.folder(asset)
            r.validate(managed)
        if action=='open-offline':
            from services.info_asset_runtime import prepare_offline_open
            result=prepare_offline_open(managed,mod_id=str(asset.mod_pk))
            if not result.ok or not result.path:raise ValueError('未保存离线页面，或归档资源缺失')
            index=r.validate(result.path)
            return {'success':True,'path':str(index),'internal_id':asset.internal_id}
        if action=='cover':
            from services.importers.image_picker import apply_cover_to_mod,validate_cover_image
            from PySide6.QtGui import QImage
            source=r.validate(args['_selected_source']);validate_cover_image(source)
            if source.stat().st_size>32*1024*1024 or QImage(str(source)).isNull():raise ValueError('请选择有效图片（最大 32 MiB）')
            if managed is None:
                from services.mod_presence import persist_miss_cover
                rel=persist_miss_cover(asset.mod_pk,source,db=r.db)
            else:
                rel=apply_cover_to_mod(managed,source,mod_id=asset.mod_pk)
            if not rel or bridge.adapter.db.get_mod_display_info(asset.mod_pk).cover_path!=rel:
                raise RuntimeError('封面未成功持久化')
            return {'success':True,'message':'封面已保存并标记用户覆盖','internal_id':asset.internal_id}
        if action=='import-offline':
            if info.platform.lower()!='nexus':raise ValueError('旧版手动离线页面关联仅支持 Nexus；其他来源需对应归档服务')
            from services.importers.offline_admission import inspect_page
            page=inspect_page(r,args['_selected_source'],info.app_id)
            if page['external_id']!=str(info.external_id or info.workspace_id):raise ValueError('网页 Nexus 身份与当前 Mod 不匹配，未关联')
            from services.offline.manager import attach_nexus_offline_page
            result=attach_nexus_offline_page(asset.mod_pk,args['_selected_source'],managed_path=managed,library_root=r.path('mod'))
            if result.status!='archived':raise RuntimeError(result.error or '离线页面未成功归档')
            return {'success':True,'message':'已关联当前 Mod 的离线页面，未创建新身份','internal_id':asset.internal_id}
        if action=='refresh-metadata':
            if info.platform.lower()!='steam':
                from services.mod_refresh import refresh_mod
                from contextlib import nullcontext
                with r.source_network('sync') if getattr(r,'production',False) else nullcontext():
                    refreshed=refresh_mod(asset.mod_pk,managed,platform=info.platform,library_root=r.path('mod'),source_url=info.source_url,db=r.db)
                if not refreshed.success or (refreshed.official_attempted and not refreshed.official_success):
                    raise RuntimeError(f'{info.platform} 来源刷新未完成（{asset.name}）：'+(refreshed.error or refreshed.message or '元数据刷新失败'))
                message=refreshed.provider.message if refreshed.provider and refreshed.provider.message else refreshed.message
                return {'success':True,'message':message or '本地元数据已重新读取','internal_id':asset.internal_id}
            from core.steam_api import SteamWorkshopClient
            from services.metadata_refresh import refresh_steam_mod_metadata
            from contextlib import nullcontext
            from services.proxy_resolver import resolve_proxy
            production=getattr(r,'production',False)
            with r.source_network('sync') if production else nullcontext():
                from services.mod_refresh import reconcile_local_state
                reconcile_local_state(asset.mod_pk,managed,library_root=r.path('mod'),db=r.db)
                with SteamWorkshopClient(db=r.db,proxy_url=resolve_proxy().url if production else '',enable_scrape_fallback=False) as client:
                    result=refresh_steam_mod_metadata(asset.mod_pk,managed,library_root=r.path('mod'),client=client,force=True,download_cover=production,db=r.db)
            if not result.success or result.skipped:raise RuntimeError(result.error or '来源没有完成刷新')
            return {'success':True,'message':'Steam 官方元数据已刷新，用户覆盖保持不变','internal_id':asset.internal_id}
        raise PermissionError('不支持的 Inspector 操作')


def save_offline(bridge,args):
    from services.offline.manager import OfflineManager
    from contextlib import nullcontext
    r=bridge.resources
    with bridge._lock,r.bind():
        asset,info=bridge._command_asset(args)
        if info.platform.lower() not in ('steam','github','mod.io','modio'):raise ValueError('此来源使用导入离线网页，而非在线保存')
        source_url(bridge,asset)
        managed=r.validate(bridge.adapter.folder(asset))
    with r.source_network('offline') if hasattr(r,'source_network') else nullcontext():
        with r.bind():
            result=OfflineManager(db=r.db,library_root=r.path('mod')).update_mod_offline(asset.mod_pk,managed_path=managed,platform=info.platform,force_refresh=True)
            if result.outcome!='success' or not result.index_path:raise RuntimeError(result.error or result.skip_reason or '离线页面未完成保存')
            r.validate(result.index_path)
    return {'success':True,'message':'离线网页及资源已保存','internal_id':asset.internal_id}
