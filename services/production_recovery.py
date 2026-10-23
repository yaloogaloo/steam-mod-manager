"""Require the independently verified recovery point before production writes."""
import hashlib
import json
from pathlib import Path


def require_recovery_point(root, library):
    pointer=Path(root)/'config/production_recovery.json'
    if not pointer.is_file():
        raise RuntimeError('缺少生产恢复点。请先运行 tools/production_restore_point.py <_tmp/protected_recovery/任务标识>；不会开放写入。')
    record=json.loads(pointer.read_text('utf-8'))
    receipt=Path(record['receipt'])
    resolved_root = Path(root).resolve()
    protected = resolved_root / '_tmp' / 'protected_recovery'
    for candidate in (receipt, *receipt.parents):
        if candidate.exists() and (candidate.is_symlink() or getattr(candidate.lstat(), 'st_file_attributes', 0) & 0x400):
            raise PermissionError('恢复点不能经过链接或 junction')
    if receipt.resolve().is_relative_to(resolved_root) and not receipt.resolve().is_relative_to(protected):
        raise PermissionError('项目内恢复点只能位于 _tmp/protected_recovery')
    payload=receipt.read_bytes()
    if hashlib.sha256(payload).hexdigest()!=record['sha256']:
        raise PermissionError('恢复点校验失败')
    saved=json.loads(payload)
    if not saved.get('complete') or Path(saved['root']).resolve()!=Path(root).resolve() or Path(saved['library']).resolve()!=Path(library).resolve():
        raise PermissionError('恢复点不属于当前生产资源')
    if not Path(saved['database']).is_file():
        raise FileNotFoundError('恢复点 SQLite 已丢失')
    return saved
