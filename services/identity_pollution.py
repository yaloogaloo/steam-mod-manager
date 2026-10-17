"""Identity pollution **detect** (scan + plan). Apply lives on IdentityRepairService.

Workspace ID is display-only. Unique Mod identity is always::

    (platform, app_id, external_id)

This module:
  1. Scans for historical pollution (cross-game workspace collisions,
     cross-platform external_id collisions, app_id=0 identity rows).
  2. Emits a Repair Report (JSON-serializable) with planned actions.

Mutating apply is owned by ``IdentityRepairService.repair()``. Never uses
workspace_id to guess identity. Never deletes Mod rows.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.mod_platform import (
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    is_internal_mod_id,
    normalize_platform,
)

logger = logging.getLogger(__name__)

_NEXUS_GAME_SLUG_RE = re.compile(
    r"nexusmods\.com/([a-z0-9\-]+)/mods/(\d+)", re.IGNORECASE
)

# Common Nexus game slug → Steam app_id (best-effort repair only).
_NEXUS_SLUG_TO_APP: dict[str, int] = {
    "stardewvalley": 413150,
    "baldursgate3": 1086940,
    "cyberpunk2077": 1091500,
    "witcher3": 292030,
    "kingdomcomedeliverance2": 1771300,
    "skyrimspecialedition": 489830,
    "fallout4": 377160,
    "monsterhunterworld": 582010,
    "eldenring": 1245620,
}


@dataclass
class PollutionRow:
    workspace_id: str = ""
    mod_id: str = ""
    game_id: int = 0
    platform: str = ""
    external_id: str = ""
    name: str = ""
    source_url: str = ""
    kind: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RepairAction:
    mod_id: str
    action: str
    details: dict[str, Any] = field(default_factory=dict)
    confidence: str = "MEDIUM"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IdentityPollutionReport:
    generated_at: str = ""
    cross_game_workspace: list[PollutionRow] = field(default_factory=list)
    cross_platform_external: list[PollutionRow] = field(default_factory=list)
    app_id_zero: list[PollutionRow] = field(default_factory=list)
    incomplete_identity: list[PollutionRow] = field(default_factory=list)
    actions: list[RepairAction] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "counts": {
                "cross_game_workspace": len(self.cross_game_workspace),
                "cross_platform_external": len(self.cross_platform_external),
                "app_id_zero": len(self.app_id_zero),
                "incomplete_identity": len(self.incomplete_identity),
                "actions": len(self.actions),
            },
            "cross_game_workspace": [r.to_dict() for r in self.cross_game_workspace],
            "cross_platform_external": [
                r.to_dict() for r in self.cross_platform_external
            ],
            "app_id_zero": [r.to_dict() for r in self.app_id_zero],
            "incomplete_identity": [r.to_dict() for r in self.incomplete_identity],
            "actions": [a.to_dict() for a in self.actions],
            "notes": list(self.notes),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _row_name(row: Any) -> str:
    return str(row["display_name"] or row["title"] or "").strip()


def _infer_app_id_from_nexus_url(url: str) -> int:
    m = _NEXUS_GAME_SLUG_RE.search(str(url or ""))
    if not m:
        return 0
    slug = m.group(1).lower()
    return int(_NEXUS_SLUG_TO_APP.get(slug, 0))


def scan_identity_pollution(db: Any) -> IdentityPollutionReport:
    """Read-only scan — never mutates."""
    report = IdentityPollutionReport(generated_at=_utc_now())
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(  # noqa: SLF001
            """
            SELECT mod_id, app_id, platform, external_id, workspace_id,
                   title, display_name, source_url
            FROM mods
            """
        ).fetchall()

    by_ws: dict[str, list[Any]] = defaultdict(list)

    for row in rows:
        mid = str(row["mod_id"])
        app = int(row["app_id"] or 0)
        plat = normalize_platform(str(row["platform"] or ""))
        ext = str(row["external_id"] or "").strip()
        ws = str(row["workspace_id"] or "").strip()
        url = str(row["source_url"] or "").strip()
        name = _row_name(row)

        if ws:
            by_ws[ws].append(row)

        if app <= 0 and plat and plat != PLATFORM_STEAM and ext:
            report.app_id_zero.append(
                PollutionRow(
                    workspace_id=ws,
                    mod_id=mid,
                    game_id=app,
                    platform=plat,
                    external_id=ext,
                    name=name,
                    source_url=url,
                    kind="app_id_zero",
                    notes=["non-Steam identity without game scope"],
                )
            )

        incomplete = (
            (not plat or plat == "?")
            or (not ext and plat != PLATFORM_STEAM)
            or (app <= 0 and plat not in ("", PLATFORM_STEAM))
        )
        if incomplete and (ws or ext or url):
            report.incomplete_identity.append(
                PollutionRow(
                    workspace_id=ws,
                    mod_id=mid,
                    game_id=app,
                    platform=plat,
                    external_id=ext,
                    name=name,
                    source_url=url,
                    kind="incomplete_identity",
                )
            )

    for ws, items in by_ws.items():
        apps = {int(i["app_id"] or 0) for i in items}
        if len(items) > 1 and len(apps) > 1:
            for i in items:
                report.cross_game_workspace.append(
                    PollutionRow(
                        workspace_id=ws,
                        mod_id=str(i["mod_id"]),
                        game_id=int(i["app_id"] or 0),
                        platform=normalize_platform(str(i["platform"] or "")),
                        external_id=str(i["external_id"] or ""),
                        name=_row_name(i),
                        source_url=str(i["source_url"] or ""),
                        kind="cross_game_workspace",
                        notes=["same workspace_id, different app_id"],
                    )
                )

    by_ext: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        ext = str(row["external_id"] or "").strip()
        if ext and not is_internal_mod_id(ext):
            by_ext[ext].append(row)
    for ext, items in by_ext.items():
        plats = {normalize_platform(str(i["platform"] or "")) for i in items}
        if len(plats) > 1:
            for i in items:
                report.cross_platform_external.append(
                    PollutionRow(
                        workspace_id=str(i["workspace_id"] or ""),
                        mod_id=str(i["mod_id"]),
                        game_id=int(i["app_id"] or 0),
                        platform=normalize_platform(str(i["platform"] or "")),
                        external_id=ext,
                        name=_row_name(i),
                        source_url=str(i["source_url"] or ""),
                        kind="cross_platform_external",
                        notes=["same external_id digits on different platforms"],
                    )
                )

    report.actions = plan_identity_pollution_repair(report)
    report.notes.append(
        "Workspace ID is display-only; identity is (platform, app_id, external_id)."
    )
    report.notes.append(
        "Detect-only. Apply via IdentityRepairService.repair()."
    )
    return report


def plan_identity_pollution_repair(
    report: IdentityPollutionReport,
) -> list[RepairAction]:
    """Derive gated repair actions from a scan report (no DB writes)."""
    actions: list[RepairAction] = []
    seen: set[str] = set()

    for row in report.app_id_zero:
        if row.mod_id in seen:
            continue
        inferred = _infer_app_id_from_nexus_url(row.source_url)
        if row.platform == PLATFORM_NEXUS and inferred > 0:
            actions.append(
                RepairAction(
                    mod_id=row.mod_id,
                    action="INFER_APP_ID",
                    details={
                        "new_app_id": inferred,
                        "from_url": row.source_url,
                        "external_id": row.external_id,
                        "platform": row.platform,
                    },
                    confidence="HIGH",
                )
            )
            seen.add(row.mod_id)
        else:
            actions.append(
                RepairAction(
                    mod_id=row.mod_id,
                    action="MARK_UNRESOLVED",
                    details={
                        "reason": "cannot_infer_app_id",
                        "platform": row.platform,
                        "external_id": row.external_id,
                    },
                    confidence="LOW",
                )
            )
            seen.add(row.mod_id)

    by_ws: dict[str, list[PollutionRow]] = defaultdict(list)
    for row in report.cross_game_workspace:
        by_ws[row.workspace_id].append(row)
    for ws, items in by_ws.items():
        keeper = next((r for r in items if r.game_id > 0), items[0])
        for row in items:
            if row.mod_id == keeper.mod_id:
                continue
            actions.append(
                RepairAction(
                    mod_id=row.mod_id,
                    action="REASSIGN_WORKSPACE_UNIQUE",
                    details={
                        "old_workspace_id": ws,
                        "keeper_mod_id": keeper.mod_id,
                        "reason": "cross_game_workspace_collision",
                    },
                    confidence="HIGH",
                )
            )

    for row in report.cross_platform_external:
        actions.append(
            RepairAction(
                mod_id=row.mod_id,
                action="NO_LINK",
                details={
                    "reason": "cross_platform_external_must_remain_distinct",
                    "external_id": row.external_id,
                    "platform": row.platform,
                },
                confidence="HIGH",
            )
        )

    return actions


def apply_identity_pollution_repair(
    db: Any,
    report: IdentityPollutionReport | None = None,
    *,
    apply: bool = False,
    library_root: str | Path | None = None,
) -> dict[str, Any]:
    """Compatibility shim → ``IdentityRepairService.repair`` (pollution only).

    Prefer calling ``IdentityRepairService`` directly. Never deletes rows.
    """
    from core.paths import default_mod_library
    from services.identity_repair_service import (
        IdentityRepairDetection,
        get_identity_repair_service,
    )

    scanned = report or scan_identity_pollution(db)
    detection = IdentityRepairDetection(pollution=scanned)
    root = Path(library_root) if library_root else Path(default_mod_library())
    result = get_identity_repair_service().repair(
        db,
        root,
        apply=apply,
        detection=detection,
        include_entity=False,
        include_field_scrubs=False,
        include_pollution=True,
    )
    payload = dict(result.pollution_apply or {})
    payload.setdefault("apply", apply)
    payload.setdefault("planned", len(scanned.actions))
    payload.setdefault("applied", [])
    payload.setdefault("skipped", [])
    if not apply:
        payload["actions"] = [a.to_dict() for a in scanned.actions]
    return payload


def write_pollution_report(
    report: IdentityPollutionReport,
    *,
    path: str | Path | None = None,
) -> Path:
    # Prefer _tmp/audits when caller omits path (local disposable dump).
    out = (
        Path(path)
        if path
        else Path(__file__).resolve().parents[1]
        / "_tmp"
        / "audits"
        / "identity_pollution_report.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out
