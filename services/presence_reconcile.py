"""Presence Reconcile — LIVE / MISS / Rediscovered for Library Refresh.

Runs off the UI thread. One directory scan per game root. UI consumes
``folder_present`` / projection only — cards and Detail never discover MISS.

After a folder returns (MISS → LIVE / Rediscovery), content_status is
re-evaluated via ``persist_evaluated_content_status`` so stale
``content_missing`` cannot survive when the payload is present again.
Presence alone must never leave ``folder_present=1`` + ``content_missing``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.paths import default_mod_library
from services.file_ops import INFO_DIR_NAME, read_info_metadata_dict
from services.mod_identity import read_internal_id
from services.mod_presence import RediscoveryIndex, rediscover_entity_path

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_worker: threading.Thread | None = None
_pending: tuple[str, str] | None = None
_LAST_STATS: "PresenceReconcileStats | None" = None
_LAST_SCHEDULE_UI_MS = 0.0


@dataclass
class PresenceReconcileStats:
    mods_examined: int = 0
    game_roots_examined: int = 0
    directory_scans: int = 0
    filesystem_stat_calls: int = 0
    info_reads: int = 0
    rediscovery_attempted: int = 0
    live: int = 0
    miss: int = 0
    rediscovered: int = 0
    unchanged: int = 0
    content_reevaluated: int = 0
    content_status_cleared: int = 0
    duration_ms: float = 0.0
    ui_blocking_ms: float = 0.0
    changed_ids: list[str] = field(default_factory=list)
    roots: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mods_examined": self.mods_examined,
            "game_roots_examined": self.game_roots_examined,
            "directory_scans": int(self.directory_scans),
            "filesystem_stat_calls": self.filesystem_stat_calls,
            "info_reads": self.info_reads,
            "rediscovery_attempted": self.rediscovery_attempted,
            "live": self.live,
            "miss": self.miss,
            "rediscovered": self.rediscovered,
            "unchanged": self.unchanged,
            "content_reevaluated": self.content_reevaluated,
            "content_status_cleared": self.content_status_cleared,
            "duration_ms": round(self.duration_ms, 2),
            "ui_blocking_ms": round(self.ui_blocking_ms, 2),
            "changed": len(self.changed_ids),
            "roots": list(self.roots),
        }


def last_presence_stats() -> dict[str, Any]:
    stats = _LAST_STATS
    if stats is None:
        return PresenceReconcileStats().as_dict()
    return stats.as_dict()


def record_schedule_ui_ms(ms: float) -> None:
    global _LAST_SCHEDULE_UI_MS
    _LAST_SCHEDULE_UI_MS = max(0.0, float(ms))


def _norm_path(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _scan_game_root(root: Path) -> tuple[list[Path], set[str], int]:
    """One ``iterdir`` of a game managed root. No payload walk."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return [], set(), 0
    children: list[Path] = []
    live: set[str] = set()
    for child in entries:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        children.append(child)
        live.add(_norm_path(child))
    return children, live, len(entries)


def _index_unclaimed(
    children: list[Path], claimed: set[str]
) -> tuple[dict[str, list[Path]], int]:
    mapping: dict[str, list[Path]] = {}
    reads = 0
    for child in children:
        if _norm_path(child) in claimed:
            continue
        try:
            if not (child / INFO_DIR_NAME).is_dir():
                continue
            reads += 1
            iid = read_internal_id(read_info_metadata_dict(child) or {})
        except OSError:
            continue
        if iid:
            mapping.setdefault(iid, []).append(child)
    return mapping, reads


def _game_root_for_row(
    row: dict[str, str],
    *,
    library_root: Path,
    folder_by_app: dict[int, str],
) -> Path | None:
    lkp = str(row.get("last_known_path") or "").strip()
    if lkp:
        try:
            parent = Path(lkp).expanduser().parent
            return Path(_norm_path(parent))
        except OSError:
            pass
    try:
        app_id = int(row.get("app_id") or 0)
    except (TypeError, ValueError):
        app_id = 0
    folder = str(folder_by_app.get(app_id) or "").strip()
    if folder:
        return Path(_norm_path(library_root / folder))
    return None


