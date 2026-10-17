"""Order workbench. Membership and projection remain existing Backend-owned."""
import hashlib
import json
from pathlib import Path
from uuid import UUID
from contextlib import nullcontext
from services.order_backend import get_order_backend

ACTIONS={'order-save','order-retry'}


def state(bridge,args):
    bridge._command_game(args)
    game=int(args['game'])
    backend=get_order_backend(game)
    assets=[asset for asset in bridge.adapter.assets(game) if asset.deployed]
    flags=[tuple(r) for r in bridge.adapter.db._conn.execute('SELECT mod_id,internal_id,deploy_status,enabled FROM mods WHERE app_id=? ORDER BY mod_id',(game,))]
    inactive={str(row[0]) for row in flags if not row[3]}
    resources=bridge.resources
    config=bridge.adapter.db.get_game_deploy_config(game)
    reason=''
    if backend is None:reason='当前游戏没有加载顺序 Backend'
    elif game in (1158310,281990,1086940) and not getattr(resources,'production',False):reason='此环境未绑定当前游戏的用户配置投影目标'
    elif resources is None or not bridge.adapter.writable:reason='只读模式：不允许保存或投影'
    order_path=projection=None
    saved=[]
    if game==1142710:
        data_parent=Path(bridge.adapter.db.db_path).resolve().parent
        base=resources.root if resources else data_parent.parent if data_parent.name.casefold()=='data' else None
        if resources:
            order_path=resources.path('config/load_order/wh3.json')
            if config and config.install_path:projection=resources.validate(Path(config.install_path)/'used_mods.txt')
        elif base is not None:
            order_path=base/'config/load_order/wh3.json'
        from services.wh3_activation import (WH3_STATE_DIRNAME,WH3_ORDER_FILENAME,WH3_LEGACY_ORDER_FILENAME,
            canonicalize_wh3_order_tokens,merge_deployed_order)
        read_paths=([order_path] if order_path.is_file() else [base/'data'/WH3_STATE_DIRNAME/WH3_ORDER_FILENAME,base/'data'/WH3_LEGACY_ORDER_FILENAME]) if order_path else []
        for read_path in read_paths:
            if resources:resources.validate(read_path)
            if not read_path.is_file():continue
            payload=json.loads(read_path.read_text('utf8'))
            saved=payload.get('order',[]) if isinstance(payload,dict) else payload
            if not isinstance(saved,list):raise ValueError('加载顺序 JSON 无效，请先修复文件')
            saved,_=canonicalize_wh3_order_tokens(saved,bridge.adapter.db)
            break
        order=merge_deployed_order(saved,[a.internal_id for a in assets])
    elif game in (1158310,281990):
        from services.paradox_activation import _order_path
        with resources.bind() if resources else nullcontext(), resources.game_projection(game) if getattr(resources,'production',False) else nullcontext():
            order_path=_order_path(game)
            order=backend.get_current_order(db=bridge.adapter.db)
            if getattr(resources,'production',False):
                from services.paradox_activation import DLC_LOAD_FILENAME
                projection=resources.projection_directory(game)/DLC_LOAD_FILENAME
    elif game==1086940 and getattr(resources,'production',False):
        from services.bg3_activation import bg3_order_path,bg3_modsettings_path
        with resources.bind(),resources.game_projection(game):
            order_path=bg3_order_path()
            projection=bg3_modsettings_path()
            resources.validate(order_path)
            order=backend.get_current_order(db=bridge.adapter.db,library_root=resources.path('mod'))
    else:order=[a.internal_id for a in assets]
    unresolved={}
    if game==1086940 and getattr(resources,'production',False):
        from services.bg3_activation import inspect_bg3_membership
        with resources.bind(),resources.game_projection(game):
            report=inspect_bg3_membership(bridge.adapter.db,library_root=resources.path('mod'))
        unresolved={item.internal_id:'已部署但资源无法投影：'+item.code+' · '+item.message for item in report.unresolved}
        unresolved.update({iid:'自定义部署成员：按原游戏规则不参与 PAK 投影' for iid in report.skipped_custom})
    rows=[]
    rank={iid:i for i,iid in enumerate(order)}
    for asset in sorted(assets,key=lambda a:rank.get(a.internal_id,10**9)):
        iid=asset.internal_id
        warning='已部署但未启用：按游戏既有规则不参与投影' if str(asset.mod_pk) in inactive else ''
        if iid in unresolved:warning=unresolved[iid]
        if resources and game==1142710:
            try:
                UUID(iid)
                # Ordering never deploys or extracts content. Deployment admission
                # must not disable canonical ordering of existing members.
                from services.wh3_activation import workshop_content_root
                root=workshop_content_root(config.workshop_path)
                native=resources.validate(root/asset.workspace_id)
                if native.is_dir():
                    for path in native.rglob('*'):resources.validate(path)
                if not native.is_dir() or not any(native.rglob('*.pack')):
                    warning='Workshop 包缺失：保留已部署成员，投影可能无法解析'
            except Exception as exc:
                warning='资源路径未获文件操作授权；保留已部署成员，由原 Backend 解析可投影资源：'+str(exc)
        token=next((t for t,a in bridge._assets.items() if a.internal_id==iid and bridge._snapshots.get(t)==bridge._generation),'')
        if not token:reason='资产快照已变化，请刷新后重试'
        rows.append({'internal_id':iid,'token':token,'name':asset.name,'platform':asset.platform,'warning':warning})
    order=[row['internal_id'] for row in rows]
    if game==1142710 and resources and (projection is None or not resources.game_target(config.install_path)):
        reason='缺少明确的游戏安装目录或 used_mods.txt 写入权限'
    with resources.game_projection(game) if game in (1158310,281990,1086940) and getattr(resources,'production',False) else nullcontext():
        projection_bytes=projection.read_bytes().hex() if projection and projection.is_file() else ''
    version=hashlib.sha256(json.dumps([game,order,flags,
        [(a.internal_id,a.mod_pk,a.managed_path) for a in assets],
        order_path.read_bytes().hex() if order_path and order_path.is_file() else '',
        projection_bytes,
        [config.install_path,config.mod_path,config.workshop_path] if config else None,
        resources.projection_version(game) if game in (1158310,281990,1086940) and getattr(resources,'production',False) else None],sort_keys=True).encode()).hexdigest()
    return {'game':str(game),'version':version,'rows':rows,'order':order,'enabled':not reason,
        'reason':reason,'backend':type(backend).__name__ if backend else None,
        'order_path':str(order_path or ''),'projection_path':str(projection or ''),
        'projection_error':bridge._order_errors.get(str(game),''),
        'retry_projection':bridge._order_errors.get(str(game),'').startswith('顺序 JSON 已保存')}


