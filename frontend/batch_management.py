"""Selected Mods use the original Type ID, platform-edit and membership writers."""
from .type_management import catalog

ACTIONS={'batch-type-set','batch-platform-set','batch-memberships'}


def selected(bridge,args):
    bridge._command_game(args)
    ids=args.get('internal_ids')
    if not isinstance(ids,list) or not ids or len(ids)>5000 or not all(isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):raise ValueError('请选择有效且不重复的 Mod')
    known={a.internal_id:a for a in bridge.adapter.assets(int(args['game']))}
    assets=[]
    for iid in ids:
        if not isinstance(iid,str) or iid not in known:raise ValueError('选择含其他游戏或不存在的 Mod')
        asset=known[iid];bridge.adapter.verify(asset)
        if iid in bridge._busy:raise RuntimeError('所选 Mod 有未完成操作，请等待')
        assets.append(asset)
    return assets


def state(bridge,args):
    assets=selected(bridge,args);db=bridge.adapter.db
    from dataclasses import asdict
    types=[asdict(t) for t in catalog(bridge).list_types(int(args['game']))]
    records=bridge.adapter.collections(int(args['game']))
    collections=[{'id':str(c.collection_id),'name':c.name,'version':bridge.collection_version(c)} for c in records]
    facts=[(a.internal_id,db.get_mod_type_id(a.mod_pk),db.get_mod_display_info(a.mod_pk).platform) for a in assets]
    return {'types':types,'collections':collections,'count':len(assets),'version':bridge._version([facts,types,collections])}


def execute(bridge,action,args):
    with bridge._lock,bridge.adapter.db._lock,bridge.resources.bind():
        current=state(bridge,args)
        if args.get('version')!=current['version']:raise RuntimeError('选择、类型或合集已经变化，请重新打开批量编辑')
        assets=selected(bridge,args);db=bridge.adapter.db
        if action=='batch-memberships':
            from services.collection import apply_collection_memberships
            add=args.get('add',[]);remove=args.get('remove',[])
            known={c['id'] for c in current['collections']}
            if not isinstance(add,list) or not isinstance(remove,list) or any(str(i) not in known for i in add+remove) or set(add)&set(remove):raise ValueError('合集选择无效')
            apply_collection_memberships(args['game'],[a.internal_id for a in assets],add,remove,db=db)
        else:
            if action=='batch-type-set':
                tid=args.get('type_id')
                if tid is not None and (type(tid) is not int or catalog(bridge).get(int(args['game']),tid) is None):raise ValueError('类型不属于当前游戏')
            elif action=='batch-platform-set':
                from core.mod_platform import get_available_sources,normalize_platform_if_known
                game=db.get_game(int(args['game']));platform=normalize_platform_if_known(args.get('platform'))
                if platform not in {p for p,label in get_available_sources(game.name,int(args['game']))}:raise ValueError('该游戏不支持此来源')
            else:raise PermissionError('未知批量编辑动作')
            completed=0
            try:
                for asset in assets:
                    if action=='batch-type-set':db.set_mod_type_id(asset.mod_pk,tid)
                    else:
                        info=db.get_mod_display_info(asset.mod_pk)
                        db.update_mod_user_metadata(asset.mod_pk,{'display_name':info.user_display_name,'custom_description':info.custom_description,'user_notes':info.user_notes,'favorite':info.favorite,'platform':platform})
                        from pathlib import Path
                        if Path(asset.managed_path).is_dir():
                            from services.info_sidecar import write_sidecar_for_mod
                            if write_sidecar_for_mod(bridge.adapter.folder(asset),asset.mod_pk,db=db) is None:raise RuntimeError('元数据侧车保存失败')
                        else:
                            from services.mod_presence import persist_entity_metadata_to_backup
                            persist_entity_metadata_to_backup(asset.mod_pk,db=db)
                    completed+=1
            except Exception as exc:raise RuntimeError(f'已完成 {completed}/{len(assets)} 项；当前项可能已保存数据库，请刷新确认。失败原因：{exc}') from exc
        return {'success':True,'message':f'已保存 {len(assets)} 个 Mod；未执行部署或改变加载顺序'}
