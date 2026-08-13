"""Platform-agnostic local Mod folder scanner → ``ModFileEntry`` list."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from core.mod_platform import (
    FILE_TYPE_MAIN,
    FILE_TYPE_OPTIONAL,
    ModFileEntry,
    ModFilesBundle,
    new_file_id,
    normalize_file_type,
)
from services.importers.image_scanner import IMAGE_EXTENSIONS, is_image_path

# Multi-file Files list: archives only (absolute rule).
ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar"}

# Recognized package / config extensions (not Steam/Nexus/GitHub specific).
# Kept for classify_file_kind / directory_batch hints — NOT for Files list scan.
KNOWN_EXTENSIONS = {
    ".pak": "pak",
    ".dll": "dll",
    ".so": "dll",
    ".dylib": "dll",
    ".json": "json",
    ".ini": "ini",
    ".cfg": "cfg",
    ".txt": "config",
    ".xml": "config",
    ".yaml": "config",
    ".yml": "config",
    ".zip": "archive",
    ".7z": "archive",
    ".rar": "archive",
}

_SKIP_DIRS = {
    ".info",
    "info",
    ".git",
    "__pycache__",
    "node_modules",
    ".vs",
    "历史版本",  # never enroll archives under history folders
}
# Preview / cover images are UI-only — never enter mod_files / deploy.
_SKIP_SUFFIXES = set(IMAGE_EXTENSIONS)

HISTORY_VERSION_DIR = "历史版本"


def is_history_version_path(path: Path | str) -> bool:
    """
    Bulletproof block: any path segment **or** substring ``历史版本`` is skipped.

    Must run at the front of every Mod-file walk / scan.
    """
    text = str(path or "")
    if HISTORY_VERSION_DIR in text:
        return True
    try:
        return HISTORY_VERSION_DIR in Path(path).parts
    except (TypeError, ValueError, OSError):
        return False


def is_history_version_entry(entry: Any) -> bool:
    """True when a ModFileEntry (or mapping) touches ``历史版本`` in any identity field."""
    if entry is None:
        return False
    if isinstance(entry, Mapping):
        values = [
            entry.get("path"),
            entry.get("filename"),
            entry.get("name"),
            entry.get("display_name"),
        ]
    else:
        values = [
            getattr(entry, "path", None),
            getattr(entry, "filename", None),
            getattr(entry, "name", None),
            getattr(entry, "display_name", None),
        ]
    return any(is_history_version_path(v) for v in values if v is not None)


def filter_out_history_version_entries(files: Sequence[Any] | None) -> list[Any]:
    """Drop any file entries that reference ``历史版本`` (cache / UI / deploy)."""
    return [f for f in (files or []) if not is_history_version_entry(f)]


def is_skipped_mod_path_part(name: str) -> bool:
    """True when a path segment must be ignored by Mod file scans / deploy walks."""
    text = str(name or "").strip()
    return text in _SKIP_DIRS or text == HISTORY_VERSION_DIR or HISTORY_VERSION_DIR in text


def is_under_skipped_mod_dir(rel_parts: tuple[str, ...] | list[str]) -> bool:
    """True when any path component is a skipped Mod directory (e.g. ``历史版本``)."""
    if any(is_skipped_mod_path_part(part) for part in rel_parts):
        return True
    # Also catch joined relative paths that embed the history folder name.
    return is_history_version_path("/".join(str(p) for p in rel_parts))


def classify_file_kind(path: Path | str) -> str:
    """Return a coarse kind label for *path* (pak/dll/json/ini/cfg/other)."""
    ext = Path(path).suffix.lower()
    return KNOWN_EXTENSIONS.get(ext, "other")


def is_archive_mod_file(path: Path | str) -> bool:
    """True when *path* is a Files-list archive (``.zip`` / ``.7z`` / ``.rar``)."""
    return Path(path).suffix.lower() in ARCHIVE_SUFFIXES


def scan_mod_directory(
    folder: str | Path,
    *,
    relative_to: str | Path | None = None,
) -> ModFilesBundle:
    """
    Scan a Mod directory into a ``ModFilesBundle``.

    Absolute rule for the Detail Files list:
    - **Only** ``.zip`` / ``.7z`` / ``.rar`` archives are enrolled
    - Pure directory Mods (loose ``.pak`` / ``.json`` / … only) → empty bundle
    - Skip ``.info`` / ``info`` / VCS / hidden files / images

    First archive → ``type=main``, ``enabled=True``; remaining → optional.
    """
    root = Path(folder).expanduser().resolve()
    base = Path(relative_to).expanduser().resolve() if relative_to else root
    if not root.is_dir():
        return ModFilesBundle()

    candidates: list[Path] = []
    for path in sorted(root.rglob("*")):
        # 最前端绝对过滤：任意层级含「历史版本」一律跳过
        if is_history_version_path(path) or (
            "历史版本" in Path(path).parts or "历史版本" in str(path)
        ):
            continue
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if not is_archive_mod_file(path):
            continue
        if is_image_path(path) or path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if is_under_skipped_mod_dir(rel_parts):
            continue
        candidates.append(path)

    def _rank(p: Path) -> tuple[int, int, str]:
        stem = p.stem.lower()
        main_boost = 0 if ("main" in stem or stem in {"main", "primary"}) else 1
        prefer = {".zip": 0, ".7z": 1, ".rar": 2}
        return (main_boost, prefer.get(p.suffix.lower(), 50), p.name.lower())

    candidates.sort(key=_rank)
    files: list[ModFileEntry] = []
    for index, path in enumerate(candidates):
        try:
            rel = path.relative_to(base).as_posix()
        except ValueError:
            rel = path.name
        is_main = index == 0
        name_l = path.stem.lower()
        if index > 0 and ("main" in name_l or name_l.startswith("bp_")):
            ftype = FILE_TYPE_MAIN
            enabled = True
        elif is_main:
            ftype = FILE_TYPE_MAIN
            enabled = True
        else:
            ftype = FILE_TYPE_OPTIONAL
            enabled = False
        if ftype == FILE_TYPE_MAIN and any(f.type == FILE_TYPE_MAIN for f in files):
            ftype = FILE_TYPE_OPTIONAL
            enabled = False
        files.append(
            ModFileEntry(
                id=new_file_id(),
                name=path.stem if is_main else path.name,
                filename=path.name,
                path=rel,
                type=normalize_file_type(ftype),
                enabled=enabled,
            )
        )
    if files and files[0].type == FILE_TYPE_MAIN:
        files[0].name = "Main File"
    return ModFilesBundle(files=files)


# Back-compat alias used by older call sites / docs.
scan_folder_to_mod_files = scan_mod_directory
