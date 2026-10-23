"""Bounded multi-package admission around the existing archive extractor.

Steam publishes extracted paths from each archive into one directory tree, so
those paths stay unique. Other sources keep one original archive per source
unit; member names may repeat across archives and are not merged. Within one
archive, admission still refuses traversal, illegal paths, links, and duplicate
members. Publish targets are never overwritten. Nothing is published here; the
caller commits once against the original entity.
"""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import shutil
import stat
from uuid import uuid4

from services.archive_admission import MAX_FILES, MAX_BYTES, MAX_FILE_BYTES, safe_member


def members(source):
    from services.archive_admission import inspect_archive
    _, entries = inspect_archive(source)
    for item in entries:
        yield item.name, item.directory, item.size or 0


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
    if not directory and len({p.name.casefold() for p in sources})!=len(sources):raise ValueError('来源包文件名重名，未覆盖')
    if sum(p.stat().st_size for p in sources if p.is_file())>MAX_BYTES:raise ValueError('输入包总体积超过限额')
    workspace=resources.path('cache/content_completion')/uuid4().hex
    workspace.mkdir(parents=True)
    try:
        files=[];actual_total=actual_count=0;paths=set();directories=set()
        merge_members=platform.lower()=='steam'
        if directory:
            for name,is_dir,_ in directory_entries(resources,sources[0]):
                safe_member(name)
                if not is_dir:
                    item=sources[0]/name
                    from services.importers.archive import ARCHIVE_SUFFIXES
                    if item.suffix.lower() in ARCHIVE_SUFFIXES:raise ValueError('目录中的 ZIP / 7z/RAR 等压缩包请通过压缩包选择器单独选择')
                    frozen=workspace/'directory'/name
                    frozen.parent.mkdir(parents=True,exist_ok=True)
                    copied=0
                    with item.open('rb') as src,frozen.open('xb') as dst:
                        while block:=src.read(1024*1024):
                            cancel_check();copied+=len(block)
                            if copied>MAX_FILE_BYTES or actual_total+copied>MAX_BYTES:raise ValueError('输入文件超过限额')
                            dst.write(block)
                    actual_total+=copied;actual_count+=1
                    if actual_total>MAX_BYTES or actual_count>MAX_FILES:raise ValueError('内容体积或文件数超过限额')
                    files.append((frozen,Path(name)))
        else:
            from services.archive_admission import extract_archive,storage_name
            for index,source in enumerate(sources):
                cancel_check()
                frozen=workspace/'inputs'/str(index)/source.name
                frozen.parent.mkdir(parents=True)
                fingerprint=digest(source,cancel_check)
                with source.open('rb') as src,frozen.open('xb') as dst:
                    copied=0
                    while block:=src.read(1024*1024):
                        cancel_check();copied+=len(block)
                        if copied>MAX_BYTES:raise ValueError('输入包体积超过限额')
                        dst.write(block)
                if digest(frozen,cancel_check)!=fingerprint:raise ValueError('输入包已变化，请重新选择')
                dest=workspace/str(index)
                extract_archive(frozen,dest,max_bytes=MAX_BYTES-actual_total,max_file_bytes=MAX_FILE_BYTES,max_files=MAX_FILES-actual_count,cancel_check=cancel_check)
                actual=0
                for item in dest.rglob('*'):
                    resources.validate(item)
                    if item.is_file():
                        relative=safe_member(item.relative_to(dest).as_posix())
                        key=relative.as_posix().casefold()
                        if merge_members:
                            if key in paths:raise ValueError('多包路径重名，未覆盖：'+str(relative))
                            parents={p.as_posix().casefold() for p in relative.parents if p.parts}
                            if key in directories or parents & paths:raise ValueError('多包文件与目录冲突：'+str(relative))
                            paths.add(key);directories.update(parents)
                        size=item.stat().st_size
                        actual+=size;actual_total+=size;actual_count+=1
                        if size>MAX_FILE_BYTES or actual_total>MAX_BYTES or actual_count>MAX_FILES:raise ValueError('实际内容超过限额')
                        if actual>MAX_BYTES:raise ValueError('实际解包内容超过限额')
                        if platform.lower()=='steam':files.append((item,Path(item.relative_to(dest))))
                if platform.lower()!='steam':files.append((frozen,Path(storage_name(frozen,cancel_check))))
        if len({relative.as_posix().casefold() for item,relative in files})!=len(files):raise ValueError('来源包归档路径重名，未覆盖')
        cancel_check()
        yield files
    finally:
        resources.validate(workspace)
        shutil.rmtree(workspace)
