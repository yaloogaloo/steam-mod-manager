"""Signature-driven, bounded archive admission for registration and completion.

7-Zip only emits member bytes to stdout. Python owns every destination path;
the decoder never receives an output directory or writes archive links.
"""
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import subprocess
import tempfile
import threading
import time

MAX_BYTES = 8 * 1024**3
MAX_FILE_BYTES = 8 * 1024**3
MAX_FILES = 65536
MEMORY_BYTES = 512 * 1024**2
DISK_RESERVE = 256 * 1024**2
ARCHIVE_FILTER = 'Archives (*.zip *.7z *.rar *.tar *.tar.gz *.tgz *.tar.bz2 *.tbz2 *.tar.xz *.txz *.tar.zst *.tzst *.gz *.bz2 *.xz *.zst *.cab *.cpio *.ar *.xar);;All files (*)'
STREAM_TYPES = {'gzip', 'bzip2', 'xz', 'zstd'}
SAFE_TYPES = {'zip', '7z', 'rar', 'rar5', 'tar', 'cab', 'cpio', 'ar', 'arj', 'lzh', 'xar'} | STREAM_TYPES


def storage_name(source, cancel_check=lambda:None):
    """Use the existing stored-path/original-name contract for mismatched suffixes."""
    source=Path(source);kind,_=inspect_archive(source,cancel_check=cancel_check)
    aliases={'rar5':('rar',),'gzip':('gz','tgz'),'bzip2':('bz2','tbz2'),'xz':('xz','txz'),'zstd':('zst','tzst')}.get(kind,(kind,))
    return source.name if source.suffix.lower().lstrip('.') in aliases else source.name+'.'+aliases[0]


def safe_member(name):
    value = str(name).replace('\\', '/')
    path = PurePosixPath(value)
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if (not path.parts or path.is_absolute() or PureWindowsPath(value).drive or '..' in path.parts
        or any(p.rstrip(' .') != p or ':' in p or p.split('.')[0].upper() in reserved
               or p.casefold() in ('.info', 'smm_info') or any(ord(c) < 32 for c in p) for p in path.parts)
        or path.name.casefold() in ('deploy_manifest.json', '.is_missing_content')):
        raise ValueError('不安全的归档路径：' + value)
    return path


@dataclass(frozen=True)
class Member:
    name: str
    directory: bool
    size: int | None


def _memory_job(process):
    """Hard process memory ceiling on the supported Windows desktop runtime."""
    if os.name != 'nt':
        raise RuntimeError('当前受控归档解码需要 Windows Job Object 资源限制')
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64), ('flags', w.DWORD),
                    ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t), ('active', w.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                    ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]; api.CreateJobObjectW.restype = w.HANDLE
    api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    api.CloseHandle.argtypes = [w.HANDLE]
    handle = api.CreateJobObjectW(None, None)
    limits = Extended(); limits.basic.flags = 0x2000 | 0x100; limits.process_memory = MEMORY_BYTES
    if not handle or not api.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not api.AssignProcessToJobObject(handle, w.HANDLE(int(process._handle))):
        if handle: api.CloseHandle(handle)
        raise RuntimeError('无法应用归档解码内存限额，已拒绝无界解压')
    return lambda: api.CloseHandle(handle)


