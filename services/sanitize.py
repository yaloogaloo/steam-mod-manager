"""Windows-safe folder/file name sanitization."""

from __future__ import annotations

import re
from pathlib import Path

# Characters illegal in Windows file/folder names
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Collapse runs of whitespace / underscores produced by sanitization
_MULTI_SPACE = re.compile(r"[\s_]+")

_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Leave room for uniqueness suffix like "_(1234567890)"
DEFAULT_MAX_NAME_LENGTH = 120


def sanitize_folder_name(
    name: str,
    *,
    fallback: str = "unnamed_mod",
    max_length: int = DEFAULT_MAX_NAME_LENGTH,
) -> str:
    """
    Convert an arbitrary Mod title into a Windows-safe folder name.

    - Strips illegal characters ``<>:\"/\\|?*`` and control chars
    - Trims trailing dots/spaces (invalid on Windows)
    - Avoids reserved device names (CON, PRN, …)
    - Truncates to *max_length*
    """
    cleaned = _ILLEGAL_CHARS.sub(" ", (name or "").strip())
    cleaned = _MULTI_SPACE.sub(" ", cleaned).strip(" .")

    if not cleaned:
        cleaned = fallback

    # Reserved names are case-insensitive and may appear with an extension-like suffix
    stem = cleaned.split(".")[0].upper()
    if stem in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")

    return cleaned or fallback


def unique_destination(
    parent: Path,
    folder_name: str,
    *,
    published_file_id: str | None = None,
) -> Path:
    """
    Return a non-existing path under *parent*.

    Strategy:
    1. ``parent / folder_name``
    2. ``parent / f"{folder_name}_{id}"`` when *published_file_id* is given
    3. ``parent / f"{folder_name} ({n})"`` thereafter
    """
    parent = Path(parent)
    candidate = parent / folder_name
    if not candidate.exists():
        return candidate

    if published_file_id:
        with_id = parent / f"{folder_name}_{published_file_id}"
        if not with_id.exists():
            return with_id

    index = 2
    while True:
        alt = parent / f"{folder_name} ({index})"
        if not alt.exists():
            return alt
        index += 1
