"""Metadata ownership — user overrides vs official provider fields (SQLite only).

Cover domains (one pointer, ``mods.cover_path`` — not a second identity):

- USER OWNED: ``user_override_fields.cover`` is set. Generic refresh, official
  sync, and sidecar rescan must not change the pointer or the live file.
- OFFICIAL / REFRESH OWNED: no user override and no live cover. A provider may
  fill ``.info/cover.*`` once.
- DERIVED / CACHE: decoded ``QImage`` in ``cover_cache``. Never a source of truth.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from core.models import is_unknown_mod_title

logger = logging.getLogger(__name__)

FIELD_DISPLAY_NAME = "display_name"
FIELD_DESCRIPTION = "description"
FIELD_COVER = "cover"

_SUPPORTED_OVERRIDE_FIELDS = frozenset(
    {FIELD_DISPLAY_NAME, FIELD_DESCRIPTION, FIELD_COVER}
)


def parse_user_override_fields(raw: str | None) -> dict[str, bool]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, bool] = {}
    for key, val in data.items():
        k = str(key or "").strip()
        if k in _SUPPORTED_OVERRIDE_FIELDS and bool(val):
            out[k] = True
    return out


def serialize_user_override_fields(fields: dict[str, bool]) -> str:
    clean = {
        k: True
        for k, v in (fields or {}).items()
        if k in _SUPPORTED_OVERRIDE_FIELDS and bool(v)
    }
    return json.dumps(clean, ensure_ascii=False, separators=(",", ":"))


def user_has_override(overrides: dict[str, bool], field: str) -> bool:
    return bool(overrides.get(str(field or "").strip()))


def cover_reference_is_foreign(managed_path: str | Path | None, cover_ref: str | None) -> bool:
    """True when *cover_ref* is an absolute path outside the managed Mod folder.

    Relative references (``.info/cover.png``) belong to the Mod. An absolute
    path inside that folder is the same file. An absolute path in another
    tree is a stale/foreign pointer and must not be written to ``mods.cover_path``.
    """
    ref = str(cover_ref or "").strip()
    if not ref or managed_path is None:
        return False
    path = Path(ref)
    if not path.is_absolute():
        return False
    try:
        root = Path(managed_path).expanduser().resolve()
        path.resolve().relative_to(root)
    except (OSError, ValueError):
        return True
    return False


def is_placeholder_display_name(value: str | None, *, mod_id: str = "") -> bool:
    return is_unknown_mod_title(value, published_file_id=mod_id)


def is_placeholder_description(value: str | None) -> bool:
    return not str(value or "").strip()


def should_apply_official_field(
    field: str,
    *,
    overrides: dict[str, bool],
    local_value: str = "",
    mod_id: str = "",
) -> bool:
    """True when official value may replace the local/user-facing field."""
    if user_has_override(overrides, field):
        return False
    if field == FIELD_DISPLAY_NAME:
        return is_placeholder_display_name(local_value, mod_id=mod_id)
    if field == FIELD_DESCRIPTION:
        return is_placeholder_description(local_value)
    if field == FIELD_COVER:
        return not str(local_value or "").strip()
    return False


def merge_official_sidecar_fields(
    data: dict[str, Any],
    *,
    mod_id: str,
    overrides: dict[str, bool],
    official_title: str = "",
    official_description: str = "",
    official_preview_url: str = "",
    cover_rel: str = "",
) -> dict[str, Any]:
    """
    Merge official fields into a metadata.json dict without clobbering user edits.

    Always updates ``title`` (official source). User-facing fields obey overrides.
    """
    out = dict(data or {})
    mid = str(mod_id or "").strip()
    if official_title.strip():
        out["title"] = official_title.strip()
    local_display = str(out.get("display_name") or "").strip()
    if should_apply_official_field(
        FIELD_DISPLAY_NAME,
        overrides=overrides,
        local_value=local_display,
        mod_id=mid,
    ):
        out["display_name"] = official_title.strip()
    local_desc = str(out.get("description") or "").strip()
    if official_description.strip() and should_apply_official_field(
        FIELD_DESCRIPTION,
        overrides=overrides,
        local_value=local_desc,
        mod_id=mid,
    ):
        out["description"] = official_description.strip()
    if official_preview_url.strip():
        out["preview_url"] = official_preview_url.strip()
    if cover_rel.strip() and should_apply_official_field(
        FIELD_COVER,
        overrides=overrides,
        local_value=str(out.get("cover_path") or ""),
        mod_id=mid,
    ):
        out["cover_path"] = cover_rel.strip()
    return out
