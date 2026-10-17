"""Thin adapter for the original game-scoped Type ID catalog and DAL."""
from dataclasses import asdict
from pathlib import Path
from services.mod_type_catalog import ModTypeCatalog

ACTIONS={'type-set','type-create','type-delete'}


def catalog(bridge):
    store=ModTypeCatalog(Path(bridge.adapter.db.db_path).parent/'mod_types.json')
    store.reload()
    return store


def state(bridge,args):
    bridge._command_game(args)
    store=catalog(bridge)
    types=[asdict(t) for t in store.list_types(int(args['game']))]
    return {'types':types,'version':bridge._version(types)}


def execute(bridge,action,args):
    with bridge._lock,bridge.adapter.db._lock,bridge.resources.bind():
        bridge._command_game(args)
        asset,_=bridge._command_asset(args)
        current=state(bridge,args)
        if args.get('version')!=current['version']:raise RuntimeError('类型目录已变化，请重新打开编辑器')
        store=catalog(bridge);game=int(args['game'])
        raw=args.get('type_id')
        if action!='type-create':
            if raw is not None and (type(raw) is not int or store.get(game,raw) is None):
                raise ValueError('类型不存在或不属于当前游戏')
        if action=='type-set':
            bridge.adapter.db.set_mod_type_id(asset.mod_pk,raw)
        else:
            bridge.resources.validate_write(store.path)
            if action=='type-create':
                name=args.get('name')
                if not isinstance(name,str) or not name.strip() or len(name)>100 or any(ord(c)<32 for c in name):
                    raise ValueError('类型名称须为 1–100 个字符')
                store.add_type(game,name.strip())
            elif action=='type-delete':
                if raw is None or args.get('confirm') is not True:raise ValueError('请确认删除类型并解除本游戏成员绑定')
                store.delete_type(game,raw,bridge.adapter.db)
            else:raise PermissionError('未知类型操作')
        return {'success':True,'message':'类型已保存','selected_type_id':bridge.adapter.db.get_mod_type_id(asset.mod_pk),**state(bridge,args)}
