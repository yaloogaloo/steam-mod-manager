"""Optional exclusive-archive cleanup around the existing registration service.

CAS and external backups are never garbage-collected here. A same-volume
quarantine and durable journal preserve an explicit recovery path on failure.
"""
import json
import os
import shutil
import uuid
from pathlib import Path

from services.business_resources import checked_resource_path


def exclusive_archive(db, resources, mod_id):
    from services.path_lifecycle import resolve_managed_folder
    if resources.db is not db:
        raise PermissionError('归档资源与登记数据库不属于同一业务上下文')
    row = db.get_mod_backup_row(mod_id) or {}
    status = db._conn.execute('SELECT deploy_status FROM mods WHERE mod_id=?', (mod_id,)).fetchone()
    if not status or status[0] != 'not_deployed':
        raise PermissionError('仅明确未部署的 Mod 可以清理归档')
    root = resources.path('mod')
    path = resolve_managed_folder(row.get('internal_id', ''), library_root=root, db=db).path
    if path is None:
        raise PermissionError('无法用 Frozen UUID 确认独占归档')
    path = checked_resource_path(root, path)
    if path == root or len(path.relative_to(root).parts) < 2:
        raise PermissionError('不能清理资源库或游戏公共目录')
    if path.name == '.smm-unregister':
        raise PermissionError('不能清理恢复目录')
    for other in db._conn.execute('SELECT mod_id,last_known_path,cover_path,backup_cover_path,backup_offline_path,deploy_path FROM mods'):
        other = dict(other)
        if str(other.get('mod_id')) == str(mod_id):
            continue
        for key in ('last_known_path', 'cover_path', 'backup_cover_path', 'backup_offline_path', 'deploy_path'):
            value = other.get(key)
            if value:
                other_path = Path(value).resolve()
                if other_path.is_relative_to(path) or (key == 'last_known_path' and path.is_relative_to(other_path)):
                    raise PermissionError('归档与其他 Mod 的路径或封面共享，不能清理')
    for game in db.list_games():
        config = db.get_game_deploy_config(game.app_id)
        for raw in (config.install_path, config.mod_path, config.workshop_path):
            if raw:
                target = Path(raw).resolve()
                if path.is_relative_to(target) or target.is_relative_to(path):
                    raise PermissionError('归档与游戏或 Workshop 目录重叠，不能清理')
    files = []
    for item in path.rglob('*'):
        checked_resource_path(root, item)
        if item.name == 'metadata.json' and item.parent.name == '.info' and item.parent.parent != path:
            raise PermissionError('归档包含嵌套的 Mod 资产目录，不能清理')
        if item.is_file():
            if item.stat().st_nlink > 1:
                raise PermissionError('归档含共享硬链接，不能清理')
            files.append(item)
    return path, files


def remove_with_archive(service, resources, mod_id):
    db = service._database()
    with db._lock:
        path, files = exclusive_archive(db, resources, mod_id)
        from services.mutation_context import assert_mutation_allowed
        assert_mutation_allowed(path)
        # Opening all files without sharing on Windows rejects occupied inputs
        # before moving anything. Handles are released before the directory move.
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            create.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handles = []
            try:
                for item in files:
                    handle = create(str(item), 0x80000000, 0, None, 3, 0, None)
                    if handle == ctypes.c_void_p(-1).value:
                        raise OSError(ctypes.get_last_error(), '归档文件被占用或不可读取：'+str(item))
                    handles.append(handle)
            finally:
                for handle in handles:
                    kernel.CloseHandle(handle)
        base = resources.path('mod/.smm-unregister') / uuid.uuid4().hex
        base.mkdir(parents=True)
        staged = base / 'archive'
        journal = base / 'operation.json'
        record = {'mod_pk': str(mod_id), 'internal_id': (db.get_mod_backup_row(mod_id) or {})['internal_id'], 'original': str(path), 'staged': str(staged),
                  'files': [str(f.relative_to(path)) for f in files], 'state': 'prepared'}
        def persist(state):
            record['state'] = state
            pending = journal.with_suffix('.tmp')
            with pending.open('w', encoding='utf-8') as stream:
                json.dump(record, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            pending.replace(journal)
        persist('prepared')
        path.rename(staged)
        try:
            persist('staged')
            result = service.remove_mod_registration(mod_id)
            if not result.success:
                raise RuntimeError(result.error)
        except Exception:
            db._conn.rollback()
            try:
                staged.rename(path)
            except OSError as restore_error:
                raise RuntimeError('数据库注销失败；归档恢复受阻，文件保留于 '+str(staged)+'；恢复记录：'+str(journal)) from restore_error
            persist('rolled-back')
            raise
        try:
            persist('registration-removed')
            # Revalidate the actual staged tree immediately before recursive removal.
            checked_resource_path(resources.path('mod'), staged)
            for item in staged.rglob('*'):
                checked_resource_path(resources.path('mod'), item)
            shutil.rmtree(staged)
            persist('complete')
        except Exception as exc:
            raise RuntimeError('登记已注销，但独占归档清理未完成；剩余文件保留于 '+str(staged)+
                               '，恢复记录：'+str(journal)+'；请勿重新导入覆盖。原因：'+str(exc)) from exc
        result.filesystem_touched = True
        result.details.update({'removed_files': record['files'], 'journal': str(journal)})
        return result


def recover_pending(service, resources, operation_id):
    """Resume one recorded operation; restore files if registration survived.

    Not a WebChannel file API. Callers supply only the generated operation UUID.
    Repeated calls are safe; no CAS or external resource is ever removed.
    """
    token = uuid.UUID(operation_id).hex
    base = resources.path('mod/.smm-unregister') / token
    journal = checked_resource_path(resources.path('mod'), base / 'operation.json')
    record = json.loads(journal.read_text('utf-8'))
    staged = checked_resource_path(resources.path('mod'), base / 'archive')
    original = checked_resource_path(resources.path('mod'), record['original'])
    db = service._database()
    from services.mutation_context import assert_mutation_allowed
    assert_mutation_allowed(staged, original)
    with db._lock:
        row = db.get_mod_backup_row(record['mod_pk'])
        if row and row['internal_id'] != record['internal_id']:
            raise PermissionError('登记身份已变化，禁止恢复或清理')
        if staged.exists():
            for item in staged.rglob('*'):
                checked_resource_path(resources.path('mod'), item)
            if row:
                if original.exists():
                    raise PermissionError('原归档位置已被占用，保留暂存文件')
                staged.rename(original)
            else:
                shutil.rmtree(staged)
        record['state'] = 'rolled-back' if row else 'complete'
        pending = journal.with_suffix('.tmp')
        with pending.open('w', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(journal)
        return record
