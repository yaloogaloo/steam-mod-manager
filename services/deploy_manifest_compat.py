"""Narrow v2 numeric-identity compatibility. No manifest or status writes."""
import hashlib
import json
from pathlib import Path
import zipfile


def digest(stream):
    value=hashlib.sha256()
    for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def validate_legacy_v2(manifest, ctx, db, *, verify_files):
    """A v2 PK stamp is accepted only for its proven current entity and files.

    v0/v1 omitted backup ownership, so they remain read-only evidence. No
    workspace-id guessing or absolute target drive is admitted as authority.
    """
    from services.path_lifecycle import _folder_proves_mod_entity
    from services.deploy_paths import remap_manifest_targets
    from services.deploy_security import validate_manifest_targets,collect_allowed_target_roots
    from services.backup_manager import BackupManager
    from services.business_resources import checked_resource_path
    managed=ctx.library_folder();pk=str(ctx.mod_pk)
    from services.deploy_rules.manifest import manifest_path_for
    raw=json.loads(manifest_path_for(managed).read_text(encoding='utf8'))
    if (not isinstance(raw,dict) or not isinstance(raw.get('files'),list) or not manifest.files
        or len(raw['files'])!=len(manifest.files)
        or any(not isinstance(item,dict) or 'backup' not in item for item in raw['files'])):
        raise PermissionError('历史 Manifest 未明确记录覆盖前备份；拒绝撤出')
    for item in raw['files']:
        backup=item['backup']
        if backup is not None and (not isinstance(backup,dict) or not backup.get('path') or not backup.get('hash')):
            raise PermissionError('历史备份信息不完整；拒绝撤出')
    if manifest.schema_version!=2 or str(manifest.mod_id)!=pk:
        raise PermissionError('历史 Manifest 身份或版本未能验证；保留记录，拒绝写入')
    if manifest.internal_id not in ('',ctx.internal_id,pk):
        raise PermissionError('Manifest 声明了其他实体身份')
    if not _folder_proves_mod_entity(managed,mod_id=pk,db=db):
        raise PermissionError('归档目录未证明当前实体身份')
    remap_manifest_targets(manifest,ctx)
    roots=collect_allowed_target_roots(ctx)
    validate_manifest_targets(manifest,allowed_roots=roots)
    if verify_files:refuse_shared_history(manifest,ctx,db)
    manager=BackupManager(managed,internal_id=ctx.internal_id,mod_pk=pk)
    archives={}
    for entry in manifest.files:
        if entry.source_relative:
            source=checked_resource_path(managed,managed/entry.source_relative)
        else:
            source=checked_resource_path(managed,entry.source)
        target=Path(entry.target)
        for root in roots:
            if target.resolve().is_relative_to(root.resolve()):
                target=checked_resource_path(root,entry.target);break
        else:raise PermissionError('历史目标不属于原部署规则的允许路径')
        if entry.backup:
            backup=manager.resolve_backup_file(entry.backup)
            for root in manager._allowed_backup_roots():
                if backup.is_relative_to(root):checked_resource_path(root,backup);break
            if verify_files:manager.verify_backup_hash(entry.backup)
        if not verify_files:continue
        if not source.is_file() or not target.is_file():raise PermissionError('历史部署源或目标缺失；拒绝覆盖或撤出')
        if not entry.source_hash:raise PermissionError('历史部署缺少源哈希；保留待核验')
        if source not in archives:
            with source.open('rb') as stream:archives[source]=digest(stream)
        if archives[source]!=entry.source_hash:raise PermissionError('历史部署源已变化；拒绝覆盖或撤出')
        if entry.type=='archive':
            if source.suffix.lower()!='.zip':raise PermissionError('历史归档格式尚无只读内容核验器')
            with zipfile.ZipFile(source) as archive:
                # Exact canonical member only; no basename or suffix guessing.
                member=entry.relative.replace('\\','/')
                if archive.namelist().count(member)!=1:raise PermissionError('历史归档成员路径不明确')
                info=archive.getinfo(member)
                if info.file_size!=target.stat().st_size:raise PermissionError('历史部署目标大小已变化')
                with archive.open(info) as stream:expected=digest(stream)
        else:expected=archives[source]
        with target.open('rb') as stream:actual=digest(stream)
        if expected!=actual:raise PermissionError('历史部署目标内容已变化；拒绝覆盖或撤出')
    return manifest


def refuse_shared_history(manifest,ctx,db):
    """A history claim must not authorize removal of another Mod's target."""
    from services.deploy_rules.base import DeployContext
    from services.deploy_rules.manifest import load_manifest,manifest_path_for
    from services.deploy_paths import remap_entry_target
    targets={str(Path(entry.target).resolve()).casefold() for entry in manifest.files}
    for row in db.list_mod_list_items(game_id=ctx.app_id):
        if int(row['mod_pk'])==int(ctx.mod_pk) or not row['managed_path']:continue
        folder=Path(row['managed_path'])
        if not manifest_path_for(folder).is_file():continue
        other=load_manifest(folder)
        if other is None:raise PermissionError('同游戏历史清单无法解析，无法排除目标共享')
        display=db.get_mod_display_info(row['mod_pk'])
        peer=DeployContext(internal_id=row['internal_id'],source=folder,app_id=ctx.app_id,
            config=ctx.config,deploy_type=other.deploy_type,mod_pk=row['mod_pk'],
            custom_deploy_path=display.custom_deploy_path if display else '')
        for entry in other.files:
            candidates={str(Path(entry.target).resolve()).casefold()}
            try:candidates.add(str(Path(remap_entry_target(entry,peer).target).resolve()).casefold())
            except ValueError:pass
            if targets & candidates:
                raise PermissionError('历史目标被多个 Mod 声明；拒绝覆盖或撤出：'+entry.target)
