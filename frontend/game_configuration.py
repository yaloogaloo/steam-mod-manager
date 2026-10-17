"""Minimal existing game-path DAL editor; native picks authorize new directories."""
from dataclasses import asdict
from pathlib import Path
import json

FIELDS=('install_path','mod_path','workshop_path')
ACTIONS={'game-directory-pick','game-config-save','game-create'}


def state(bridge,args):
    bridge._command_game(args)
    config=bridge.adapter.db.get_game_deploy_config(int(args['game']))
    if config is None:raise LookupError('游戏配置不存在')
    data=asdict(config)
    return {'config':data,'version':bridge._version(data),'writable':bridge.adapter.writable}


def check_paths(bridge,args):
    if args.get('creating'):
        bridge._command_game(args);current={field:'' for field in FIELDS}
    else:current=state(bridge,args)['config']
    paths=args.get('paths')
    if not isinstance(paths,dict) or set(paths)!={'install_path','mod_path'}:raise ValueError('路径字段无效')
    for field,value in paths.items():
        if not isinstance(value,str) or len(value)>4000:raise ValueError('路径字段无效')
        if value and value!=current[field] and str(Path(value).resolve())!=bridge._directory_choices.get((str(args['game']),field)):
            raise ValueError('请使用目录选择按钮指定新路径')
    from services.game_path_validation import validate_deploy_paths
    return {'errors':validate_deploy_paths(paths['install_path'],paths['mod_path'])}


def pick(bridge,request_id,args):
    from PySide6.QtWidgets import QFileDialog,QApplication
    from services.business_resources import checked_resource_path
    bridge._command_game(args)
    field=args.get('field')
    if field not in FIELDS:raise ValueError('不支持的游戏路径字段')
    config={field:'' for field in FIELDS} if args.get('creating') else state(bridge,args)['config']
    selected=QFileDialog.getExistingDirectory(QApplication.activeWindow(),'选择游戏目录',config[field] or '')
    bridge._command_game(args)
    if selected:
        path=Path(selected)
        checked_resource_path(path.resolve(),path)
        if len(path.resolve().parts)<3:raise ValueError('不能使用磁盘根目录')
        bridge._directory_choices[(str(args['game']),field)]=str(path.resolve())
    bridge._busy.discard(args['_busy_key'])
    bridge.response.emit(json.dumps({'request':request_id,'result':{'path':selected,'cancelled':not bool(selected)}}))


def save(bridge,args):
    with bridge._lock,bridge.adapter.db._lock,bridge.resources.bind():
        current=state(bridge,args)
        if args.get('version')!=current['version']:raise RuntimeError('游戏配置已变化，请重新打开')
        if bridge._jobs:raise RuntimeError('业务任务尚未完成，不能修改游戏路径')
        values=args.get('paths')
        if not isinstance(values,dict) or set(values)!=set(FIELDS):raise ValueError('游戏路径字段不符合合同')
        name=args.get('name',current['config']['name'])
        strategy=args.get('deploy_type',current['config']['deploy_type'])
        from core.db_manager import SUPPORTED_DEPLOY_TYPES
        if not isinstance(name,str) or not name.strip() or len(name)>200 or any(ord(c)<32 for c in name):
            raise ValueError('请填写有效游戏名称（1–200 字符）')
        if strategy not in SUPPORTED_DEPLOY_TYPES:raise ValueError('不支持的部署类型')
        for field,value in values.items():
            if not isinstance(value,str):raise ValueError('路径必须为文本')
            if value==current['config'][field]:continue
            if not value:continue
            path=Path(value)
            # Only a result from the native picker can introduce a new path.
            if str(path.resolve())!=bridge._directory_choices.get((str(args['game']),field)):
                raise PermissionError('请使用目录选择按钮选择新路径')
            from services.business_resources import checked_resource_path
            checked_resource_path(path.resolve(),path)
            if not path.is_dir():raise ValueError('目录不存在')
            import os
            forbidden=[bridge.resources.root,bridge.resources.path('mod'),Path(os.environ.get('WINDIR','C:/Windows'))]
            if any(path.resolve().is_relative_to(p.resolve()) or p.resolve().is_relative_to(path.resolve()) for p in forbidden):
                raise PermissionError('游戏路径不能覆盖程序、系统或归档库')
        bridge.adapter.db.update_game_deploy_config(int(args['game']),name=name.strip(),deploy_type=strategy,**values)
        return {'success':True,'message':'游戏路径已保存',**state(bridge,args)}


def create(bridge,args):
    from core.db_manager import SUPPORTED_DEPLOY_TYPES
    with bridge._lock,bridge.adapter.db._lock,bridge.resources.bind():
        bridge._command_game(args)
        if bridge._jobs:raise RuntimeError('请等待当前任务完成')
        raw=args.get('app_id');name=args.get('name');strategy=args.get('deploy_type','folder_copy')
        if not isinstance(raw,str) or not raw.isascii() or not raw.isdigit() or not 0<int(raw)<=2147483647:
            raise ValueError('请填写有效的正整数 Steam AppID')
        if not isinstance(name,str) or not name.strip() or len(name)>200 or any(ord(c)<32 for c in name):raise ValueError('请填写有效游戏名称（1–200 字符）')
        if strategy not in SUPPORTED_DEPLOY_TYPES:raise ValueError('不支持的部署类型')
        if bridge.adapter.db.get_game_deploy_config(int(raw)) is not None:raise ValueError('此 AppID 已存在，请在游戏配置中修改')
        paths=args.get('paths')
        if not isinstance(paths,dict) or set(paths)!=set(FIELDS):raise ValueError('路径字段不完整')
        for field,value in paths.items():
            if not isinstance(value,str):raise ValueError('非法路径')
            if value:
                from services.business_resources import checked_resource_path
                path=Path(value);checked_resource_path(path.resolve(),path)
                if str(path.resolve())!=bridge._directory_choices.get((str(args['game']),field)) or not path.is_dir():raise PermissionError('请选择有效目录')
                import os
                forbidden=[bridge.resources.root,bridge.resources.path('mod'),Path(os.environ.get('WINDIR','C:/Windows'))]
                if len(path.resolve().parts)<3 or any(path.resolve().is_relative_to(p.resolve()) or p.resolve().is_relative_to(path.resolve()) for p in forbidden):raise PermissionError('不能使用系统、应用或归档目录作为游戏目录')
        bridge.adapter.db.update_game_deploy_config(int(raw),name=name.strip(),deploy_type=strategy,**paths)
        return {'success':True,'game':str(int(raw)),'message':'游戏已创建；部署能力按实际游戏和策略单独检查'}