def execute(bridge,action,args):
    bridge.adapter._write()
    with bridge._lock,bridge.adapter.db._lock:
        current=state(bridge,args)
        if not current['enabled']:raise PermissionError(current['reason'])
        if args.get('version')!=current['version']:raise RuntimeError('已部署成员、顺序或投影文件已变化，请刷新后重试')
        resources=bridge.resources
        projection_scope=resources.game_projection(int(args['game']),backup=True) if int(args['game']) in (1158310,281990,1086940) and getattr(resources,'production',False) else nullcontext()
        with resources.bind(),projection_scope:
            backend=get_order_backend(int(args['game']))
            if set(backend.get_sortable_mods(db=bridge.adapter.db,library_root=resources.path('mod')))!=set(current['order']):
                raise RuntimeError('Backend 与当前游戏的已部署成员集合不一致，拒绝写入')
            if action=='order-save':
                tokens=args.get('order')
                if not isinstance(tokens,list) or len(tokens)!=len(set(tokens)) or set(tokens)!=set(current['order']):
                    raise ValueError('只能排序当前游戏完整的已部署 UUID 成员集合')
                for token in tokens:UUID(token)
                try:backend.persist_order(tokens,db=bridge.adapter.db,library_root=resources.path('mod'))
                except Exception as exc:
                    bridge._order_errors[str(args['game'])]='排序保存失败，请重新读取文件状态：'+str(exc)
                    raise RuntimeError(bridge._order_errors[str(args['game'])]) from exc
            try:
                projected=backend.sync_projection(db=bridge.adapter.db,library_root=resources.path('mod'))
                if projected is not None and getattr(projected,'errors',None):
                    raise RuntimeError('；'.join(str(e) for e in projected.errors))
            except Exception as exc:
                bridge._order_errors[str(args['game'])]='顺序 JSON 已保存，但游戏投影失败；请修复后重试投影：'+str(exc)
                raise RuntimeError(bridge._order_errors[str(args['game'])]) from exc
            bridge._order_errors.pop(str(args['game']),None)
        warning=any(row['warning'] for row in current['rows'])
        return {'game':str(args['game']),'success':True,'message':'顺序已保存；可用资源已投影，请查看成员警告' if warning else '顺序已保存并完成游戏投影'}
