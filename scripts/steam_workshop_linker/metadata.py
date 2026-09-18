"""Read SMM ``.info/metadata.json`` without importing SMM runtime.

Production samples in this repo use:

* ``source_type`` (canonical) and ``platform`` — usually ``steam``
* ``workspace_id`` — Steam Workshop published_file_id
* ``source`` — often **absent**; the user-facing name is still accepted
* ``source_path`` — old Workshop filesystem path, not a platform token

Never treat a missing platform field as Steam.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Real schema aliases, duplicated here so this tool never imports core/.
STEAM_SOURCE_KEYS = ("source", "source_type", "platform")


def read_metadata(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def metadata_source_token(meta: dict) -> str:
    for key in STEAM_SOURCE_KEYS:
        token = str(meta.get(key) or "").strip()
        if token:
            return token
    return ""


def is_steam_source(meta: dict) -> bool:
    return metadata_source_token(meta).casefold() == "steam"


def normalize_workspace_id(value: object) -> str | None:
    """Return a decimal published_file_id, or None if unsafe/invalid."""
    text = str(value or "").strip()
    if not text or text in {".", ".."}:
        return None
    if "/" in text or "\\" in text or ":" in text:
        return None
    if any(ch.isspace() for ch in text):
        return None
    if not text.isdecimal():
        return None
    if Path(text).is_absolute() or Path(text).name != text:
        return None
    return text


@dataclass(frozen=True)
class ScannedMod:
    path: Path
    title: str
    source_token: str
    workspace_id: str | None
    steam: bool
    skip_reason: str = ""


def disk_internal_id(mod_dir: Path, meta: dict | None) -> tuple[str, bool]:
    """Return ``(internal_id, conflict)``.

    Reads ``metadata.json`` and optional ``.info/internal_id`` proof file.
    ``conflict`` is True when those two disagree. Empty is allowed.
    """
    values: list[str] = []
    if isinstance(meta, dict):
        raw = str(meta.get("internal_id") or "").strip()
        if raw:
            values.append(raw)
    proof = mod_dir / ".info" / "internal_id"
    if proof.is_file():
        try:
            disk = proof.read_text(encoding="utf-8-sig").strip()
        except OSError:
            disk = ""
        if disk:
            values.append(disk)
    unique = list(dict.fromkeys(values))
    if len(unique) > 1:
        return unique[0], True
    return (unique[0] if unique else ""), False


def scan_smm_workspace_index(smm_mod_root: Path) -> dict[str, list[Path]]:
    """Map ``metadata.workspace_id`` → candidate SMM folders (direct children only).

    Used only as a filesystem fallback after DB registration. Does **not**
    decide whether a Workshop Mod is registered.
    """
    index: dict[str, list[Path]] = {}
    try:
        children = sorted(smm_mod_root.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return index
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        meta = read_metadata(child / ".info" / "metadata.json")
        if meta is None:
            continue
        workspace_id = normalize_workspace_id(meta.get("workspace_id"))
        if workspace_id is None:
            continue
        index.setdefault(workspace_id, []).append(child)
    return index


def scan_smm_mods(smm_mod_root: Path) -> list[ScannedMod]:
    """Scan immediate children of an SMM game folder (metadata helper, not registration)."""
    found: list[ScannedMod] = []
    try:
        children = sorted(smm_mod_root.iterdir(), key=lambda p: p.name.casefold())
    except OSError as exc:
        raise OSError(f"cannot read smm_mod_root {smm_mod_root}: {exc}") from exc

    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        meta_path = child / ".info" / "metadata.json"
        if not meta_path.is_file():
            continue
        meta = read_metadata(meta_path)
        if meta is None:
            found.append(
                ScannedMod(
                    path=child,
                    title=child.name,
                    source_token="",
                    workspace_id=None,
                    steam=False,
                    skip_reason="invalid metadata.json",
                )
            )
            continue
        title = str(meta.get("display_name") or meta.get("title") or child.name).strip() or child.name
        token = metadata_source_token(meta)
        steam = is_steam_source(meta)
        workspace_id = normalize_workspace_id(meta.get("workspace_id"))
        if not steam:
            found.append(
                ScannedMod(
                    path=child,
                    title=title,
                    source_token=token or "missing",
                    workspace_id=workspace_id,
                    steam=False,
                    skip_reason=f"non-steam source={token or 'missing'}",
                )
            )
            continue
        if workspace_id is None:
            found.append(
                ScannedMod(
                    path=child,
                    title=title,
                    source_token=token,
                    workspace_id=None,
                    steam=True,
                    skip_reason="invalid or missing workspace_id",
                )
            )
            continue
        found.append(
            ScannedMod(
                path=child,
                title=title,
                source_token=token,
                workspace_id=workspace_id,
                steam=True,
            )
        )
    return found
