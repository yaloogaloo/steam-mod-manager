"""Read-only evidence, separate from canonical SQLite deployment membership."""
import json
from pathlib import Path
from services.deploy_rules import resolve_deploy_type
from services.deploy_rules.base import DeployContext
from services.deploy_rules.manifest import DeployManifest,manifest_path_for
from services.deploy_paths import remap_entry_target


def inspect_deployment(db, mod_pk):
    row=db.get_mod_backup_row(mod_pk)
    if row is None:return {'state':'unknown','message':'状态无法确认：记录不存在'}
    config=db.get_game_deploy_config(row['app_id'])
    with db._lock:
        status=db._conn.execute('SELECT deploy_status FROM mods WHERE mod_id=?',(mod_pk,)).fetchone()
    recorded=str(status[0] or '') if status else ''
    if recorded not in ('deployed','not_deployed'):
        return {'recorded':recorded,'state':'unknown','message':'部署登记失败或未知；磁盘状态尚未确认'}
    if config is None:return {'state':'unknown','recorded':recorded,'message':'状态无法确认：缺少游戏配置'}
    strategy=resolve_deploy_type(row['app_id'],config.deploy_type)
    display=db.get_mod_display_info(mod_pk)
    custom=display.custom_deploy_path if display else ''
    if custom:strategy='custom_path'
    if strategy=='warhammer3_pack':
        from services.wh3_activation import workshop_content_root
        try:
            root=workshop_content_root(config.workshop_path)
            native=root/str(row['workspace_id']) if root is not None else None
            present=native is not None and native.is_dir() and any(native.rglob('*.pack'))
            return {'recorded':recorded,'backend':strategy,'state':'native_present' if present else 'unknown',
                    'message':'原生 Workshop 包存在；加载配置与游戏加载仍需核验' if present else '原生 Workshop 包缺失或无法解析；保留部署登记'}
        except OSError as exc:return {'recorded':recorded,'state':'unknown','message':'状态无法确认：'+str(exc)}
    if strategy=='paradox_launcher':return inspect_paradox(db,row,recorded)
    if not row.get('last_known_path'):return {'recorded':recorded,'state':'unknown','message':'状态无法确认：缺少归档路径'}
    managed=Path(row['last_known_path'])
    path=manifest_path_for(managed)
    result={'recorded':recorded,'backend':strategy,'manifest':str(path),'state':'unverified','message':'部署记录尚未验证磁盘；不代表游戏已加载'}
    try:
        if not path.is_file():
            if recorded=='deployed':result.update(state='unknown',message='已登记部署；未找到复制清单，需按原 Backend 核验')
            else:result.update(state='not_deployed',message='未登记部署；未发现历史复制清单')
            return result
        raw=json.loads(path.read_text(encoding='utf8'))
        if not isinstance(raw,dict) or not isinstance(raw.get('files'),list):raise ValueError('Manifest Schema 无效')
        manifest=DeployManifest.from_dict(raw)
        if not manifest.files:raise ValueError('Manifest 未包含可核对文件')
        aliases={str(mod_pk),str(row['internal_id']),str(row.get('workspace_id') or '')}
        if str(manifest.mod_id) not in aliases or (manifest.internal_id and manifest.internal_id not in aliases):
            raise ValueError('Manifest 声明了其他实体身份')
        ctx=DeployContext(internal_id=row['internal_id'],source=managed,app_id=row['app_id'],config=config,deploy_type=strategy,mod_pk=mod_pk,custom_deploy_path=custom)
        existing=missing=unresolved=relocated=0
        for entry in manifest.files:
            try:
                target=Path(remap_entry_target(entry,ctx).target)
                relocated+=str(target).casefold()!=str(Path(entry.target)).casefold()
                if target.is_file():existing+=1
                else:missing+=1
            except (OSError,ValueError):unresolved+=1
        result.update(schema=manifest.schema_version,files=len(manifest.files),existing=existing,missing=missing,unresolved=unresolved,relocated=relocated)
        if missing or unresolved:result.update(state='inconsistent',message=f'记录与磁盘不一致：{existing} 存在、{missing} 缺失、{unresolved} 路径待确认')
        elif manifest.schema_version<2 or manifest.internal_id!=row['internal_id'] or recorded!='deployed':
            result.update(state='compatibility_required',message=f'历史 Manifest 需兼容核验：{existing} 个目标存在；未改部署记录，内容与备份仍需核验')
        else:result.update(state='files_present',message=f'已登记部署，{existing} 个目标存在；尚未证明内容或游戏加载')
    except (OSError,ValueError,TypeError,KeyError) as exc:result.update(state='unknown',message='部署状态无法确认：'+str(exc))
    return result


def inspect_paradox(db,row,recorded):
    from services.paradox_activation import (resolve_paradox_user_dir,list_installed_paradox_mods,
        map_to_launcher_id,parse_mod_descriptor,get_paradox_effective_state)
    aid=row['app_id'];docs=resolve_paradox_user_dir(db,app_id=aid)
    result={'recorded':recorded,'backend':'paradox_launcher','state':'unverified','game_load_verified':False}
    try:
        ref=next((r for r in list_installed_paradox_mods(db,app_id=aid) if r.internal_id==row['internal_id']),None)
        if ref is None:raise ValueError('原 Backend 无法解析实体')
        mapping=map_to_launcher_id(ref,user_dir=docs);result['descriptor']=str(docs/mapping.launcher_id)
        if not mapping.available:raise ValueError('缺少或无法解析游戏描述符')
        data=parse_mod_descriptor((docs/mapping.launcher_id).read_text(encoding='utf8'))
        value=data.get('path') or data.get('archive') or '';target=Path(value) if Path(value).is_absolute() else docs/value
        present=target.is_dir() if data.get('path') else target.is_file()
        if not value or not present:raise ValueError('描述符指向的本体不存在')
        payload=json.loads((docs/'dlc_load.json').read_text(encoding='utf8'))
        if not isinstance(payload,dict) or not isinstance(payload.get('enabled_mods'),list):raise ValueError('加载配置格式无效')
        direct=mapping.launcher_id in payload['enabled_mods']
        effective=get_paradox_effective_state(db,app_id=aid,user_dir=docs)
        result.update(direct_enabled=direct,active_playset=effective.active_playset_id,launcher_enabled=mapping.launcher_id in effective.effective_launcher_ids)
        if effective.read_error:raise ValueError(effective.read_error)
        if recorded=='deployed' and not direct:raise ValueError('已登记部署，但直接启动配置未包含此 Mod')
        if recorded=='deployed' and not result['launcher_enabled']:
            result.update(state='configuration_warning',message='文件与直接启动配置存在；Launcher 未启用此 Mod，游戏实际加载未验证')
        else:result.update(state='configuration_present',message='文件与配置已核对；游戏实际加载未验证')
    except (OSError,ValueError,TypeError) as exc:result.update(state='unknown',message='部署状态需核验：'+str(exc)+'；未修改游戏配置')
    return result
