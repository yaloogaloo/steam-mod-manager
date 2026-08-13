"""Metadata ownership — user overrides vs official provider fields (SQLite only)."""

from __future__ import annotations

import json
import logging
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