def _reevaluate_content_after_presence(
    database: Any,
    *,
    live_ids: list[str],
    rediscovered_ids: list[str],
    stale_live_ids: list[str],
    miss_ids: list[str],
    stats: PresenceReconcileStats,
    changed: list[str],
) -> None:
    """Re-eval content_status for restored LIVE folders + stale LIVE missing.

    Does not invent health: uses ``persist_evaluated_content_status`` only.
    Never writes content_missing for MISS rows (presence overlay owns MISS).
    """
    from services.content_status_eval import persist_evaluated_content_status
    from services.library_status import CONTENT_CONTENT_MISSING, normalize_content_axis
    from services.mod_fs_observer import (
        begin_projection_defer,
        drain_projection_touch_ids,
        end_projection_defer,
    )

    miss_set = {str(m) for m in miss_ids}
    recheck = [
        m
        for m in dict.fromkeys([*live_ids, *rediscovered_ids, *stale_live_ids])
        if m not in miss_set
    ]
    if not recheck:
        return

    begin_projection_defer()
    try:
        for mid in recheck:
            try:
                brow = database.get_mod_backup_row(mid) or {}
            except Exception:  # noqa: BLE001
                continue
            prev_cs = normalize_content_axis(str(brow.get("content_status") or ""))
            path_s = str(brow.get("last_known_path") or "").strip()
            path: Path | None = Path(path_s) if path_s else None
            present = False
            if path is not None:
                try:
                    present = path.is_dir()
                except OSError:
                    present = False
            if not present:
                # Presence owns MISS; do not stamp content_missing here.
                continue
            try:
                cs = persist_evaluated_content_status(
                    mid,
                    path,
                    db=database,
                    folder_present=True,
                    backup_status=str(brow.get("backup_status") or ""),
                    sync_sticky_marker=True,
                    notify_projection=True,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "presence content re-eval failed mid=%s", mid, exc_info=True
                )
                continue
            stats.content_reevaluated += 1
            new_cs = normalize_content_axis(cs)
            if prev_cs == CONTENT_CONTENT_MISSING and new_cs != CONTENT_CONTENT_MISSING:
                stats.content_status_cleared += 1
            if new_cs != prev_cs:
                changed.append(mid)
    finally:
        end_projection_defer()
        drain_projection_touch_ids()


