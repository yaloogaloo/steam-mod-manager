"""Path Lifecycle Phase 2 — audit stale deploy paths and apply safe repairs.

Scans configured absolute paths that no longer exist after environment moves
(Steam Library drive changes, etc.).

Does **not**:
- treat path / folder name / workspace_id as entity identity
- auto-guess drive letters or migrate paths implicitly
- modify ``internal_id`` / Identity Contract fields
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from services.deploy_path_lifecycle import (
    CUSTOM_DEPLOY_PATH_MISSING,
    GAME_CONFIG_PATH_MISSING,
    validate_custom_deploy_target,
    validate_game_install_dir,
    validate_game_mod_path,
)

logger = logging.getLogger(__name__)

REPAIR_CLEAR_CUSTOM = "clear_custom_deploy_path"
REPAIR_UPDATE_CUSTOM = "update_custom_deploy_path"
REPAIR_UPDATE_GAME = "update_game_path"
REPAIR_IGNORE = "ignore"

RepairAction = Literal[
    "clear_custom_deploy_path",
    "update_custom_deploy_path",
    "update_game_path",
    "ignore",
]

_LOCK = threading.Lock()
_REPAIR_HISTORY: list["PathRepairRecord"] = []


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _is_absolute_path(raw: str) -> bool:
    text = _text(raw)
    if not text:
        return False
    try:
        return Path(text).expanduser().is_absolute()
    except OSError:
        return False


def _repair_log_path() -> Path:
    from core.paths import data_dir

    return Path(data_dir()) / "path_lifecycle_repair.jsonl"


@dataclass(frozen=True, slots=True)
class PathAuditFinding:
    """One stale configured deploy path."""

    entity_kind: str  # "mod" | "game"
    internal_id: str  # mods.mod_id PK as string; empty for game rows
    app_id: int
    path_field: str  # install_path | mod_path | custom_deploy_path
    configured_path: str
    error_code: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PathAuditReport:
    scanned_games: int = 0
    scanned_mods: int = 0
    findings: list[PathAuditFinding] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned_games": self.scanned_games,
            "scanned_mods": self.scanned_mods,
            "finding_count": len(self.findings),
            "findings": [f.as_dict() for f in self.findings],
        }


@dataclass(frozen=True, slots=True)
class PathRepairRecord:
    action: str
    entity_kind: str
    internal_id: str
    app_id: int
    path_field: str
    before_path: str
    after_path: str
    timestamp: str
    ignored: bool = False
    success: bool = True
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PathRepairResult:
    success: bool
    record: PathRepairRecord | None = None
    error: str = ""
    # Post-repair re-validation for the touched field (None when ignored).
    still_missing: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "error": self.error,
            "still_missing": self.still_missing,
            "record": self.record.as_dict() if self.record else None,
        }


def reset_path_repair_history() -> None:
    """Test helper — clear in-memory repair history."""
    with _LOCK:
        _REPAIR_HISTORY.clear()


def list_path_repair_history() -> list[PathRepairRecord]:
    with _LOCK:
        return list(_REPAIR_HISTORY)


def _append_repair_record(record: PathRepairRecord) -> None:
    with _LOCK:
        _REPAIR_HISTORY.append(record)
    try:
        path = _repair_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")
    except OSError:
        logger.debug("path repair log write failed", exc_info=True)


def audit_deploy_paths(*, db: Any = None) -> PathAuditReport:
    """
    Scan ``games.install_path`` / ``games.mod_path`` and ``mods.custom_deploy_path``.

    Records absolute configured paths that fail Phase-1 validators.
    Never invents entity identity from path / workspace_id.
    """
    from core.db_manager import get_db

    database = db if db is not None else get_db()
    report = PathAuditReport()

    games = list(database.list_games() or [])
    report.scanned_games = len(games)
    for game in games:
        app_id = int(getattr(game, "app_id", 0) or 0)
        if app_id <= 0:
            continue
        cfg = database.get_game_deploy_config(app_id)
        if cfg is None:
            continue
        install = _text(getattr(cfg, "install_path", ""))
        mod_path = _text(getattr(cfg, "mod_path", ""))
        if install and _is_absolute_path(install):
            err = validate_game_install_dir(install, app_id=app_id)
            if err:
                report.findings.append(
                    PathAuditFinding(
                        entity_kind="game",
                        internal_id="",
                        app_id=app_id,
                        path_field="install_path",
                        configured_path=install,
                        error_code=GAME_CONFIG_PATH_MISSING,
                    )
                )
        if mod_path and _is_absolute_path(mod_path):
            err = validate_game_mod_path(mod_path, app_id=app_id)
            if err:
                report.findings.append(
                    PathAuditFinding(
                        entity_kind="game",
                        internal_id="",
                        app_id=app_id,
                        path_field="mod_path",
                        configured_path=mod_path,
                        error_code=GAME_CONFIG_PATH_MISSING,
                    )
                )

    # Mod custom_deploy_path — keyed by internal_id (mods.mod_id PK).
    with database._lock:
        rows = database._conn.execute(
            """
            SELECT mod_id, app_id, custom_deploy_path
            FROM mods
            WHERE TRIM(COALESCE(custom_deploy_path, '')) != ''
            ORDER BY mod_id
            """
        ).fetchall()
    report.scanned_mods = len(rows)
    for row in rows:
        mid = _text(row["mod_id"])
        app_id = int(row["app_id"] or 0)
        custom = _text(row["custom_deploy_path"])
        if not custom or not _is_absolute_path(custom):
            continue
        err = validate_custom_deploy_target(custom, app_id=app_id)
        if err:
            report.findings.append(
                PathAuditFinding(
                    entity_kind="mod",
                    internal_id=mid,
                    app_id=app_id,
                    path_field="custom_deploy_path",
                    configured_path=custom,
                    error_code=CUSTOM_DEPLOY_PATH_MISSING,
                )
            )
    return report


def _read_mod_custom_path(db: Any, internal_id: str) -> tuple[int, str]:
    mid = int(str(internal_id).strip())
    with db._lock:
        row = db._conn.execute(
            """
            SELECT app_id, custom_deploy_path FROM mods WHERE mod_id = ?
            """,
            (mid,),
        ).fetchone()
    if row is None:
        raise ValueError(f"unknown mod internal_id={internal_id}")
    return int(row["app_id"] or 0), _text(row["custom_deploy_path"])


def _read_game_path(db: Any, app_id: int, path_field: str) -> str:
    cfg = db.get_game_deploy_config(app_id)
    if cfg is None:
        raise ValueError(f"unknown game app_id={app_id}")
    if path_field == "install_path":
        return _text(getattr(cfg, "install_path", ""))
    if path_field == "mod_path":
        return _text(getattr(cfg, "mod_path", ""))
    raise ValueError(f"unsupported game path_field={path_field}")


def _field_still_missing(
    *,
    db: Any,
    path_field: str,
    app_id: int,
    internal_id: str,
) -> bool:
    if path_field == "custom_deploy_path":
        _aid, custom = _read_mod_custom_path(db, internal_id)
        if not custom:
            return False
        return validate_custom_deploy_target(custom, app_id=app_id) is not None
    if path_field == "install_path":
        raw = _read_game_path(db, app_id, "install_path")
        if not raw:
            return False
        return validate_game_install_dir(raw, app_id=app_id) is not None
    if path_field == "mod_path":
        raw = _read_game_path(db, app_id, "mod_path")
        if not raw:
            return False
        return validate_game_mod_path(raw, app_id=app_id) is not None
    return False


def repair_deploy_path(
    *,
    action: RepairAction | str,
    db: Any = None,
    internal_id: str = "",
    app_id: int = 0,
    path_field: str = "",
    new_path: str = "",
) -> PathRepairResult:
    """
    Apply one explicit path repair.

    Actions
    -------
    - ``clear_custom_deploy_path``: clear Mod override → inherit ``game.mod_path``
    - ``update_custom_deploy_path``: set ``mods.custom_deploy_path`` to *new_path*
    - ``update_game_path``: set ``games.install_path`` or ``games.mod_path``
    - ``ignore``: record only; no DB write

    Never mutates ``internal_id`` / identity columns.
    """
    from core.db_manager import get_db

    database = db if db is not None else get_db()
    act = _text(action)
    field = _text(path_field)
    mid = _text(internal_id)
    aid = int(app_id or 0)
    after = _text(new_path)
    ts = _utc_now()

    try:
        if act == REPAIR_IGNORE:
            before = ""
            entity_kind = "mod" if mid else "game"
            if field == "custom_deploy_path" and mid:
                aid, before = _read_mod_custom_path(database, mid)
                entity_kind = "mod"
            elif field in ("install_path", "mod_path") and aid > 0:
                before = _read_game_path(database, aid, field)
                entity_kind = "game"
            record = PathRepairRecord(
                action=REPAIR_IGNORE,
                entity_kind=entity_kind,
                internal_id=mid,
                app_id=aid,
                path_field=field or "unknown",
                before_path=before,
                after_path=before,
                timestamp=ts,
                ignored=True,
                success=True,
            )
            _append_repair_record(record)
            return PathRepairResult(success=True, record=record, still_missing=None)

        if act == REPAIR_CLEAR_CUSTOM:
            if not mid.isdigit():
                return PathRepairResult(
                    success=False,
                    error="clear_custom_deploy_path requires internal_id",
                )
            aid, before = _read_mod_custom_path(database, mid)
            # Identity guard: capture PK before write.
            identity_before = mid
            database.update_mod_user_metadata(mid, {"custom_deploy_path": ""})
            identity_after = mid
            if identity_after != identity_before:
                return PathRepairResult(
                    success=False,
                    error="repair refused: internal_id changed",
                )
            record = PathRepairRecord(
                action=REPAIR_CLEAR_CUSTOM,
                entity_kind="mod",
                internal_id=mid,
                app_id=aid,
                path_field="custom_deploy_path",
                before_path=before,
                after_path="",
                timestamp=ts,
            )
            _append_repair_record(record)
            still = _field_still_missing(
                db=database,
                path_field="custom_deploy_path",
                app_id=aid,
                internal_id=mid,
            )
            return PathRepairResult(success=True, record=record, still_missing=still)

        if act == REPAIR_UPDATE_CUSTOM:
            if not mid.isdigit():
                return PathRepairResult(
                    success=False,
                    error="update_custom_deploy_path requires internal_id",
                )
            if not after:
                return PathRepairResult(
                    success=False,
                    error="update_custom_deploy_path requires new_path",
                )
            aid, before = _read_mod_custom_path(database, mid)
            # Explicit update only — still validate lifecycle of the new path.
            err = validate_custom_deploy_target(after, app_id=aid)
            if err:
                return PathRepairResult(success=False, error=err)
            identity_before = mid
            database.update_mod_user_metadata(mid, {"custom_deploy_path": after})
            if mid != identity_before:
                return PathRepairResult(
                    success=False,
                    error="repair refused: internal_id changed",
                )
            record = PathRepairRecord(
                action=REPAIR_UPDATE_CUSTOM,
                entity_kind="mod",
                internal_id=mid,
                app_id=aid,
                path_field="custom_deploy_path",
                before_path=before,
                after_path=after,
                timestamp=ts,
            )
            _append_repair_record(record)
            still = _field_still_missing(
                db=database,
                path_field="custom_deploy_path",
                app_id=aid,
                internal_id=mid,
            )
            return PathRepairResult(success=True, record=record, still_missing=still)

        if act == REPAIR_UPDATE_GAME:
            if aid <= 0:
                return PathRepairResult(
                    success=False,
                    error="update_game_path requires app_id",
                )
            if field not in ("install_path", "mod_path"):
                return PathRepairResult(
                    success=False,
                    error="update_game_path requires path_field=install_path|mod_path",
                )
            if not after:
                return PathRepairResult(
                    success=False,
                    error="update_game_path requires new_path",
                )
            before = _read_game_path(database, aid, field)
            if field == "install_path":
                err = validate_game_install_dir(after, app_id=aid)
            else:
                err = validate_game_mod_path(after, app_id=aid)
            if err:
                return PathRepairResult(success=False, error=err)
            kwargs = {field: after}
            database.update_game_deploy_config(aid, **kwargs)
            record = PathRepairRecord(
                action=REPAIR_UPDATE_GAME,
                entity_kind="game",
                internal_id="",
                app_id=aid,
                path_field=field,
                before_path=before,
                after_path=after,
                timestamp=ts,
            )
            _append_repair_record(record)
            still = _field_still_missing(
                db=database,
                path_field=field,
                app_id=aid,
                internal_id="",
            )
            return PathRepairResult(success=True, record=record, still_missing=still)

        return PathRepairResult(success=False, error=f"unknown repair action={act}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("repair_deploy_path failed action=%s", act, exc_info=True)
        return PathRepairResult(success=False, error=str(exc))


def reaudit_after_repair(
    *,
    db: Any = None,
    internal_id: str = "",
    app_id: int = 0,
    path_field: str = "",
) -> list[PathAuditFinding]:
    """Re-scan and return findings that still match the repaired entity/field."""
    report = audit_deploy_paths(db=db)
    mid = _text(internal_id)
    aid = int(app_id or 0)
    field = _text(path_field)
    out: list[PathAuditFinding] = []
    for finding in report.findings:
        if field and finding.path_field != field:
            continue
        if mid and finding.internal_id != mid:
            continue
        if aid > 0 and finding.app_id != aid:
            continue
        if mid or aid > 0 or field:
            out.append(finding)
    return out
