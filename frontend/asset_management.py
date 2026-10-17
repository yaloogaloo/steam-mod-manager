"""Original file, relationship and user-annotation services exposed to Inspector."""
from services.mod_files import ModFileManager as ModFilesJsonManager

ACTIONS={'file-select','file-selection','file-role','file-category','file-description',
         'relation-add','relation-remove','dependency-add','annotation-set','enabled-set'}


def conflict_report(bridge,args):
    asset,_=bridge._command_asset(args)
    if bridge.resources is None:
        raise PermissionError('未绑定资产库资源目录，不能检查部署清单')
    from services.conflict import ConflictDetector
    with bridge.resources.bind():
        report=ConflictDetector(bridge.resources.path('mod'),db=bridge.adapter.db).check_mod(asset.mod_pk,persist=False)
    bridge._command_asset(args)
    return report.as_dict()


def state(bridge,args):
    asset,info=bridge._command_asset(args);db=bridge.adapter.db
    data={'files':db.get_mod_files(asset.mod_pk).to_dict(),
          'relations':db.get_mod_relationships(asset.mod_pk),
          'status':db.get_mod_status(asset.mod_pk).to_dict(),
          'enabled':db.is_mod_enabled(asset.mod_pk),
          'abandoned':any(t.tag_type=='abandoned' for t in db.get_mod_tags(asset.mod_pk))}
    return {**data,'version':bridge._version(data),'platform':info.platform,
            'peers':[{'internal_id':a.internal_id,'name':a.name,'workspace_id':a.workspace_id}
                     for a in bridge.adapter.assets(int(args['game'])) if a.internal_id!=asset.internal_id]}


def execute(bridge,action,args):
    with bridge._lock,bridge.adapter.db._lock,bridge.resources.bind():
        asset,info=bridge._command_asset(args);db=bridge.adapter.db;pk=asset.mod_pk
        current=state(bridge,args)
        if args.get('version')!=current['version']:raise RuntimeError('Mod 文件、关系或状态已变化，请重新读取')
        files=ModFilesJsonManager(db)
        if action in ('file-select','file-role','file-category','file-description'):
            fid=args.get('file_id')
            if not isinstance(fid,str) or db.get_mod_files(pk).find(fid) is None:raise ValueError('文件记录不存在')
            if action=='file-select':
                if type(args.get('value')) is not bool:raise ValueError('文件选择必须为布尔值')
                files.set_file_selection(pk,fid,args['value'])
            elif action=='file-role':
                if args.get('value') not in ('main','source','other'):raise ValueError('无效文件角色')
                files.set_file_badge_role(pk,fid,args['value'],platform=info.platform)
            elif action=='file-category':
                if info.platform.lower()!='nexus' or args.get('value') not in ('Main','Optional','Miscellaneous','汉化','Other'):raise ValueError('无效 Nexus 文件分类')
                files.set_nexus_file_category(pk,fid,args['value'])
            else:
                value=args.get('value')
                if not isinstance(value,str) or len(value)>10000:raise ValueError('文件描述须为文本，最多 10000 字符')
                files.set_file_description(pk,fid,value)
        elif action=='file-selection':
            mode=args.get('mode')
            if mode=='all':files.set_all_selection(pk,True)
            elif mode=='main':files.select_main_only(pk)
            elif mode=='clear-optional':files.clear_optional_selection(pk)
            elif mode=='default':files.reset_default_selection(pk)
            else:raise ValueError('未知文件选择方式')
        elif action=='dependency-add':
            from services.mod_relationships import add_dependency_by_workspace_id
            wid=args.get('workspace_id')
            if not isinstance(wid,str) or not wid or len(wid)>200:raise ValueError('请填写 Workspace ID')
            add_dependency_by_workspace_id(pk,wid,db=db)
        elif action=='relation-add':
            from core.db_manager import SUPPORTED_RELATIONSHIP_TYPES
            kind=args.get('kind');iid=args.get('target')
            if kind not in SUPPORTED_RELATIONSHIP_TYPES:raise ValueError('无效关系类型')
            peers={a.internal_id:a for a in bridge.adapter.assets(int(args['game']))}
            target=peers.get(iid)
            if target is None or iid==asset.internal_id:raise ValueError('请选择当前游戏的其他 Mod')
            bridge.adapter.verify(target)
            db.add_mod_relationship(pk,target.mod_pk,kind)
        elif action=='relation-remove':
            rid=args.get('relationship_id')
            if type(rid) is not int or rid not in {r['id'] for group in current['relations'].values() for r in group}:raise ValueError('关系不属于当前 Mod')
            if not db.remove_mod_relationship(rid):raise RuntimeError('关系未删除')
        elif action=='annotation-set':
            field=args.get('field');value=args.get('value');note=args.get('note','')
            if type(value) is not bool or not isinstance(note,str) or len(note)>2000:raise ValueError('标记参数无效')
            if field=='invalid':db.update_mod_status(pk,invalid=value,invalid_reason=note if value else '',touch_check_time=True)
            elif field=='conflict':
                from services.user_annotation import apply_conflict_annotation
                apply_conflict_annotation(pk,conflict=value,note=note,db=db)
            elif field=='abandoned':
                if value:db.add_mod_tag(pk,'abandoned','')
                else:db.remove_mod_tag(pk,'abandoned')
            else:raise ValueError('未知用户标记')
        elif action=='enabled-set':
            if type(args.get('value')) is not bool:raise ValueError('启用状态必须为布尔值')
            (db.enable_mod if args['value'] else db.disable_mod)(pk)
            if int(info.app_id)==1142710:
                from services.wh3_activation import sync_used_mods_txt
                try:sync_used_mods_txt()
                except Exception as exc:raise RuntimeError('启用状态已保存，但 WH3 投影失败：'+str(exc)) from exc
        else:raise PermissionError('未知资产管理操作')
        if action.startswith('file-'):
            from pathlib import Path
            if Path(asset.managed_path).is_dir():
                path=bridge.adapter.folder(asset)
                from services.info_sidecar import write_sidecar_for_mod
                if write_sidecar_for_mod(path,pk,db=db) is None:raise RuntimeError('数据库已保存，但文件信息侧车未保存；请重新读取并重试')
        return {'success':True,'message':'已按原业务规则保存',**state(bridge,args)}
