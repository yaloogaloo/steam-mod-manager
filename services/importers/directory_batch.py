"""Directory-level multi-Mod discovery and sidecar (cover / mhtml) extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from services.importers.image_picker import IMAGE_SUFFIXES
from services.importers.local_scanner import KNOWN_EXTENSIONS

_SKIP_DIRS = {".info", "info", ".git", "__pycache__", "node_modules", ".vs", "历史版本"}
# Structural folders inside a single Mod — not independent Mods.
_INNER_ONLY = frozenset(
    {
        "scripts",
        "logicmods",
        "paks",
        "content",
        "binaries",
        "mods",
        "ue4ss",
        "win64",
        "win32",
        "docs",
        "doc",
        "resources",
        "assets",
        "config",
        "configs",
        "data",
    }
)
_SIDECAR_SUFFIXES = IMAGE_SUFFIXES | {".mhtml", ".mht", ".html", ".htm", ".txt", ".md"}
_MHTML_SUFFIXES = {".mhtml", ".mht"}
_COVER_NAME_HINTS = ("cover", "preview", "poster", "thumb", "thumbnail", "header")


@dataclass(frozen=True)
class DirectorySidecars:
    """Cover / offline page candidates found under a Mod directory."""

    cover: Path | None = None
    offline_page: Path | None = None
    # Paths that must not be copied into the managed Mod library.
    ignore_paths: tuple[Path, ...] = field(default_factory=tuple)


def _is_skipped_dir(name: str) -> bool:
    text = str(name or "").strip()
    if text == "历史版本":
        return True
    return text.startswith(".") or text.lower() in {d.lower() for d in _SKIP_DIRS}


def _child_dirs(folder: Path) -> list[Path]:
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    return sorted(
        (p for p in entries if p.is_dir() and not _is_skipped_dir(p.name)),
        key=lambda p: p.name.lower(),
    )


def _top_level_package_files(folder: Path) -> bool:
    try:
        entries = list(folder.iterdir())
    except OSError:
        return False
    for path in entries:
        if path.is_file() and path.suffix.lower() in KNOWN_EXTENSIONS:
            return True
    return False


def _only_sidecar_top_files(folder: Path) -> bool:
    try:
        files = [p for p in folder.iterdir() if p.is_file()]
    except OSError:
        return True
    if not files:
        return True
    for path in files:
        suffix = path.suffix.lower()
        if suffix and suffix not in _SIDECAR_SUFFIXES:
            return False
    return True


def discover_mod_directories(selected: str | Path) -> list[Path]:
    """
    Resolve one or more Mod roots under a user-selected folder.

    - No child dirs → ``[selected]`` (single Mod).
    - Parent looks like a multi-Mod container → each immediate child dir is a Mod.
    - Otherwise → ``[selected]`` (single Mod with internal structure).
    """
    root = Path(selected).expanduser()
    if not root.is_dir():
        return []

    children = _child_dirs(root)
    if not children:
        return [root]

    if _top_level_package_files(root):
        return [root]

    if all(c.name.lower() in _INNER_ONLY for c in children):
        return [root]

    if len(children) == 1 and children[0].name.lower() in _INNER_ONLY:
        return [root]

    # Multiple sibling folders, or a container with only sidecar files at top.
    if len(children) >= 2 or _only_sidecar_top_files(root):
        return children

    return [root]


def _iter_files_shallow(folder: Path, *, max_depth: int = 2) -> list[Path]:
    root = folder.resolve()
    found: list[Path] = []
    try:
        from services.importers.local_scanner import is_history_version_path

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if is_history_version_path(path):
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if len(rel.parts) > max_depth:
                continue
            if any(_is_skipped_dir(part) for part in rel.parts[:-1]):
                continue
            found.append(path)
    except OSError:
        return []
    return found


def _pick_cover(images: list[Path]) -> Path | None:
    if not images:
        return None

    def rank(path: Path) -> tuple[int, int, str]:
        stem = path.stem.lower()
        hint = 0 if any(h in stem for h in _COVER_NAME_HINTS) else 1
        # Prefer shallower paths.
        depth = len(path.parts)
        return (hint, depth, path.name.lower())

    return sorted(images, key=rank)[0]


def extract_directory_sidecars(folder: str | Path) -> DirectorySidecars:
    """
    Scan *folder* for a cover image and an offline ``.mhtml`` page.

    Missing assets are ignored silently — never raises for absent files.
    """
    root = Path(folder).expanduser()
    if not root.is_dir():
        return DirectorySidecars()

    images: list[Path] = []
    mhtmls: list[Path] = []
    for path in _iter_files_shallow(root):
        suffix = path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            images.append(path)
        elif suffix in _MHTML_SUFFIXES:
            mhtmls.append(path)

    cover = _pick_cover(images)
    offline = None
    if mhtmls:
        offline = sorted(mhtmls, key=lambda p: (len(p.parts), p.name.lower()))[0]

    ignore: list[Path] = []
    # All identified display assets stay out of the managed Mod copy.
    ignore.extend(images)
    ignore.extend(mhtmls)
    # Deduplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in ignore:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)

    return DirectorySidecars(
        cover=cover,
        offline_page=offline,
        ignore_paths=tuple(unique),
    )
