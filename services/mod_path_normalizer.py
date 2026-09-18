"""Mod directory name → deployable directory name.

Pure naming helpers. This module must not copy, move, deploy, back up,
touch the database, or encode game-specific rules.

Callers that need an ASCII / engine-safe deploy folder:

    folder_name = normalize_mod_folder_name(mod_folder.name)
    folder_name = ensure_unique_mod_folder_name(folder_name, target_directory)
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)
_PYPINYIN_MISSING_LOGGED = False

# CJK Unified Ideographs (汉字). Shared detection for deploy-folder policies.
_CJK_HAN_START = "\u4e00"
_CJK_HAN_END = "\u9fff"

_SAFE_ASCII_KEEP = frozenset("._-")
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_UNDERSCORE = re.compile(r"_+")
_MAX_UNIQUE_SUFFIX = 10_000
_FALLBACK_NAME = "unnamed_mod"
_MAX_NAME_LENGTH = 120
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def contains_chinese(text: str) -> bool:
    """True when *text* contains at least one CJK Unified Ideograph (汉字)."""
    return any(_CJK_HAN_START <= ch <= _CJK_HAN_END for ch in str(text or ""))


def _is_han(ch: str) -> bool:
    return _CJK_HAN_START <= ch <= _CJK_HAN_END


def _unknown_han_token(chars: str) -> list[str]:
    """Stable ASCII stand-in for CJK that pypinyin cannot convert."""
    return [f"u{ord(ch):04x}" for ch in chars]


def _han_to_pinyin(text: str) -> str:
    """Tone-less pinyin for a Han run. Same input always yields the same output.

    ``pypinyin`` is optional at runtime. When it is missing, map each Han
    character to a stable ``uXXXX`` token so the deploy folder name stays
    ASCII. Never keep raw Han in the output — that would leave a Chinese
    directory on disk.
    """
    global _PYPINYIN_MISSING_LOGGED
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        if not _PYPINYIN_MISSING_LOGGED:
            logger.warning(
                "pypinyin is not installed; using ASCII codepoint tokens "
                "for Han characters in deploy folder names"
            )
            _PYPINYIN_MISSING_LOGGED = True
        return "".join(_unknown_han_token(text))

    syllables = lazy_pinyin(
        text,
        style=Style.NORMAL,
        errors=_unknown_han_token,
    )
    return "".join(str(part) for part in syllables if part)


def _ascii_is_deploy_safe(name: str) -> bool:
    """True when *name* is already ASCII and needs no Han conversion."""
    return bool(name) and name.isascii() and not contains_chinese(name)


def _windows_safe_deploy_name(name: str) -> str:
    """Windows-safe folder name that keeps ``_`` and ``-`` (unlike library sanitize)."""
    cleaned = _ILLEGAL_CHARS.sub("_", (name or "").strip())
    cleaned = _MULTI_UNDERSCORE.sub("_", cleaned).strip(" ._")
    if not cleaned:
        cleaned = _FALLBACK_NAME
    stem = cleaned.split(".")[0].upper()
    if stem in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned) > _MAX_NAME_LENGTH:
        cleaned = cleaned[:_MAX_NAME_LENGTH].rstrip(" ._")
    return cleaned or _FALLBACK_NAME


def normalize_mod_folder_name(name: str) -> str:
    """
    Convert a Mod directory name into a stable deployable folder name.

    - ASCII letters, digits, ``_``, ``-``, and ``.`` are kept
    - Han characters become tone-less pinyin (no random hash)
    - The result never contains CJK Unified Ideographs
    - The same input always produces the same output
    """
    raw = unicodedata.normalize("NFKC", str(name or "")).strip()
    if not raw:
        return _FALLBACK_NAME

    if _ascii_is_deploy_safe(raw):
        return _windows_safe_deploy_name(raw)

    pieces: list[str] = []
    ascii_buf: list[str] = []
    han_buf: list[str] = []

    def flush_ascii() -> None:
        if ascii_buf:
            pieces.append("".join(ascii_buf))
            ascii_buf.clear()

    def flush_han() -> None:
        if not han_buf:
            return
        token = _han_to_pinyin("".join(han_buf))
        if token:
            pieces.append(token)
        han_buf.clear()

    for ch in raw:
        if _is_han(ch):
            flush_ascii()
            han_buf.append(ch)
        elif ch.isascii() and (ch.isalnum() or ch in _SAFE_ASCII_KEEP):
            flush_han()
            ascii_buf.append(ch)
        else:
            flush_ascii()
            flush_han()

    flush_ascii()
    flush_han()

    cleaned = "_".join(part for part in pieces if part)
    cleaned = _MULTI_UNDERSCORE.sub("_", cleaned).strip("._- ")
    if contains_chinese(cleaned):
        ascii_chars: list[str] = []
        for ch in cleaned:
            if _is_han(ch):
                ascii_chars.extend(_unknown_han_token(ch))
            else:
                ascii_chars.append(ch)
        cleaned = "".join(ascii_chars)
        cleaned = _MULTI_UNDERSCORE.sub("_", cleaned).strip("._- ")
    return _windows_safe_deploy_name(cleaned)


def _existing_name_keys(existing_paths: Iterable[str | Path] | str | Path | None) -> set[str]:
    if existing_paths is None:
        return set()

    if isinstance(existing_paths, (str, Path)):
        path = Path(existing_paths)
        if path.is_dir():
            try:
                return {
                    child.name.casefold()
                    for child in path.iterdir()
                    if child.name not in {".", ".."}
                }
            except OSError:
                return set()
        name = path.name or str(existing_paths).strip()
        return {name.casefold()} if name else set()

    keys: set[str] = set()
    for item in existing_paths:
        if isinstance(item, Path):
            label = item.name
        else:
            text = str(item or "").strip()
            label = Path(text).name if text else ""
        if label:
            keys.add(label.casefold())
    return keys


def ensure_unique_mod_folder_name(
    base_name: str,
    existing_paths: Iterable[str | Path] | str | Path | None = None,
) -> str:
    """
    Return *base_name* or ``base_name_1`` / ``base_name_2`` / … so it does not
    collide with *existing_paths*.

    *existing_paths* may be:

    - a target directory (its child names are occupied)
    - an iterable of names or paths
    """
    cleaned = str(base_name or "").strip() or _FALLBACK_NAME
    occupied = _existing_name_keys(existing_paths)
    if cleaned.casefold() not in occupied:
        return cleaned

    for index in range(1, _MAX_UNIQUE_SUFFIX + 1):
        candidate = f"{cleaned}_{index}"
        if candidate.casefold() not in occupied:
            return candidate
    raise ValueError(f"unable to allocate a unique folder name for {cleaned!r}")


__all__ = [
    "contains_chinese",
    "ensure_unique_mod_folder_name",
    "normalize_mod_folder_name",
]
