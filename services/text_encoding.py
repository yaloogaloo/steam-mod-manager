"""Best-effort repair of Windows mojibake in display strings."""

from __future__ import annotations


def looks_like_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def repair_mojibake(text: str) -> str:
    """Return *text* unchanged when it already contains CJK; else try UTF-8 recovery."""
    raw = str(text or "")
    if not raw or looks_like_cjk(raw):
        return raw
    for encode, decode in (
        ("latin-1", "utf-8"),
        ("cp1252", "utf-8"),
        ("gbk", "utf-8"),
        ("utf-8", "gbk"),
    ):
        try:
            recovered = raw.encode(encode).decode(decode)
        except (UnicodeDecodeError, UnicodeEncodeError, LookupError):
            continue
        if looks_like_cjk(recovered) and recovered != raw:
            return recovered
    return raw