def reconcile_presence(
    library_root: str | Path | None = None,
    *,
    game_folder: str | None = None,
    db: Any | None = None,
    notify: bool = True,
) -> PresenceReconcileStats:
    """Compute LIVE / MISS / Rediscovered. Never runs Backup repair."""
    global _LAST_STATS
    t0 = time.perf_counter()
    stats = PresenceReconcileStats()
    root = Path(library_root) if library_root is not None else Path(default_mod_library())
    filter_folder = str(game_folder or "").strip()
    if filter_folder in {"全部游戏", "*"}:
        filter_folder = ""

    try:
        database = db
        if database is None:
            from core.db_manager import get_db

            database = get_db()
        rows = list(database.iter_mod_backup_key_rows())
    except Exception:  # noqa: BLE001
        logger.exception("presence reconcile failed to load entities")
        stats.duration_ms = (time.perf_counter() - t0) * 1000.0
        _LAST_STATS = stats
        return stats

    folder_by_app: dict[int, str] = {}
    try:
        from core.sanitize import sanitize_folder_name

        for game in database.list_games():
            app_id = int(getattr(game, "app_id", 0) or 0)
            if app_id <= 0:
                continue
            name = str(getattr(game, "name", "") or "").strip()
            folder = str(getattr(game, "folder_name", "") or "").strip()
            if not folder:
                folder = sanitize_folder_name(name, fallback=f"App_{app_id}")
            folder_by_app[app_id] = folder
    except Exception:  # noqa: BLE001
        folder_by_app = {}

    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        game_root = _game_root_for_row(
            row, library_root=root, folder_by_app=folder_by_app
        )
        if game_root is None:
            continue
        if filter_folder and game_root.name != filter_folder:
            continue
        grouped.setdefault(str(game_root), []).append(row)

    index = RediscoveryIndex()
    try:
        from services import mod_presence as presence_mod

        presence_mod._LAST_REDISCOVERY = index
    except Exception:  # noqa: BLE001
        pass
    miss_ids: list[str] = []
    live_ids: list[str] = []
    rediscovered_ids: list[str] = []
    stale_live_ids: list[str] = []
    changed: list[str] = []

    from services.library_status import CONTENT_CONTENT_MISSING, normalize_content_axis

    for root_key, members in grouped.items():
        game_root = Path(root_key)
        stats.game_roots_examined += 1
        children, live_paths, n_entries = _scan_game_root(game_root)
        stats.directory_scans += 1
        stats.filesystem_stat_calls += n_entries + len(children)

        claimed: set[str] = set()
        missing_rows: list[dict[str, str]] = []
        present_by_id: dict[str, bool] = {}
        for row in members:
            stats.mods_examined += 1
            mid = str(row.get("mod_id") or "").strip()
            if not mid.isdigit():
                continue
            was_present = str(row.get("folder_present") or "1").strip() != "0"
            present_by_id[mid] = was_present
            initial_cs = normalize_content_axis(str(row.get("content_status") or ""))
            lkp = str(row.get("last_known_path") or "").strip()
            lkp_key = _norm_path(Path(lkp)) if lkp else ""
            if lkp_key and lkp_key in live_paths:
                claimed.add(lkp_key)
                stats.live += 1
                if was_present:
                    stats.unchanged += 1
                    if initial_cs == CONTENT_CONTENT_MISSING:
                        stale_live_ids.append(mid)
                else:
                    live_ids.append(mid)
                    changed.append(mid)
                continue
            missing_rows.append(row)

        iid_map, info_reads = _index_unclaimed(children, claimed)
        stats.info_reads += info_reads
        index.prime_root(
            game_root,
            live_paths=live_paths,
            iid_map=iid_map,
            directory_scans=0,
            iterdir_entries=n_entries,
            info_reads=info_reads,
        )

        for row in missing_rows:
            mid = str(row.get("mod_id") or "").strip()
            was_present = present_by_id.get(mid, True)
            stats.rediscovery_attempted += 1
            found = rediscover_entity_path(
                mid, db=database, library_root=root, index=index, row=row
            )
            if found.success and found.path:
                stats.rediscovered += 1
                stats.live += 1
                rediscovered_ids.append(mid)
                changed.append(mid)
                continue
            stats.miss += 1
            if was_present:
                miss_ids.append(mid)
                changed.append(mid)
            else:
                stats.unchanged += 1

    stats.roots = list(index.roots_scanned)
    if miss_ids:
        try:
            database.set_mods_folder_present(miss_ids, present=False)
        except Exception:  # noqa: BLE001
            logger.exception("batch folder_present=0 failed")
            for mid in miss_ids:
                try:
                    database.set_mod_folder_present(mid, present=False)
                except Exception:  # noqa: BLE001
                    pass
    if live_ids:
        try:
            database.set_mods_folder_present(live_ids, present=True)
        except Exception:  # noqa: BLE001
            for mid in live_ids:
                try:
                    database.set_mod_folder_present(mid, present=True)
                except Exception:  # noqa: BLE001
                    pass

    _reevaluate_content_after_presence(
        database,
        live_ids=live_ids,
        rediscovered_ids=rediscovered_ids,
        stale_live_ids=stale_live_ids,
        miss_ids=miss_ids,
        stats=stats,
        changed=changed,
    )

    stats.changed_ids = list(dict.fromkeys(changed))
    stats.duration_ms = (time.perf_counter() - t0) * 1000.0
    stats.ui_blocking_ms = float(_LAST_SCHEDULE_UI_MS)
    _LAST_STATS = stats
    logger.info(
        "presence reconcile examined=%s roots=%s scans=%s miss=%s "
        "rediscovered=%s live=%s unchanged=%s content_reeval=%s "
        "content_cleared=%s duration_ms=%.1f",
        stats.mods_examined,
        stats.game_roots_examined,
        stats.directory_scans,
        stats.miss,
        stats.rediscovered,
        stats.live,
        stats.unchanged,
        stats.content_reevaluated,
        stats.content_status_cleared,
        stats.duration_ms,
    )
    if notify and stats.changed_ids:
        try:
            from services.mod_projection_events import notify_mods_changed

            notify_mods_changed(stats.changed_ids)
        except Exception:  # noqa: BLE001
            logger.debug("presence projection notify failed", exc_info=True)
    return stats


def schedule_presence_reconcile(
    library_root: str | Path | None = None,
    *,
    game_folder: str | None = None,
    db: Any | None = None,
) -> bool:
    """Daemon-thread Presence Reconcile. Safe to call from the UI thread."""
    global _worker, _pending
    t0 = time.perf_counter()
    root = str(library_root or "")
    folder = str(game_folder or "")

    def _run() -> None:
        global _worker, _pending
        try:
            reconcile_presence(root or None, game_folder=folder or None, db=db)
        except Exception:  # noqa: BLE001
            logger.exception("scheduled presence reconcile failed")
        nxt: tuple[str, str] | None = None
        with _LOCK:
            nxt = _pending
            _pending = None
            _worker = None
        if nxt is not None:
            schedule_presence_reconcile(nxt[0] or None, game_folder=nxt[1] or None, db=db)

    thread = threading.Thread(
        target=_run, name="presence-reconcile", daemon=True
    )
    started = False
    with _LOCK:
        prev = _worker
        if prev is not None and prev.is_alive():
            _pending = (root, folder)
            logger.debug("presence reconcile queued behind in-flight pass")
        else:
            _worker = thread
            started = True
    if started:
        thread.start()
    record_schedule_ui_ms((time.perf_counter() - t0) * 1000.0)
    return started


def drain_presence_reconcile(*, timeout: float = 30.0) -> None:
    """Test helper: wait for the scheduled worker."""
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        with _LOCK:
            thread = _worker
        if thread is None or not thread.is_alive():
            return
        time.sleep(0.05)