def _decode(source, arguments, sink, limit, cancel_check):
    from services.importers.archive import find_7z_executable
    seven = find_7z_executable()
    if not seven: raise ValueError('需要可用的 7-Zip 解码组件；未找到 7z/7zz，未宣称支持该归档')
    command = [seven, *arguments, '-sccUTF-8', '-mmt=2', '-pSMM_NO_PASSWORD', '--', str(source)]
    from services.business_resources import archive_decoder
    with archive_decoder(command, source):
        process = subprocess.Popen(command, executable=seven, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    stopped = threading.Event(); errors = []; stderr = bytearray(); close_job = None
    def drain():
        while block := process.stderr.read(4096):
            stderr.extend(block[:max(0, 65536 - len(stderr))])
    def monitor():
        deadline = time.monotonic() + 600
        while not stopped.wait(.025):
            try:
                cancel_check()
                if time.monotonic() > deadline: raise ValueError('归档解码超过 10 分钟限额')
            except Exception as exc:
                errors.append(exc)
                if process.poll() is None: process.kill()
                return
    try:
        close_job = _memory_job(process)
        reader = threading.Thread(target=drain, daemon=True); reader.start()
        watcher = threading.Thread(target=monitor, daemon=True); watcher.start()
        written = 0
        while block := process.stdout.read(1024 * 1024):
            cancel_check(); written += len(block)
            if written > limit: raise ValueError('实际解包内容超过限额')
            sink(block)
        code = process.wait(); reader.join(); stopped.set(); watcher.join()
        if errors: raise errors[0]
        if code:
            detail = stderr.decode('utf8', errors='replace').strip()[-1500:]
            message=detail.lower()
            if any(s in message for s in ('password','encrypted')):reason='加密归档不支持'
            elif code in (8,-1073741801) or any(s in message for s in ('not enough memory','cannot allocate','can not allocate')):reason='归档解码超过 512 MiB 内存限额'
            elif any(s in message for s in ('unsupported method','not supported')):reason='当前解码引擎不支持该归档方法'
            elif any(s in message for s in ('data error','crc failed','unexpected end')):reason='归档损坏或校验失败'
            else:reason='无法识别此归档或文件已损坏'
            raise ValueError(f'{reason}（{source.name} / {source.suffix.upper()}，7-Zip {code}）：{detail}')
        return written
    finally:
        stopped.set()
        if process.poll() is None: process.kill()
        process.wait()
        process.stdout.close(); process.stderr.close()
        if close_job: close_job()


def inspect_archive(source, *, max_bytes=MAX_BYTES, max_files=MAX_FILES, cancel_check=lambda: None):
    source = Path(source)
    if not source.is_file(): raise ValueError('归档文件不存在：' + str(source))
    if source.stat().st_size > max_bytes: raise ValueError('输入归档超过限额')
    data = bytearray()
    _decode(source, ['l', '-slt'], data.extend, 16 * 1024**2, cancel_check)
    text = data.decode('utf8', errors='strict').replace('\r', '')
    header, separator, entries = text.partition('----------\n')
    fields = dict(line.split(' = ', 1) for line in header.splitlines() if ' = ' in line)
    kind = fields.get('Type', '').lower()
    if not separator or kind not in SAFE_TYPES: raise ValueError('不是可安全导入的归档格式：' + (kind or source.name))
    if int(fields.get('Volumes', '1')) > 1: raise ValueError('分卷归档需先完整合并；多包导入不是分卷恢复')
    if kind == 'zip':
        import zipfile
        with zipfile.ZipFile(source) as archive:
            if any(stat.S_ISLNK(i.external_attr >> 16) or i.flag_bits & 1 for i in archive.infolist()):
                raise ValueError('不支持链接、特殊文件类型或加密归档')
    if kind in ('rar', 'rar5'):
        import rarfile
        with rarfile.RarFile(source) as archive:
            if archive.needs_password(): raise ValueError('加密 RAR 归档不支持')
            if any(i.is_symlink() or getattr(i, 'file_redir', None) for i in archive.infolist()): raise ValueError('不支持 RAR 链接')
    members = []; seen = set(); directories=set(); files=set(); total = count = 0
    for record in entries.strip().split('\n\n'):
        if not record.strip(): continue
        props = dict(line.split(' = ', 1) for line in record.splitlines() if ' = ' in line)
        if not props: continue  # 7-Zip may append a human-readable warning count.
        if 'Path' not in props and kind in STREAM_TYPES:
            props['Path'] = source.stem
        name = safe_member(props.get('Path', '')).as_posix(); key = name.casefold()
        mode = (props.get('Mode', '') + ' ' + props.get('Attributes', '')).split()
        if (props.get('Encrypted') == '+' or props.get('Symbolic Link') or props.get('Hard Link')
            or any(len(m) >= 10 and m[0] in 'lbcps' for m in mode)):
            raise ValueError('不支持链接、特殊文件类型或加密归档：' + name)
        directory = props.get('Folder') == '+' or any(m.startswith('D') or m.startswith('d') for m in mode)
        size = int(props['Size']) if props.get('Size', '').isdigit() else None
        if key in seen: raise ValueError('归档路径重名，未覆盖：' + name)
        parents={p.as_posix().casefold() for p in PurePosixPath(name).parents if p.parts}
        if parents & files or (not directory and key in directories): raise ValueError('归档文件与目录冲突：' + name)
        seen.add(key);directories.update(parents)
        if directory:directories.add(key)
        else:files.add(key)
        total += 0 if directory else size or 0
        count += not directory
        if len(seen) > max_files * 2 or count > max_files or total > max_bytes:
            raise ValueError('归档内容体积或文件数超过限额')
        members.append(Member(name, directory, size))
    if not any(not m.directory for m in members): raise ValueError('归档没有内容文件')
    return kind, members


def extract_archive(source, destination, *, max_bytes=MAX_BYTES, max_file_bytes=MAX_FILE_BYTES,
                    max_files=MAX_FILES, cancel_check=lambda: None, _depth=0):
    """Only an explicitly owned empty stage is accepted; partial output is removed."""
    source = Path(source); destination = Path(destination)
    from services.mutation_context import assert_mutation_allowed
    assert_mutation_allowed(destination)
    if destination.exists() and any(destination.iterdir()): raise ValueError('解包目录必须为空，不能覆盖已有内容')
    destination.mkdir(parents=True, exist_ok=True)
    try:
        kind, members = inspect_archive(source, max_bytes=max_bytes, max_files=max_files, cancel_check=cancel_check)
        expected = sum(m.size or 0 for m in members if not m.directory)
        if shutil.disk_usage(destination).free < expected + DISK_RESERVE: raise ValueError('暂存空间不足，保留 256 MiB 磁盘余量')
        total = 0
        for member in members:
            cancel_check(); target = destination / member.name
            if member.directory: target.mkdir(parents=True, exist_ok=True); continue
            target.parent.mkdir(parents=True, exist_ok=True)
            def write(block):
                if shutil.disk_usage(destination).free < len(block) + DISK_RESERVE: raise ValueError('暂存磁盘空间不足')
                output.write(block)
            # -i! with -spd makes the member an exact literal, including leading '-'.
            with target.open('xb') as output:
                size = _decode(source, ['x', '-so', '-spd', '-i!' + member.name.replace('/', os.sep)], write,
                               min(max_file_bytes, max_bytes - total), cancel_check)
            if member.size is not None and size != member.size: raise ValueError('解包长度与归档目录不一致：' + member.name)
            total += size
        if kind in STREAM_TYPES and _depth == 0:
            files = [destination / m.name for m in members if not m.directory]
            if len(files) != 1: raise ValueError('单文件压缩流的成员数量异常')
            # A compressed TAR is a container; an ordinary compressed file stays one file.
            import tarfile
            try:
                with tarfile.open(files[0], mode='r:'): is_tar = True
            except tarfile.TarError:
                is_tar = False
            if is_tar:
                owned=Path(tempfile.mkdtemp(prefix='tar-stream-',dir=destination.parent));inner=owned/'payload'
                try:
                    extract_archive(files[0], inner, max_bytes=max_bytes, max_file_bytes=max_file_bytes,
                                    max_files=max_files, cancel_check=cancel_check, _depth=1)
                    shutil.rmtree(destination); inner.rename(destination)
                finally:
                    shutil.rmtree(owned)
        cancel_check()
        return destination
    except Exception:
        if destination.exists(): shutil.rmtree(destination)
        raise
