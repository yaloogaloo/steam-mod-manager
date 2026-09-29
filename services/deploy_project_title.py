"""Darkest Dungeon post-apply ``project.xml`` <Title> sync.

Runs after FilePlan Apply and before Validate / Hash / Manifest.
Touches only ``target_root/project.xml`` (or the FilePlan target path).
Never walks the Mod tree. Never rewrites source or ``.info``.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.deploy_rules.game_capabilities import (
    CAPABILITY_SYNC_PROJECT_XML_TITLE,
    supports_game_capability,
)
from services.info_sidecar import load_info_sidecar

logger = logging.getLogger(__name__)

_UTF8_BOM = b"\xef\xbb\xbf"
_TITLE_RE = re.compile(r"(<Title\s*>)(.*?)(</Title\s*>)", re.IGNORECASE | re.DOTALL)
_SKIP_NOT_FOUND = "project.xml not found"
_SKIP_NO_TITLE = "<Title> not found"
_SKIP_NO_DISPLAY = "display_name missing"
_SKIP_CAPABILITY = "capability disabled"


@dataclass(frozen=True)
class ProjectTitleSyncResult:
    status: str
    reason: str = ""
    path: str = ""
    changed: bool = False


def _xml_escape_text(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def replace_project_title_text(xml_text: str, display_name: str) -> str | None:
    """Return XML with only the first ``<Title>`` text replaced, or None to skip."""
    match = _TITLE_RE.search(xml_text)
    if match is None:
        return None
    inner = _xml_escape_text(display_name)
    if match.group(2) == inner:
        return xml_text
    return xml_text[: match.start()] + match.group(1) + inner + match.group(3) + xml_text[
        match.end() :
    ]


def _sidecar_display_name(managed_path: str | Path) -> str:
    sidecar = load_info_sidecar(managed_path)
    if sidecar is None:
        return ""
    return str(sidecar.display_name or "").strip()


def _is_project_xml_name(name: str) -> bool:
    return Path(str(name or "").replace("\\", "/")).name.lower() == "project.xml"


def resolve_target_project_xml(file_plan: Any) -> tuple[Path | None, str]:
    """Single known ``project.xml`` target. Never walks the tree.

    Prefers a FilePlan entry whose target relative path is ``project.xml``.
    Falls back to ``target_root / "project.xml"``.
    """
    root_hit: tuple[Path, str] | None = None
    named_hit: tuple[Path, str] | None = None
    for entry in list(getattr(file_plan, "files", None) or []):
        rel = (
            str(getattr(entry, "target_relative", "") or "")
            .replace("\\", "/")
            .strip()
            .lstrip("/")
        )
        abs_raw = str(getattr(entry, "target_absolute", "") or "").strip()
        name = Path(rel).name if rel else Path(abs_raw).name
        if name.lower() != "project.xml":
            continue
        if not abs_raw:
            continue
        item = (Path(abs_raw), str(getattr(entry, "source", "") or ""))
        if rel.lower() == "project.xml":
            root_hit = item
            break
        if named_hit is None:
            named_hit = item
    hit = root_hit or named_hit
    if hit is not None:
        return hit
    root = str(getattr(file_plan, "target_root", "") or "").strip()
    if root:
        return Path(root) / "project.xml", ""
    return None, ""


def _write_title(path: Path, display_name: str) -> str:
    """Rewrite only ``<Title>`` text. Returns skip/update reason token."""
    raw = path.read_bytes()
    has_bom = raw.startswith(_UTF8_BOM)
    body = raw[len(_UTF8_BOM) :] if has_bom else raw
    text = body.decode("utf-8")
    updated = replace_project_title_text(text, display_name)
    if updated is None:
        return _SKIP_NO_TITLE
    if updated == text:
        return "unchanged"
    out = updated.encode("utf-8")
    if has_bom:
        out = _UTF8_BOM + out
    path.write_bytes(out)
    return "updated"


def _note_target_hash(source: str, target: Path) -> None:
    if not source or not _is_project_xml_name(source):
        return
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    from services.deploy_apply import update_apply_source_hash

    update_apply_source_hash(source, digest)


def sync_deployed_project_title(
    *,
    app_id: int | str,
    file_plan: Any,
    managed_path: str | Path,
) -> ProjectTitleSyncResult:
    """Post-apply Title sync for games with ``sync_project_xml_title``."""
    if not supports_game_capability(app_id, CAPABILITY_SYNC_PROJECT_XML_TITLE):
        return ProjectTitleSyncResult(status="skipped", reason=_SKIP_CAPABILITY)

    target, source = resolve_target_project_xml(file_plan)
    if target is None:
        logger.info(
            "DarkestDungeon Project Title sync: skipped, %s", _SKIP_NOT_FOUND
        )
        return ProjectTitleSyncResult(status="skipped", reason=_SKIP_NOT_FOUND)

    try:
        exists = target.is_file()
    except OSError:
        exists = False
    if not exists:
        logger.info(
            "DarkestDungeon Project Title sync: skipped, %s", _SKIP_NOT_FOUND
        )
        return ProjectTitleSyncResult(
            status="skipped", reason=_SKIP_NOT_FOUND, path=str(target)
        )

    display_name = _sidecar_display_name(managed_path)
    if not display_name:
        logger.info(
            "DarkestDungeon Project Title sync: skipped, %s", _SKIP_NO_DISPLAY
        )
        return ProjectTitleSyncResult(
            status="skipped", reason=_SKIP_NO_DISPLAY, path=str(target)
        )

    try:
        action = _write_title(target, display_name)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "DarkestDungeon Project Title sync failed path=%s error=%s",
            target,
            exc,
        )
        return ProjectTitleSyncResult(
            status="failed", reason=str(exc), path=str(target)
        )

    if action == _SKIP_NO_TITLE:
        logger.info(
            "DarkestDungeon Project Title sync: skipped, %s", _SKIP_NO_TITLE
        )
        return ProjectTitleSyncResult(
            status="skipped", reason=_SKIP_NO_TITLE, path=str(target)
        )

    if action == "updated":
        try:
            _note_target_hash(source, target)
        except OSError as exc:
            logger.warning(
                "DarkestDungeon Project Title sync hash update failed path=%s error=%s",
                target,
                exc,
            )
        logger.info(
            "DarkestDungeon Project Title sync: updated path=%s", target
        )
        return ProjectTitleSyncResult(
            status="updated", reason="updated", path=str(target), changed=True
        )

    return ProjectTitleSyncResult(
        status="skipped", reason=action, path=str(target)
    )
