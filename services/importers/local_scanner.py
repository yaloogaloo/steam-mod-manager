"""Platform-agnostic local Mod folder scanner → ``ModFileEntry`` list."""

from __future__ import annotations

from pathlib import Path

from core.mod_platform import (
    FILE_TYPE_MAIN,
    FILE_TYPE_OPTIONAL,
    ModFileEntry,
    ModFilesBundle,
    new_file_id,
    normalize_file_type,
)
from services.importers.image_scanner import IMAGE_SUFFIXES, is_image_path

# Recognized package / config extensions (not Steam/Nexus/GitHub specific).
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

_SKIP_DIRS = {".info", "info", ".git", "__pycache__", "node_modules", ".vs"}
# Preview / cover images are UI-only — never enter mod_files / deploy.
_SKIP_SUFFIXES = set(IMAGE_SUFFIXES)


def classify_file_kind(path: Path | str) -> str:
    """Return a coarse kind label for *path* (pak/dll/json/ini/cfg/other)."""
    ext = Path(path).suffix.lower()
    return KNOWN_EXTENSIONS.get(ext, "other")


def scan_mod_directory(
    folder: str | Path,
    *,
    relative_to: str | Path | None = None,
) -> ModFilesBundle:
    """
    Scan a Mod directory into a ``ModFilesBundle``.

    Rules (platform-neutral):
    - Skip ``.info`` / ``info`` / VCS / hidden files
    - Prefer files whose stem looks like ``main`` / common package extensions
    - First primary file → ``type=main``, ``enabled=True``
    - Remaining → ``type=optional``, ``enabled=False``
    """
    root = Path(folder).expanduser().resolve()
    base = Path(relative_to).expanduser().resolve() if relative_to else root
    if not root.is_dir():
        return ModFilesBundle()

    candidates: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if is_image_path(path) or path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        candidates.append(path)

    def _rank(p: Path) -> tuple[int, int, str]:
        stem = p.stem.lower()
        main_boost = 0 if ("main" in stem or stem in {"main", "primary"}) else 1
        ext = p.suffix.lower()
        prefer = {
            ".pak": 0,
            ".zip": 1,
            ".7z": 2,
            ".rar": 3,
            ".dll": 4,
            ".json": 5,
            ".ini": 6,
            ".cfg": 7,
        }
        return (main_boost, prefer.get(ext, 50), p.name.lower())

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
