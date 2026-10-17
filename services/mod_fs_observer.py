"""Mod filesystem observation — unified L0 / L1 / L2 lifecycle.

ARCHITECTURE RULE
-----------------
``observe_mod_fs(internal_id, level)`` is the single entry for cheap probe,
content-state reconcile, and deep local sync.

Levels
------
0  cheap probe — root exists + root mtime + observation stamp
1  state reconcile — folder_present + content payload via content_status_eval
2  deep sync — rescan_mod_folder + reconcile_local_files (+ L1)

Constraints
-----------
- content_status writes only through ``persist_evaluated_content_status``
- Resolve path from Internal ID → ``last_known_path`` only
  (no new workspace_id / path identity lookup)
- L1 / L2 must not run on the UI thread
- Does not invent identity; does not change Identity Contract
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LEVEL_PROBE = 0
LEVEL_STATE = 1
LEVEL_DEEP = 2

__all__ = [
    "LEVEL_PROBE",
    "LEVEL_STATE",
    "LEVEL_DEEP",
    "FsObservationResult",
    "observe_mod_fs",
    "observe_mods_fs_batch",
    "touch_observation_stamp",
    "schedule_observe_mods_fs_batch",
    "note_content_status_projection_touch",
    "note_mod_files_projection_touch",
    "begin_projection_defer",
    "end_projection_defer",
    "drain_projection_touch_ids",
    "set_ui_thread_guard",
]


@dataclass
class FsObservationResult:
    internal_id: str
    level: int
    root_exists: bool
    root_mtime: float | None
    observed_at: str
    dirty: bool = False
    folder_present: bool | None = None
    content_status: str = ""
    projection_touched: bool = False
    notes: list[str] = field(default_factory=list)


_LOCK = threading.Lock()
_defer_projection = False
_pending_touch_ids: list[str] = []
_batch_thread: threading.Thread | None = None
# Tests may disable when QApplication exists on the pytest main thread.
_ui_thread_guard = True


def set_ui_thread_guard(enabled: bool) -> None:
    """Test helper — toggle L1/L2 UI-thread refusal."""
    global _ui_thread_guard
    _ui_thread_guard = bool(enabled)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _assert_not_ui_thread(level: int) -> None:
    if level < LEVEL_STATE or not _ui_thread_guard:
        return
    try:
        from PySide6.QtCore import QCoreApplication, QThread
    except Exception:  # noqa: BLE001
        return
    app = QCoreApplication.instance()
    if app is None:
        return
    if QThread.currentThread() is app.thread():
        raise RuntimeError(
            f"observe_mod_fs level={level} must not run on the UI thread"
        )


def begin_projection_defer() -> None:
    """Batch FS/content mutations (e.g. reconcile) — defer UI projection notify."""
    global _defer_projection
    with _LOCK:
        _defer_projection = True


def end_projection_defer() -> list[str]:
    """End deferral and return pending internal_ids (does not clear drain queue)."""
    global _defer_projection
    with _LOCK:
        _defer_projection = False
        return list(dict.fromkeys(_pending_touch_ids))


def drain_projection_touch_ids() -> list[str]:
    """Consume pending projection touch IDs accumulated while deferred."""
    global _pending_touch_ids
    with _LOCK:
        out = list(dict.fromkeys(_pending_touch_ids))
        _pending_touch_ids = []
        return out


def note_content_status_projection_touch(internal_id: str | int) -> None:
    """Record / notify Library projection after content_status/folder_present change."""
    _note_projection_touch(internal_id, reason="content_status")


def note_mod_files_projection_touch(internal_id: str | int) -> None:
    """Record / notify Library projection after mod_files change."""
    _note_projection_touch(internal_id, reason="mod_files")


def _note_projection_touch(internal_id: str | int, *, reason: str) -> None:
    mid = str(internal_id or "").strip()
    if not mid:
        return
    with _LOCK:
        deferred = _defer_projection
        if deferred:
            _pending_touch_ids.append(mid)
    if deferred:
        logger.debug(
            "fs observation projection deferred internal_id=%s reason=%s",
            mid,
            reason,
        )
        return
    try:
        from services.mod_projection_events import notify_mod_changed

        notify_mod_changed(mid)
    except Exception:  # noqa: BLE001
        logger.debug(
            "projection notify failed internal_id=%s reason=%s",
            mid,
            reason,
            exc_info=True,
        )


def _bound_root(row: dict[str, Any] | None) -> Path | None:
    """Path from Internal ID row only — no workspace / invent lookup."""
    if not row:
        return None
    raw = str(row.get("last_known_path") or "").strip()
    if not raw:
        return None
    return Path(raw)


def _probe_root(root: Path | None) -> tuple[bool, float | None]:
    if root is None:
        return False, None
    try:
        if not root.is_dir():
            return False, None
        return True, float(root.stat().st_mtime)
    except OSError:
        return False, None


def _is_dirty(
    *,
    root_exists: bool,
    root_mtime: float | None,
    row: dict[str, Any],
) -> bool:
    db_present = bool(int(row.get("folder_present") or 0))
    if root_exists != db_present:
        return True
    prev_obs = str(row.get("fs_observed_at") or "").strip()
    if not prev_obs:
        # First stamp — dirty only when presence disagrees (handled above)
        # or when DB claims healthy folder but we cannot see it.
        return False
    prev_mtime = row.get("fs_root_mtime")
    if root_exists:
        if prev_mtime is None:
            return True
        try:
            if abs(float(prev_mtime) - float(root_mtime or 0.0)) > 1e-6:
                return True
        except (TypeError, ValueError):
            return True
    return False


def touch_observation_stamp(
    internal_id: str | int,
    *,
    db: Any = None,
    managed_path: str | Path | None = None,
) -> FsObservationResult:
    """Update observation stamp only (deploy finish / post-mutation)."""
    return observe_mod_fs(
        internal_id,
        LEVEL_PROBE,
        db=db,
        managed_path=managed_path,
        persist_stamp=True,
        force_stamp=True,
    )


def observe_mod_fs(
    internal_id: str | int,
    level: int,
    *,
    db: Any = None,
    managed_path: str | Path | None = None,
    persist_stamp: bool = True,
    force_stamp: bool = False,
    notify_projection: bool = True,
) -> FsObservationResult:
    """
    Observe one Mod's filesystem at *level* (0 / 1 / 2).

    Path resolution: Internal ID → ``last_known_path`` (optional hint override
    when the hint is an existing directory). No workspace_id / invent lookup.
    """
    from core.db_manager import get_db

    _assert_not_ui_thread(int(level))
    database = db if db is not None else get_db()
    mid = str(internal_id or "").strip()
    if not mid or not mid.isdigit():
        return FsObservationResult(
            internal_id=mid,
            level=int(level),
            root_exists=False,
            root_mtime=None,
            observed_at=_utc_now(),
            notes=["invalid_internal_id"],
        )

    row = database.get_mod_fs_observation(mid) or database.get_mod_backup_row(mid) or {}
    hint = Path(managed_path) if managed_path is not None else None
    root = hint if hint is not None and hint.is_dir() else _bound_root(row)
    if hint is not None and not hint.is_dir() and root is None:
        root = hint

    root_exists, root_mtime = _probe_root(root if root is not None else None)
    observed_at = _utc_now()
    dirty = _is_dirty(root_exists=root_exists, root_mtime=root_mtime, row=row)
    notes: list[str] = []
    if dirty:
        notes.append("dirty")

    result = FsObservationResult(
        internal_id=mid,
        level=LEVEL_PROBE,
        root_exists=root_exists,
        root_mtime=root_mtime,
        observed_at=observed_at,
        dirty=dirty,
        folder_present=root_exists,
        notes=list(notes),
    )

    lvl = int(level)
    if lvl >= LEVEL_DEEP:
        result = _observe_l2(
            mid,
            root=root,
            root_exists=root_exists,
            root_mtime=root_mtime,
            observed_at=observed_at,
            dirty=dirty,
            db=database,
            notify_projection=notify_projection,
            base_notes=notes,
        )
    elif lvl >= LEVEL_STATE:
        result = _observe_l1(
            mid,
            root=root if root_exists else None,
            root_exists=root_exists,
            root_mtime=root_mtime,
            observed_at=observed_at,
            dirty=dirty,
            db=database,
            notify_projection=notify_projection,
            base_notes=notes,
        )

    if persist_stamp:
        try:
            database.update_mod_fs_observation(
                mid,
                observed_at=result.observed_at,
                root_mtime=result.root_mtime if result.root_exists else None,
                root_exists=result.root_exists,
            )
        except Exception:  # noqa: BLE001
            logger.debug("fs observation stamp failed for %s", mid, exc_info=True)
            result.notes.append("stamp_failed")

    return result


def _observe_l1(
    mid: str,
    *,
    root: Path | None,
    root_exists: bool,
    root_mtime: float | None,
    observed_at: str,
    dirty: bool,
    db: Any,
    notify_projection: bool,
    base_notes: list[str],
) -> FsObservationResult:
    from services.content_status_eval import persist_evaluated_content_status
    from services.library_status import CONTENT_CONTENT_MISSING

    notes = list(base_notes)
    notes.append("l1_state")
    prev = db.get_mod_fs_observation(mid) or {}
    prev_cs = str(prev.get("content_status") or "").strip()
    prev_present = bool(int(prev.get("folder_present") or 0))

    if not root_exists:
        cs = persist_evaluated_content_status(
            mid,
            None,
            db=db,
            folder_present=False,
            sync_sticky_marker=False,
        )
        notes.append("folder_absent")
    else:
        row = db.get_mod_backup_row(mid) or {}
        cs = persist_evaluated_content_status(
            mid,
            root,
            db=db,
            folder_present=True,
            backup_status=str(row.get("backup_status") or ""),
            sync_sticky_marker=True,
        )

    changed = cs != prev_cs or root_exists != prev_present
    touched = False
    # persist_evaluated already notes projection when content columns change;
    # re-notify only when we need an explicit touch and defer is off.
    if changed and notify_projection:
        # note already fired inside persist when columns changed; ensure touch
        # for presence-only edge cases that somehow skipped write.
        if cs == prev_cs and root_exists == prev_present:
            pass
        touched = True
        notes.append("projection_touch")

    missing = cs == CONTENT_CONTENT_MISSING
    if missing:
        notes.append("content_missing")

    return FsObservationResult(
        internal_id=mid,
        level=LEVEL_STATE,
        root_exists=root_exists,
        root_mtime=root_mtime,
        observed_at=observed_at,
        dirty=dirty,
        folder_present=root_exists,
        content_status=cs,
        projection_touched=touched,
        notes=notes,
    )


def _observe_l2(
    mid: str,
    *,
    root: Path | None,
    root_exists: bool,
    root_mtime: float | None,
    observed_at: str,
    dirty: bool,
    db: Any,
    notify_projection: bool,
    base_notes: list[str],
) -> FsObservationResult:
    """Deep sync — reuse rescan_mod_folder + reconcile_local_files via refresh."""
    from services.mod_refresh import reconcile_local_state

    notes = list(base_notes)
    notes.append("l2_deep")
    local = reconcile_local_state(mid, root, db=db)
    notes.extend(local.notes)
    if "archive_source_reconciled" in local.notes and notify_projection:
        # reconcile_local_state already notes mod_files projection touch
        notes.append("mod_files_changed")

    return FsObservationResult(
        internal_id=mid,
        level=LEVEL_DEEP,
        root_exists=bool(local.folder_present),
        root_mtime=root_mtime if local.folder_present else None,
        observed_at=observed_at,
        dirty=dirty,
        folder_present=bool(local.folder_present),
        content_status=str(local.content_status or ""),
        projection_touched=True,
        notes=notes,
    )


def observe_mods_fs_batch(
    internal_ids: Iterable[str | int] | None = None,
    level: int = LEVEL_PROBE,
    *,
    db: Any = None,
    notify_projection: bool = True,
) -> list[FsObservationResult]:
    """Batch observation. Default L0 — never a full-library L2 scan."""
    from core.db_manager import get_db

    _assert_not_ui_thread(int(level))
    if int(level) >= LEVEL_DEEP:
        raise ValueError(
            "observe_mods_fs_batch refuses level>=2 (no full-library deep scan)"
        )

    database = db if db is not None else get_db()
    if internal_ids is None:
        ids = database.list_mod_ids_for_fs_observe()
    else:
        ids = [str(x).strip() for x in internal_ids if str(x).strip()]

    out: list[FsObservationResult] = []
    for mid in ids:
        try:
            out.append(
                observe_mod_fs(
                    mid,
                    level,
                    db=database,
                    notify_projection=notify_projection,
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug("batch observe failed for %s", mid, exc_info=True)
            out.append(
                FsObservationResult(
                    internal_id=mid,
                    level=int(level),
                    root_exists=False,
                    root_mtime=None,
                    observed_at=_utc_now(),
                    notes=["batch_error"],
                )
            )
    return out


def schedule_observe_mods_fs_batch(
    level: int = LEVEL_PROBE,
    *,
    internal_ids: Iterable[str | int] | None = None,
    db: Any = None,
) -> bool:
    """Daemon-thread batch observe (UI-safe). Refuses L2."""
    global _batch_thread
    if int(level) >= LEVEL_DEEP:
        logger.warning("schedule_observe_mods_fs_batch refused level=%s", level)
        return False
    if int(level) >= LEVEL_STATE:
        # L1 batch is allowed off UI thread; caller must accept cost.
        pass

    ids = list(internal_ids) if internal_ids is not None else None

    def _worker() -> None:
        try:
            observe_mods_fs_batch(ids, level, db=db)
        except Exception:  # noqa: BLE001
            logger.exception("scheduled batch fs observe failed level=%s", level)

    thread = threading.Thread(
        target=_worker,
        name=f"mod-fs-observe-l{int(level)}",
        daemon=True,
    )
    with _LOCK:
        prev = _batch_thread
        if prev is not None and prev.is_alive() and int(level) == LEVEL_PROBE:
            # Coalesce cheap L0 batches — skip overlapping probe.
            logger.debug("L0 batch already running; skip schedule")
            return False
        _batch_thread = thread
    thread.start()
    return True
