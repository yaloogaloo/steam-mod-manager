"""Bounded multi-package admission around the existing archive extractor.

Steam contributes extracted paths from each archive root. Other sources retain
one original archive per source unit, as ArchiveImporter already does. Paths
are never flattened; duplicates (including case-insensitive ones) are refused.
Nothing is published here; the caller commits once against the original entity.
"""
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
import hashlib
import shutil
import stat
import zipfile
from uuid import uuid4

MAX_FILES = 4096
MAX_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024


def safe_member(name):
    value = str(name).replace('\\', '/')
    path = PurePosixPath(value)
    reserved = {'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}
    if (not path.parts or path.is_absolute() or PureWindowsPath(value).drive or '..' in path.parts
        or any(p.rstrip(' .') != p or ':' in p or p.split('.')[0].upper() in reserved
               or p.casefold() in ('.info','smm_info') or any(ord(c)<32 for c in p) for p in path.parts)
        or path.name.casefold() in ('deploy_manifest.json','.is_missing_content')):
        raise ValueError('不安全的归档路径：'+value)
    return path


def members(source):
    suffix = source.suffix.lower()
    if suffix == '.zip':
        with zipfile.ZipFile(source) as archive:
            for item in archive.infolist():
                if stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
                    raise ValueError('不支持链接或加密归档')
                if item.file_size > max(item.compress_size, 1) * 1000:
                    raise ValueError('压缩比例超过限额')
                yield item.filename, item.is_dir(), item.file_size
    elif suffix == '.7z':
        import inspect
        import py7zr
        if 'max_extract_size' not in inspect.signature(py7zr.SevenZipFile).parameters:
            raise ValueError('当前 py7zr 缺少受限解包能力')
        with py7zr.SevenZipFile(source) as archive:
            if archive.needs_password():raise ValueError('不支持加密归档')
            for item in archive.list():
                if item.is_symlink or not (item.is_file or item.is_directory):
                    raise ValueError('不支持链接或特殊归档成员')
                yield item.filename, item.is_directory, item.uncompressed
    elif suffix == '.rar':
        raise ValueError('RAR 补齐尚缺受控、限额的解压组件；当前不支持')
    else:
        raise ValueError('不支持的补齐格式：'+suffix)


def digest(path, cancel_check):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        while block:=stream.read(1024*1024):
            cancel_check();result.update(block)
    return result.digest()


def directory_entries(resources, root):
    pending=[root]
    while pending:
        folder=pending.pop()
        for path in folder.iterdir():
            resources.validate(path)
            mode=path.lstat()
            if stat.S_ISLNK(mode.st_mode) or getattr(mode,'st_file_attributes',0)&0x400:
                raise ValueError('不支持目录链接')
            is_dir=stat.S_ISDIR(mode.st_mode)
            if not is_dir and not stat.S_ISREG(mode.st_mode):raise ValueError('不支持特殊文件')
            yield path.relative_to(root).as_posix(),is_dir,mode.st_size if not is_dir else 0
            if is_dir:pending.append(path)


@contextmanager
def prepare(resources, sources, *, platform, cancel_check=lambda:None):
    """Validate all inputs first, extract into owned cache, yield publish files."""
    if not sources or len(sources)>32:raise ValueError('请选择 1–32 个输入包')
    sources=[resources.validate(p) for p in sources]
    if len(set(sources))!=len(sources):raise ValueError('不能重复选择同一个压缩包')
    if any(not resources.input_allowed(p) for p in sources):raise PermissionError('输入未获选择器授权')
    directory = len(sources)==1 and sources[0].is_dir()
    if not directory and any(not p.is_file() for p in sources):raise ValueError('多个输入必须都是压缩包')
    paths={}; count=total=0; archive_names=set(); fingerprints={}; paths_sizes={}
    for source in sources:
        cancel_check()
        if source.is_file():
            if source.stat().st_size>MAX_BYTES:raise ValueError('归档体积超过限额')
            key=source.name.casefold()
            if key in archive_names:raise ValueError('来源包文件名重名：'+source.name)
            archive_names.add(key)
            fingerprints[source]=digest(source,cancel_check)
            entries=members(source)
        else:
            entries=directory_entries(resources,source)
        for name,is_dir,size in entries:
            cancel_check()
            relative=safe_member(name);key=relative.as_posix().casefold()
            if key in paths and not (is_dir and paths[key]):raise ValueError('多包路径重名，未覆盖：'+name)
            if any(paths.get(parent.as_posix().casefold()) is False for parent in relative.parents if parent.parts):raise ValueError('归档文件与目录冲突：'+name)
            if not is_dir and any(k.startswith(key+'/') for k in paths):raise ValueError('归档目录与文件冲突：'+name)
            paths[key]=is_dir
            paths_sizes[key]=size
            if not is_dir:
                count+=1;total+=size
                if count>MAX_FILES or total>MAX_BYTES or size>MAX_FILE_BYTES:raise ValueError('内容体积或文件数超过限额')
    if count==0:raise ValueError('没有可补齐的内容')
    workspace=resources.path('cache/content_completion')/uuid4().hex
    workspace.mkdir(parents=True)
    try:
        files=[];actual_total=actual_count=0
        if directory:
            for name,is_dir,_ in directory_entries(resources,sources[0]):
                if not is_dir:
                    item=sources[0]/name
                    if item.suffix.lower() in ('.7z','.rar'):raise ValueError('目录中的 7z/RAR 请通过压缩包选择器单独选择')
                    frozen=workspace/'directory'/name
                    frozen.parent.mkdir(parents=True,exist_ok=True)
                    copied=0
                    with item.open('rb') as src,frozen.open('xb') as dst:
                        while block:=src.read(1024*1024):
                            cancel_check();copied+=len(block)
                            if copied>MAX_FILE_BYTES:raise ValueError('输入文件超过限额')
                            dst.write(block)
                    if copied!=paths_sizes[name.casefold()]:raise ValueError('输入文件已变化')
                    files.append((frozen,Path(name)))
        else:
            from services.importers.archive import extract_archive
            for index,source in enumerate(sources):
                cancel_check()
                frozen=workspace/('input-'+str(index)+source.suffix.lower())
                with source.open('rb') as src,frozen.open('xb') as dst:
                    copied=0
                    while block:=src.read(1024*1024):
                        cancel_check();copied+=len(block)
                        if copied>MAX_BYTES:raise ValueError('输入包体积超过限额')
                        dst.write(block)
                if digest(frozen,cancel_check)!=fingerprints[source]:raise ValueError('输入包已变化，请重新选择')
                dest=workspace/str(index)
                extract_archive(frozen,dest_dir=dest,max_output_bytes=MAX_BYTES,cancel_check=cancel_check)
                actual=0
                for item in dest.rglob('*'):
                    resources.validate(item)
                    if item.is_file():
                        relative=safe_member(item.relative_to(dest).as_posix())
                        if paths.get(relative.as_posix().casefold()) is not False:raise ValueError('解包路径与目录不一致')
                        size=item.stat().st_size
                        actual+=size;actual_total+=size;actual_count+=1
                        if size>MAX_FILE_BYTES or actual_total>MAX_BYTES or actual_count>MAX_FILES:raise ValueError('实际内容超过限额')
                        if actual>MAX_BYTES:raise ValueError('实际解包内容超过限额')
                        if platform.lower()=='steam':files.append((item,Path(item.relative_to(dest))))
                if platform.lower()!='steam':files.append((frozen,Path(source.name)))
        cancel_check()
        yield files
    finally:
        resources.validate(workspace)
        shutil.rmtree(workspace)
