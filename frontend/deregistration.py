"""Registration-only removal through the original service; no file deletion."""
def state(bridge,args):
    asset,info=bridge._command_asset(args)
    db=bridge.adapter.db
    row=db.get_mod_backup_row(asset.mod_pk)
    status=db._conn.execute('SELECT deploy_status FROM mods WHERE mod_id=?',(asset.mod_pk,)).fetchone()[0]
    reason=('只读模式：不能注销记录' if not bridge.adapter.writable else
            '未绑定可写业务资源' if bridge.resources is None else
            '请先安全撤出部署；未知或失败状态需先核实恢复情况。' if status!='not_deployed' else '')
    cleanup_reason=reason
    cleanup_files=[]
    if not cleanup_reason:
        try:
            from services.registration_archive_cleanup import exclusive_archive
            path,files=exclusive_archive(db,bridge.resources,asset.mod_pk)
            cleanup_files=[str(p.relative_to(path)) for p in files]
        except (OSError,ValueError,PermissionError) as exc:
            cleanup_reason=str(exc)
    return {'internal_id':asset.internal_id,'name':asset.name,'game':db.get_game(info.app_id).name,
            'platform':info.platform,'source_id':str(info.external_id or asset.workspace_id),
            'path':asset.managed_path,'deploy_status':status,
            'allowed':not bool(reason),'reason':reason,
            'cleanup_allowed':not bool(cleanup_reason),'cleanup_reason':cleanup_reason,'cleanup_files':cleanup_files,
            'version':bridge._version([row,status])}


def execute(bridge,args):
    with bridge._lock,bridge.adapter.db._lock,bridge.resources.bind():
        current=state(bridge,args)
        if not current['allowed']:raise PermissionError(current['reason'])
        if args.get('version')!=current['version']:raise RuntimeError('Mod 状态已变化，请重新确认')
        if args.get('confirm') is not True or not isinstance(args.get('keep_files'),bool):
            raise ValueError('请明确确认注销方式')
        cleanup=not args['keep_files']
        if cleanup and not current['cleanup_allowed']:raise PermissionError(current['cleanup_reason'])
        asset,_=bridge._command_asset(args)
        from services.registration_removal import ModRegistrationRemovalService
        result=ModRegistrationRemovalService(db=bridge.adapter.db).remove_mod_registration(asset.mod_pk,cleanup_resources=bridge.resources if cleanup else None)
        if not result.success:raise RuntimeError(result.error or '注销失败')
        return {'success':True,'internal_id':asset.internal_id,'game':str(args['game']),
                'message':('已注销并清理独占归档；共享 CAS、外部备份、Workshop 和游戏文件未清理。' if cleanup else '已从 SMM 注销；本地归档、CAS、备份和 Workshop 订阅文件均保留。'),
                'removed_files':result.details.get('removed_files',[])}
