"""List archive members for Deploy FilePlan building (no extraction).

Extraction remains exclusively in ``services/deploy_apply.py`` via
``ArchiveExtractor``. Strategies may call this module only to enumerate
members for path mapping.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


def iter_archive_members(archive: Path | str) -> list[str]:
    """
    Return posix file member paths inside *archive* (directories omitted).

    Supports ``.zip`` via stdlib. ``.7z`` / ``.rar`` use lightweight listing
    helpers when available; otherwise raises ``RuntimeError`` so callers can
    fail the plan stage with a clear error (never silent empty plans).
    """
    path = Path(archive).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"压缩包不存在：{path}")
    suffix = path.suffix.lower()
    if suffix == ".zip":
        return _list_zip_members(path)
    if suffix == ".7z":
        return _list_7z_members(path)
    if suffix == ".rar":
        return _list_rar_members(path)
    raise RuntimeError(f"不支持的压缩格式：{suffix}")


def archive_contains_prefix(archive: Path | str, prefix: str) -> bool:
    """True when any file member is under *prefix* (posix, no leading slash)."""
    needle = str(prefix or "").replace("\\", "/").strip("/")
    if not needle:
        return False
    for member in iter_archive_members(archive):
        if member == needle or member.startswith(needle + "/"):
            return True
    return False


def _list_zip_members(path: Path) -> list[str]:
    from services.importers.archive import is_history_version_path

    out: list[str] = []
    with zipfile.ZipFile(path, "r") as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/").lstrip("/")
            if not name or name.endswith("/") or info.is_dir():
                continue
            if is_history_version_path(name):
                continue
            out.append(name)
    return out


def _list_7z_members(path: Path) -> list[str]:
    from services.importers.archive import is_history_version_path

    try:
        import py7zr
    except ImportError as exc:
        raise RuntimeError("缺少 py7zr，无法枚举 7z 成员") from exc
    out: list[str] = []
    with py7zr.SevenZipFile(path, mode="r") as archive:
        for name in archive.getnames():
            member = str(name or "").replace("\\", "/").lstrip("/")
            if not member or member.endswith("/"):
                continue
            if is_history_version_path(member):
                continue
            out.append(member)
    return out


def _list_rar_members(path: Path) -> list[str]:
    from services.importers.archive import is_history_version_path

    try:
        import rarfile
    except ImportError as exc:
        raise RuntimeError("缺少 rarfile，无法枚举 RAR 成员") from exc
    out: list[str] = []
    with rarfile.RarFile(path) as rf:
        for info in rf.infolist():
            name = str(getattr(info, "filename", "") or "").replace("\\", "/")
            name = name.lstrip("/")
            if not name or name.endswith("/") or getattr(info, "isdir", lambda: False)():
                continue
            if is_history_version_path(name):
                continue
            out.append(name)
    return out
