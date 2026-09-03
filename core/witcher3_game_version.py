"""Witcher 3 ONLY — Mod game-compatibility version dimension.

This is NOT:
    - mod_version (author / installed Mod version string)
    - Mod.io ``version``
    - Steam Workshop revision
    - a Mod identity field (mod_id / external_id / workspace_id / source_url)

Legal stored values: original | next_gen | remake
Default for Witcher 3 Mods: next_gen
All other games: NULL / not applicable — never auto-assign next_gen.
"""

from __future__ import annotations

from typing import Any

# Steam App ID for The Witcher 3: Wild Hunt — the project's stable game identity.
# Do not identify Witcher 3 from folder names or Mod titles.
WITCHER3_APP_IDS = frozenset({292030})

# Exact aliases after name-key normalize (no fuzzy substring matching).
# Used only when app_id is missing; App ID always wins.
WITCHER3_NAME_ALIASES = frozenset(
    {
        "the witcher 3",
        "the witcher 3: wild hunt",
        "the witcher 3 wild hunt",
        "witcher 3",
        "witcher3",
        "巫师3",
        "巫师三",
        "巫师3狂猎",
        "巫师三狂猎",
    }
)

WITCHER3_VERSION_ORIGINAL = "original"
WITCHER3_VERSION_NEXT_GEN = "next_gen"
WITCHER3_VERSION_REMAKE = "remake"

WITCHER3_DEFAULT_VERSION = WITCHER3_VERSION_NEXT_GEN

WITCHER3_GAME_VERSIONS = frozenset(
    {
        WITCHER3_VERSION_ORIGINAL,
        WITCHER3_VERSION_NEXT_GEN,
        WITCHER3_VERSION_REMAKE,
    }
)

WITCHER3_GAME_VERSION_LABELS: dict[str, str] = {
    WITCHER3_VERSION_ORIGINAL: "原版",
    WITCHER3_VERSION_NEXT_GEN: "次世代版",
    WITCHER3_VERSION_REMAKE: "重制版",
}

# UI combo order.
WITCHER3_GAME_VERSION_CHOICES: tuple[tuple[str, str], ...] = (
    (WITCHER3_VERSION_ORIGINAL, WITCHER3_GAME_VERSION_LABELS[WITCHER3_VERSION_ORIGINAL]),
    (WITCHER3_VERSION_NEXT_GEN, WITCHER3_GAME_VERSION_LABELS[WITCHER3_VERSION_NEXT_GEN]),
    (WITCHER3_VERSION_REMAKE, WITCHER3_GAME_VERSION_LABELS[WITCHER3_VERSION_REMAKE]),
)


def _witcher3_name_key(game_name: str | None) -> str:
    """Exact-name key. Strips punctuation Steam store titles use; never substrings."""
    from core.mod_platform import _normalize_game_key

    key = _normalize_game_key(game_name)
    for ch in (":", ".", ",", ";", "!", "?"):
        key = key.replace(ch, "")
    return key


def is_witcher3_game(game_name: str = "", game_id: int | str = 0) -> bool:
    """True when the library game is The Witcher 3 (stable App ID first).

    Witcher 3 ONLY. Do not treat this as a generic “game has versions” helper.
    """
    from core.mod_platform import _coerce_game_id

    gid = _coerce_game_id(game_id)
    if gid in WITCHER3_APP_IDS:
        return True
    # A real non-Witcher App ID is never Witcher 3, even if the display name matches.
    if gid > 0:
        return False
    key = _witcher3_name_key(game_name)
    if not key:
        return False
    aliases = {_witcher3_name_key(a) for a in WITCHER3_NAME_ALIASES}
    return key in aliases


def is_valid_witcher3_game_version(value: str | None) -> bool:
    return str(value or "").strip() in WITCHER3_GAME_VERSIONS


def normalize_witcher3_game_version(value: str | None) -> str | None:
    """Return a legal stored token, or None when blank.

    Illegal strings (``foo``, Mod.io-style ``1.32`` / ``4.0``, Chinese labels)
    are not coerced. Call :func:`validate_witcher3_game_version` to reject them.
    """
    text = str(value or "").strip()
    if text in WITCHER3_GAME_VERSIONS:
        return text
    return None


def validate_witcher3_game_version(value: str | None) -> str:
    """Return a legal Witcher 3 game_version or raise ``ValueError``.

    Blank is invalid here — callers that want the product default should use
    :data:`WITCHER3_DEFAULT_VERSION` instead of a silent fallback.
    """
    text = str(value or "").strip()
    if text in WITCHER3_GAME_VERSIONS:
        return text
    raise ValueError(
        f"invalid Witcher 3 game_version {value!r}; "
        f"legal values: {sorted(WITCHER3_GAME_VERSIONS)}"
    )


def witcher3_game_version_label(value: str | None) -> str:
    key = str(value or "").strip()
    return WITCHER3_GAME_VERSION_LABELS.get(key, "")


def stored_game_version_for_game(
    *,
    game_name: str = "",
    game_id: int | str = 0,
    requested: str | None = None,
) -> str | None:
    """Value to persist: legal enum for Witcher 3, otherwise None (SQL NULL)."""
    if not is_witcher3_game(game_name, game_id):
        return None
    text = str(requested or "").strip()
    if not text:
        return WITCHER3_DEFAULT_VERSION
    return validate_witcher3_game_version(text)


def ensure_witcher3_game_version_default(
    db: Any,
    mod_id: str | int,
    *,
    app_id: int | str = 0,
    game_name: str = "",
) -> None:
    """Set next_gen on a Witcher 3 Mod when game_version is still empty.

    Never writes next_gen onto a non-Witcher-3 row. Does not change identity.
    Existing original / remake / next_gen values are left untouched.
    """
    mid = str(mod_id or "").strip()
    if not mid.isdigit():
        return
    info = db.get_mod_display_info(mid)
    row_app = int(app_id or 0)
    if info is not None and row_app <= 0:
        row_app = int(getattr(info, "app_id", 0) or 0)
    if not is_witcher3_game(game_name, row_app):
        return
    current = ""
    if info is not None:
        current = str(getattr(info, "game_version", "") or "").strip()
    if is_valid_witcher3_game_version(current):
        return
    db.set_mod_game_version(mid, WITCHER3_DEFAULT_VERSION)
