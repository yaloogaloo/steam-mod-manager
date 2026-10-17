"""Deployment record UI boundary. No deployment, ordering or resource projection."""
from dataclasses import asdict
from uuid import UUID
from services import deployment_record as records

ACTIONS = {'snapshot-save', 'snapshot-rename', 'snapshot-delete'}


def state(bridge, args):
    bridge._command_game(args)
    db = bridge.adapter.db
    game = int(args['game'])
    assets = {str(a.mod_pk): a for a in bridge.adapter.assets(game)}
    current = {pk for pk, asset in assets.items() if asset.deployed}
    listed = []
    memberships = {}
    for record in records.list_records(game, db=db):
        members = records.get_record_mod_ids(record.id, db=db)
        memberships[record.id] = members
        listed.append({**asdict(record), 'count': len(members)})
    version = bridge._version([listed, {str(k): sorted(v) for k, v in memberships.items()}, sorted(current)])
    selected = args.get('record_id')
    comparison = None
    if selected is not None:
        if type(selected) is not int or selected not in memberships:
            raise ValueError('快照不存在或不属于当前游戏')
        saved = memberships[selected]
        def rows(ids):
            result = []
            for pk in sorted(ids, key=int):
                asset = assets.get(pk)
                if asset:
                    try:
                        UUID(asset.internal_id)
                    except ValueError:
                        asset = None
                result.append({'mod_pk': pk, 'internal_id': asset.internal_id if asset else None,
                    'name': asset.name if asset else f'无法解析的历史成员 · PK {pk}',
                    'platform': asset.platform if asset else '成员已删除、移出当前游戏或身份无法解析',
                    'missing': asset is None})
            return result
        comparison = {'common': rows(saved & current), 'snapshot_only': rows(saved - current),
                      'current_only': rows(current - saved)}
    return {'game': str(game), 'records': listed, 'version': version, 'comparison': comparison,
            'current_count': len(current), 'enabled': bool(bridge.adapter.writable and bridge.resources),
            'reason': '' if bridge.adapter.writable and bridge.resources else '只读模式：不能修改部署快照'}


def execute(bridge, action, args):
    bridge.adapter._write()
    if bridge.resources is None:
        raise PermissionError('缺少明确的隔离资源上下文')
    db = bridge.adapter.db
    with bridge._lock, db._lock, bridge.resources.bind():
        if db._conn.in_transaction:
            raise RuntimeError('数据库存在未完成事务，拒绝快照写入')
        record_id = args.get('record_id')
        name = args.get('name')
        try:
            # Serialize capture/version validation with external SQLite writers too.
            db._conn.execute('BEGIN IMMEDIATE')
            current = state(bridge, args)
            if args.get('version') != current['version']:
                raise RuntimeError('快照或已部署成员已变化，请刷新后重试')
            if action != 'snapshot-save' and not any(r['id'] == record_id for r in current['records']):
                raise ValueError('快照不存在或不属于当前游戏')
            if action != 'snapshot-delete' and (not isinstance(name, str) or not name.strip() or len(name) > 120):
                raise ValueError('快照名称需要 1–120 个字符')
            if action == 'snapshot-save':
                existing = records.find_record_by_name(args['game'], name, db=db)
                if existing and args.get('overwrite') is not True:
                    raise ValueError('同名快照已存在，请明确确认覆盖')
                # Capture authority again under the same database lock, never accept client PKs.
                members = [a.mod_pk for a in bridge.adapter.assets(int(args['game'])) if a.deployed]
                record_id = records.create_or_update_record(args['game'], name, mod_ids=members, db=db).id
            elif action == 'snapshot-rename':
                records.rename_record(record_id, name, db=db)
            elif action == 'snapshot-delete':
                if not records.delete_record(record_id, db=db):
                    raise RuntimeError('快照删除失败')
            else:
                raise PermissionError('Unsupported snapshot command')
        except Exception:
            db._conn.rollback()
            raise
        return {'success': True, 'game': str(args['game']), 'record_id': record_id,
                'message': '快照已删除' if action == 'snapshot-delete' else '快照已保存'}
