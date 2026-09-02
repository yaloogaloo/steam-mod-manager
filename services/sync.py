"""End-to-end sync: scan → metadata → concurrent copy / cover / archive."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from core.models import ModMetadata
from core.scanner import WorkshopScanner
from core.steam_api import SteamWorkshopClient

from . import archive as archive_mod
from .archive import (
    RATE_LIMIT_USER_MESSAGE,
    OfflinePageArchiver,
    SteamArchiveSyncContext,
    _RATE_LIMITED_REASON,
    is_archive_globally_blocked,
    is_stub_offline_page,
    read_archive_status,
)
from .file_ops import COVER_BASENAME, ModFileManager
from .source_timestamp import (
    SourceTimestampDecision,
    compare_source_timestamps,
    normalize_source_timestamp,
)

logger = logging.getLogger(__name__)

# Mod-level offline archive queue (HTML still paced by SteamArchiveLimiter).
OFFLINE_ARCHIVE_MOD_WORKERS = 3

# (phase, current, total, message, **optional kwargs)
# Optional kwargs (backward compatible): current_mod_name, phase_detail,
# in_progress, queued_count, running_count, completed_count
ProgressCallback = Callable[..., None]


@dataclass(frozen=True)
class SyncProgressEvent:
    """Structured sync progress (offline queue may set mod/detail fields)."""

    phase: str
    current: int
    total: int
    message: str
    current_mod_name: str = ""
    phase_detail: str = ""
    in_progress: bool = False
    queued_count: int = 0
    running_count: int = 0
    completed_count: int = 0


def emit_progress(
    on_progress: ProgressCallback | None,
    phase: str,
    current: int,
    total: int,
    message: str,
    *,
    current_mod_name: str = "",
    phase_detail: str = "",
    in_progress: bool = False,
    queued_count: int | None = None,
    running_count: int | None = None,
    completed_count: int | None = None,
) -> None:
    """
    Invoke a progress callback.

    Prefer the extended keyword form; fall back to the legacy 4-arg call so
    older listeners keep working.
    """
    if on_progress is None:
        return
    kwargs: dict[str, object] = {
        "current_mod_name": current_mod_name,
        "phase_detail": phase_detail,
        "in_progress": in_progress,
    }
    if queued_count is not None:
        kwargs["queued_count"] = queued_count
    if running_count is not None:
        kwargs["running_count"] = running_count
    if completed_count is not None:
        kwargs["completed_count"] = completed_count
    try:
        on_progress(phase, current, total, message, **kwargs)
    except TypeError:
        on_progress(phase, current, total, message)


@dataclass
class SyncResult:
    """Outcome of a full or partial library sync."""

    success: list[ModMetadata] = field(default_factory=list)
    updated: list[ModMetadata] = field(default_factory=list)
    skipped: list[ModMetadata] = field(default_factory=list)
    baselined: list[ModMetadata] = field(default_factory=list)
    failed: list[tuple[ModMetadata | None, str]] = field(default_factory=list)
    rate_limited: list[tuple[ModMetadata | None, str]] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        return (
            len(self.success)
            + len(self.skipped)
            + len(self.failed)
            + len(self.rate_limited)
        )


@dataclass
class SyncOptions:
    skip_existing: bool = True
    # Full sync never archives by default — offline pages use a dedicated queue.
    archive_pages: bool = False
    download_covers: bool = True
    overwrite_files: bool = False
    recursive_scan: bool = True
    # Concurrency knobs
    io_workers: int | None = None  # default: 2..4 based on CPU
    net_workers: int | None = None  # default: 12
    # Optional HTTP(S) proxy, e.g. "http://127.0.0.1:7890"
    proxy_url: str = ""
    # Optional Steam browser Cookie string (steamLoginSecure / sessionid / …)
    steam_cookie: str = ""

    def proxies_dict(self) -> dict[str, str] | None:
        """Convert ``proxy_url`` into a ``requests``-compatible proxies dict."""
        url = (self.proxy_url or "").strip()
        if not url:
            return None
        return {"http": url, "https": url}


class ModSyncService:
    """
    Orchestrate backing up Steam Workshop mods into a managed library.

    Pipeline:
      1. Scan source for numeric ID folders
      2. Early-exit: skip mods already in the library (no network / copy)
      3. Batch-fetch metadata only for mods that still need work
      4. Resolve unique game names (parallel, cache-first)
      5. Concurrent copy (small IO pool) + cover/archive (larger net pool)
    """

    def __init__(
        self,
        workshop_dir: str | Path,
        target_dir: str | Path,
        *,
        client: SteamWorkshopClient | None = None,
        file_manager: ModFileManager | None = None,
        archiver: OfflinePageArchiver | None = None,
    ) -> None:
        self.workshop_dir = Path(workshop_dir)
        self.target_dir = Path(target_dir)
        self._owns_client = client is None
        self.client = client or SteamWorkshopClient()
        self.files = file_manager or ModFileManager(self.target_dir)
        self._owns_archiver = archiver is None
        self.archiver = archiver or OfflinePageArchiver()
        self._archive_ctx: SteamArchiveSyncContext | None = None
        # Protect destination path allocation across IO workers
        self._alloc_lock = threading.Lock()
        # Mod IDs that need copytree overwrite due to Steam source update detection.
        self._force_overwrite_ids: set[str] = set()

    def close(self) -> None:
        self._end_archive_batch()
        if self._owns_archiver and self.archiver is not None:
            self.archiver.close()
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> ModSyncService:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _begin_archive_batch(self, opts: SyncOptions) -> None:
        """One shared curl_cffi Session for all archive work in this sync run."""
        proxies = opts.proxies_dict()
        timeout = self.client.timeout
        cookie = (opts.steam_cookie or "").strip() or None
        if self._owns_archiver:
            self._end_archive_batch()
            self._archive_ctx = SteamArchiveSyncContext(
                proxies=proxies,
                timeout=timeout,
                steam_cookie=cookie,
            )
            self.archiver = self._archive_ctx.archiver
        else:
            self.archiver.configure(
                proxies=proxies,
                timeout=timeout,
                steam_cookie=cookie if cookie is not None else None,
            )

    def _end_archive_batch(self) -> None:
        if self._archive_ctx is not None:
            self._archive_ctx.close()
            self._archive_ctx = None
            if self._owns_archiver:
                # Fresh idle archiver for the next sync(); session closed above.
                self.archiver = OfflinePageArchiver()

    def sync(
        self,
        options: SyncOptions | None = None,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> SyncResult:
        opts = options or SyncOptions()
        result = SyncResult()
        self.files.ensure_target_root()
        self._begin_archive_batch(opts)

        progress_lock = threading.Lock()

        def progress(phase: str, current: int, total: int, message: str) -> None:
            emit_progress(on_progress, phase, current, total, message)

        try:
            return self._sync_body(opts, result, progress, progress_lock)
        finally:
            self._end_archive_batch()

    def _sync_body(
        self,
        opts: SyncOptions,
        result: SyncResult,
        progress: ProgressCallback,
        progress_lock: threading.Lock,
    ) -> SyncResult:
        # --- Phase 0: fix legacy numeric Mod folder names ---
        progress("scan", 0, 1, "Migrating numeric mod folders to real titles…")
        try:
            renames = self.files.migrate_numeric_mod_folders()
            if renames:
                progress(
                    "scan",
                    0,
                    1,
                    f"Renamed {len(renames)} numeric folder(s) to real titles",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Numeric folder migration failed: %s", exc)

        # --- Phase 1: scan ---
        progress("scan", 0, 1, "Scanning workshop directory…")
        scanner = WorkshopScanner(self.workshop_dir)
        scanned = scanner.scan(recursive=opts.recursive_scan)
        progress("scan", 1, 1, f"Found {len(scanned)} mod folder(s)")

        if not scanned:
            return result

        # --- Phase 1b: index library + timestamp-aware delta (minimal network) ---
        progress("scan", 1, 1, "Indexing local library for delta sync…")
        existing_index = self._build_existing_index()
        self._force_overwrite_ids = set()

        to_skip: list[tuple[str, Path]] = []
        to_fetch: list = []
        skip_candidates: list[tuple] = []
        baselined_ids: set[str] = set()

        for item in scanned:
            existing = existing_index.get(item.published_file_id)
            if opts.overwrite_files:
                to_fetch.append(item)
                continue
            if not existing or not self._is_fully_synced_mod(existing):
                to_fetch.append(item)
                continue
            skip_candidates.append((item, existing))

        source_ts_by_id = self._fetch_source_timestamps_for_candidates(
            skip_candidates,
            progress,
        )

        for item, existing in skip_candidates:
            pub_id = item.published_file_id
            local_meta = self.files.load_metadata(existing)
            local_ts = (
                normalize_source_timestamp(local_meta.time_updated)
                if local_meta
                else None
            )
            source_ts = source_ts_by_id.get(pub_id)
            if pub_id not in source_ts_by_id:
                # API returned no row; use scanner stat (one stat/mod at scan time).
                # Never used when API explicitly returned zero/missing time_updated.
                source_ts = normalize_source_timestamp(item.source_dir_mtime)

            decision = compare_source_timestamps(
                source_ts,
                local_ts,
                local_sync_complete=True,
            )
            logger.debug(
                "[SYNC] mod_id=%s source_timestamp=%s local_timestamp=%s decision=%s",
                pub_id,
                source_ts,
                local_ts,
                decision.value,
            )

            if decision == SourceTimestampDecision.UPDATED:
                to_fetch.append(item)
                self._force_overwrite_ids.add(pub_id)
                continue

            if decision == SourceTimestampDecision.BASELINE:
                if source_ts and self._apply_source_timestamp_baseline(
                    existing, pub_id, source_ts
                ):
                    stub = self._stub_metadata_for_skip(pub_id, existing, item, scanned)
                    loaded = self.files.load_metadata(existing)
                    if loaded:
                        stub.title = loaded.title
                        stub.game_name = loaded.game_name
                        stub.app_id = loaded.app_id
                        stub.time_updated = source_ts
                    result.baselined.append(stub)
                    baselined_ids.add(pub_id)
                to_skip.append((pub_id, existing))
                continue

            to_skip.append((pub_id, existing))

        for pub_id, managed in to_skip:
            if pub_id in baselined_ids:
                continue
            item = next(
                (s for s in scanned if s.published_file_id == pub_id),
                None,
            )
            stub = self._stub_metadata_for_skip(
                pub_id,
                managed,
                item,
                scanned,
            )
            loaded = self.files.load_metadata(managed)
            if loaded:
                stub.title = loaded.title
                stub.game_name = loaded.game_name
                stub.app_id = loaded.app_id
                stub.cover_path = loaded.cover_path
                stub.offline_page_path = loaded.offline_page_path
                stub.time_updated = loaded.time_updated
            self.files.enrich_title_from_db(stub)
            result.skipped.append(stub)

        updated_count = len(self._force_overwrite_ids)
        if to_skip or to_fetch:
            progress(
                "metadata",
                0,
                max(len(to_fetch), 1),
                f"Delta sync: {len(to_skip)} up to date, "
                f"{len(to_fetch)} need work"
                + (f" ({updated_count} Steam-updated)" if updated_count else "")
                + f" ({len(result.baselined)} baseline)",
            )

        if not to_fetch:
            total = len(to_skip)
            progress("done", total, max(total, 1), "Sync complete (nothing new)")
            return result

        # --- Phase 2: batch metadata ONLY for mods that need work ---
        ids = [m.published_file_id for m in to_fetch]
        path_map = {m.published_file_id: m.path for m in to_fetch}

        def meta_progress(done: int, total: int) -> None:
            progress("metadata", done, total, f"Batch fetching metadata {done}/{total}")

        metas = self.client.get_details_batch(ids, on_progress=meta_progress)
        for meta in metas:
            meta.source_path = str(path_map.get(meta.published_file_id, ""))
            self.files.enrich_title_from_db(meta)
            # Guarantee a non-numeric title before path allocation
            if not (meta.title or "").strip() or meta.title.strip().isdigit():
                meta.title = f"Unknown_Mod_{meta.published_file_id}"

        def game_progress(done: int, total: int) -> None:
            progress(
                "metadata",
                done,
                total,
                f"Resolving game names {done}/{total}",
            )

        self.client.resolve_game_names(metas, on_progress=game_progress)

        # --- Phase 3: concurrent per-mod work ---
        total_work = len(metas)
        # Two real milestones per mod: copy done + enrich/archive done.
        total_units = max(total_work * 2, 1)
        units_done = 0
        copies_done = 0
        archives_done = 0

        io_workers = opts.io_workers or max(2, min(4, (os.cpu_count() or 4)))
        net_workers = opts.net_workers or 12

        def report(message: str, *, increment: int = 0) -> None:
            nonlocal units_done
            with progress_lock:
                units_done = min(total_units, units_done + increment)
                current = units_done
            progress("sync", current, total_units, message)

        # Leave the metadata endpoint (25%) immediately — do not wait on archive.
        if opts.archive_pages:
            report("开始同步文件与 Steam 离线网页...", increment=0)
        else:
            report("开始同步 Mod 文件、元数据与封面...", increment=0)
        report("正在复制 Mod 文件...", increment=0)

        # IO pool: copytree; Net pool: cover + offline archive
        with ThreadPoolExecutor(max_workers=io_workers) as io_pool, ThreadPoolExecutor(
            max_workers=net_workers
        ) as net_pool:
            copy_futures = {
                io_pool.submit(self._copy_only, meta, opts, existing_index): meta
                for meta in metas
            }

            net_futures = {}
            for fut in as_completed(copy_futures):
                meta = copy_futures[fut]
                label = f"{meta.game_display_name} / {meta.display_name}"
                try:
                    outcome_hint, managed = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Copy failed for %s", meta.published_file_id)
                    with progress_lock:
                        result.failed.append((meta, str(exc)))
                        copies_done += 1
                    report(f"复制失败: {meta.display_name}", increment=2)
                    continue

                with progress_lock:
                    copies_done += 1
                    c_done = copies_done
                report(
                    f"正在复制 Mod 文件... ({c_done}/{total_work})",
                    increment=1,
                )

                if outcome_hint == "skipped_complete":
                    with progress_lock:
                        result.skipped.append(meta)
                        archives_done += 1
                    report(f"已跳过: {meta.display_name}", increment=1)
                    continue

                def _make_status_cb(
                    m: ModMetadata = meta,
                ) -> Callable[[str], None]:
                    def _status(kind: str) -> None:
                        nonlocal archives_done
                        name = m.display_name
                        if kind == "start":
                            with progress_lock:
                                a_done = archives_done
                            report(
                                f"正在下载 Steam 离线网页 ({a_done}/{total_work}) — "
                                f"正在下载离线页面: {name}",
                                increment=0,
                            )
                        elif kind == "ok":
                            report(f"离线页面完成: {name}", increment=0)
                        elif kind == "fail":
                            report(f"离线页面失败: {name}", increment=0)
                        elif kind == "skip_page":
                            report(f"离线页面已存在: {name}", increment=0)

                    return _status

                nf = net_pool.submit(
                    self._network_enrich_and_save,
                    meta,
                    managed,
                    opts,
                    _make_status_cb(),
                )
                net_futures[nf] = (meta, outcome_hint)

            for nf in as_completed(net_futures):
                meta, outcome_hint = net_futures[nf]
                try:
                    nf.result()
                    with progress_lock:
                        if outcome_hint == "skipped_incomplete":
                            result.skipped.append(meta)
                        elif meta.published_file_id in self._force_overwrite_ids:
                            result.updated.append(meta)
                        else:
                            result.success.append(meta)
                        archives_done += 1
                        a_done = archives_done
                    if opts.archive_pages:
                        report(
                            f"正在下载 Steam 离线网页 ({a_done}/{total_work}) — "
                            f"已处理: {meta.display_name}",
                            increment=1,
                        )
                    else:
                        report(
                            f"正在处理封面/元数据 ({a_done}/{total_work}) — "
                            f"已处理: {meta.display_name}",
                            increment=1,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "Network enrich failed for %s", meta.published_file_id
                    )
                    with progress_lock:
                        result.failed.append((meta, str(exc)))
                        archives_done += 1
                    report(f"网络增强失败: {meta.display_name}", increment=1)

        grand_total = len(result.success) + len(result.skipped) + len(result.failed)
        progress("done", grand_total, max(grand_total, 1), "Sync complete")
        return result

    # ------------------------------------------------------------------
    # Early-exit helpers
    # ------------------------------------------------------------------

    def _build_existing_index(self) -> dict[str, Path]:
        """Map published_file_id → managed folder (one library walk)."""
        index: dict[str, Path] = {}
        for folder in self.files.list_managed_mods():
            meta = self.files.load_metadata(folder)
            if meta and meta.published_file_id:
                index[meta.published_file_id] = folder
            elif folder.name.isdigit():
                index[folder.name] = folder
        return index

    @staticmethod
    def _stub_metadata_for_skip(
        pub_id: str,
        managed: Path,
        item,
        scanned,
    ) -> ModMetadata:
        stub = ModMetadata(
            published_file_id=pub_id,
            managed_path=str(managed),
            source_path=str(
                item.path
                if item is not None
                else next(
                    (s.path for s in scanned if s.published_file_id == pub_id),
                    managed,
                )
            ),
        )
        return stub

    def _fetch_source_timestamps_for_candidates(
        self,
        skip_candidates: list[tuple],
        progress: ProgressCallback,
    ) -> dict[str, int | None]:
        """Network fetch of Steam ``time_updated`` for mods that would otherwise skip."""
        if not skip_candidates:
            return {}
        ids = [item.published_file_id for item, _ in skip_candidates]
        progress(
            "metadata",
            0,
            max(len(ids), 1),
            f"Checking Steam update timestamps for {len(ids)} mod(s)…",
        )

        def _on_progress(done: int, total: int) -> None:
            progress(
                "metadata",
                done,
                total,
                f"Fetching Steam timestamps {done}/{total}",
            )

        refreshed = self.client.refresh_details(ids, on_progress=_on_progress)
        out: dict[str, int | None] = {}
        for meta in refreshed:
            ts = normalize_source_timestamp(meta.time_updated)
            # Explicit API response with missing/zero time_updated → no mtime fallback.
            out[meta.published_file_id] = ts
        return out

    def _apply_source_timestamp_baseline(
        self,
        managed: Path,
        pub_id: str,
        source_ts: int,
    ) -> bool:
        """
        Establish ``time_updated`` baseline without re-copying mod content.

        Contract: local ``time_updated=0`` here means “no sync baseline yet”, not
        “must re-download”. Only metadata is updated; mod files are untouched.

        Returns True when metadata was updated.
        """
        meta = self.files.load_metadata(managed)
        if meta is None:
            meta = ModMetadata(published_file_id=pub_id, managed_path=str(managed))
        old = normalize_source_timestamp(meta.time_updated)
        source = normalize_source_timestamp(source_ts)
        if source is None:
            return False
        if old is not None and source < old:
            logger.warning(
                "[SYNC] mod_id=%s source_timestamp_regression old=%s new=%s",
                pub_id,
                old,
                source,
            )
            return False
        if old == source:
            return False
        meta.time_updated = source
        self.files.save_metadata(meta, managed, sync_reason="source_timestamp_baseline")
        logger.info(
            "[SYNC] mod_id=%s decision=BASELINE timestamp updated old=%s new=%s",
            pub_id,
            old,
            source,
        )
        return True

    def _is_fully_synced_mod(self, managed: Path) -> bool:
        """
        True when the managed folder needs no further *file* sync work:

        - folder name is not a bare numeric ID (Detection A already done)
        - ``mod.json`` exists

        Offline Steam HTML is a separate queue and is not required here.
        """
        if not managed.is_dir():
            return False
        if managed.name.isdigit():
            return False
        return self.files.metadata_path(managed).is_file()

    def _has_valid_offline_page(self, managed: Path) -> bool:
        """
        True when a real (non-stub, non-error) offline page exists.

        Requires ``index.html`` present, non-empty, and a valid Steam Workshop
        mirror (not a failure stub / Steam error / login page).
        """
        from services.archive import is_valid_steam_workshop_page

        candidates = (
            self.files.info_dir_for_write(managed) / "index.html",
            self.files.info_dir(managed) / "index.html",
        )
        seen: set[Path] = set()
        for path in candidates:
            try:
                key = path.resolve()
            except OSError:
                key = path
            if key in seen:
                continue
            seen.add(key)
            try:
                if is_valid_steam_workshop_page(path):
                    return True
            except OSError:
                continue
        return False

    # ------------------------------------------------------------------
    # Worker stages
    # ------------------------------------------------------------------

    def _copy_only(
        self,
        meta: ModMetadata,
        opts: SyncOptions,
        existing_index: dict[str, Path],
    ) -> tuple[str, Path]:
        """
        Returns ``(outcome_hint, managed_path)``.

        Detection A: rename numeric folders to real titles.
        Detection B: skip ``copytree`` when the managed folder already exists
        (unless ``overwrite_files``).
        Network stage later handles Detection C (offline page).

        outcome_hint:
          - skipped_incomplete: reuse folder, still may need net fill
          - success: freshly copied (or overwritten)
        """
        existing = existing_index.get(meta.published_file_id)
        force_overwrite = (
            opts.overwrite_files
            or meta.published_file_id in self._force_overwrite_ids
        )

        with self._alloc_lock:
            # Detection A — numeric ID folder → real Mod title
            if existing and existing.exists():
                existing = self._rename_numeric_if_needed(existing, meta)
                existing_index[meta.published_file_id] = existing

            # Force overwrite: re-copy into (possibly renamed) destination
            if existing and force_overwrite:
                managed = self.files.copy_mod(
                    meta,
                    overwrite_existing=True,
                    destination=existing,
                )
                managed = self._rename_numeric_if_needed(Path(managed), meta)
                existing_index[meta.published_file_id] = managed
                return "success", managed

            # Detection B — folder exists: skip large copytree
            if existing and existing.exists() and opts.skip_existing:
                meta.managed_path = str(existing)
                return "skipped_incomplete", existing

            managed = self.files.copy_mod(meta, overwrite_existing=False)
            managed = self._rename_numeric_if_needed(Path(managed), meta)
            existing_index[meta.published_file_id] = managed
            return "success", managed

    def _rename_numeric_if_needed(self, folder: Path, meta: ModMetadata) -> Path:
        """If *folder* is named with digits only, rename it to the Mod title."""
        if not folder.name.isdigit():
            return folder
        self.files.enrich_title_from_db(meta)
        if not (meta.title or "").strip() or meta.title.strip().isdigit():
            return folder

        desired = self.files.mod_folder_name(meta)
        if desired == folder.name:
            return folder

        from core.sanitize import unique_destination

        target = unique_destination(
            folder.parent,
            desired,
            published_file_id=meta.published_file_id,
        )
        if target.resolve() == folder.resolve():
            return folder
        try:
            folder.rename(target)
            meta.managed_path = str(target)
            self.files.save_metadata(meta, target)
            logger.info(
                "Renamed mod folder %s -> %s", folder.name, target.name
            )
            return target
        except OSError as exc:
            logger.warning("Rename failed %s -> %s: %s", folder, target, exc)
            return folder

    def _network_enrich_and_save(
        self,
        meta: ModMetadata,
        managed: Path,
        opts: SyncOptions,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        # time_updated (and other metadata) is committed only after copy + enrich
        # succeed; a failed sync must leave the prior baseline for retry detection.
        self._network_enrich(meta, managed, opts, on_status=on_status)
        self.files.save_metadata(meta, managed)

    def _network_enrich(
        self,
        meta: ModMetadata,
        managed: Path,
        opts: SyncOptions,
        *,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        """Cover download + offline archive (shared Session for archive batch)."""
        need_cover = opts.download_covers and bool(meta.preview_url)
        need_archive = bool(opts.archive_pages)

        local_cover = self.files.find_local_cover(managed)
        if local_cover:
            meta.cover_path = str(local_cover)
            info = self.files.info_dir(managed)
            try:
                if (
                    local_cover.parent.resolve() == info.resolve()
                    and not opts.overwrite_files
                ):
                    need_cover = False
            except OSError:
                pass

        # Detection C — missing / empty index.html must be (re)fetched
        if not opts.overwrite_files and self._has_valid_offline_page(managed):
            write_index = self.files.info_dir_for_write(managed) / "index.html"
            read_index = self.files.info_dir(managed) / "index.html"
            chosen = read_index
            try:
                if write_index.is_file() and write_index.stat().st_size > 0:
                    chosen = write_index
            except OSError:
                pass
            meta.offline_page_path = str(chosen)
            need_archive = False
            if on_status is not None:
                on_status("skip_page")
        elif opts.overwrite_files:
            need_archive = bool(opts.archive_pages)

        info_dir = self.files.ensure_info_dir(managed)

        # Cover uses Steam Web API client (not Workshop HTML); keep isolated.
        if need_cover and meta.preview_url:
            with SteamWorkshopClient(
                timeout=self.client.timeout,
                enable_scrape_fallback=False,
                request_interval=0.05,
            ) as client:
                saved = client.fetch_and_save_cover(
                    meta,
                    info_dir,
                    filename=COVER_BASENAME,
                )
                if saved:
                    meta.cover_path = str(saved)

        # Archive is opt-in only (dedicated offline queue). Never run via net_workers
        # concurrency when archive_pages is False.
        if need_archive:
            archived = self.archiver.archive(
                meta.published_file_id,
                info_dir,
                overwrite=True,
                metadata=meta,
                on_status=on_status,
            )
            path = (
                archived.path
                if hasattr(archived, "path")
                else Path(archived)
            )
            meta.offline_page_path = str(path)
            if not self._has_valid_offline_page(managed):
                ensured = self.archiver.ensure_offline_page(
                    info_dir,
                    meta.published_file_id,
                    metadata=meta,
                    on_status=None,
                    force_refresh=False,
                )
                path = (
                    ensured.path
                    if hasattr(ensured, "path")
                    else Path(ensured)
                )
                meta.offline_page_path = str(path)
        elif not meta.offline_page_path:
            write_index = self.files.info_dir_for_write(managed) / "index.html"
            read_index = self.files.info_dir(managed) / "index.html"
            if write_index.is_file() or read_index.is_file():
                meta.offline_page_path = str(
                    write_index if write_index.is_file() else read_index
                )

    def sync_offline_pages_only(
        self,
        *,
        mod_ids: list[str] | None = None,
        options: SyncOptions | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> SyncResult:
        """
        Dedicated Archive queue: Steam offline pages for mods already in library.

        Does **not** copy workshop files, fetch Steam API metadata, resolve
        game names, or download covers.

        Scheduling: Mod-level ``ThreadPoolExecutor`` (``OFFLINE_ARCHIVE_MOD_WORKERS``).
        HTML GETs remain paced by ``SteamArchiveLimiter``; all Mods share
        ``GLOBAL_ASSET_WORKERS`` via ``_GLOBAL_ASSET_SEMAPHORE`` (no per-Mod
        asset pool multiplication).
        """
        opts = options or SyncOptions(
            archive_pages=True,
            download_covers=False,
            skip_existing=True,
            overwrite_files=False,
        )
        opts.archive_pages = True
        result = SyncResult()
        self.files.ensure_target_root()
        self._begin_archive_batch(opts)

        progress_lock = threading.Lock()
        result_lock = threading.Lock()
        state = {
            "running": 0,
            "completed": 0,
            "blocked": is_archive_globally_blocked(),
            "running_names": {},  # key -> label
        }

        def _counts(total: int) -> tuple[int, int, int]:
            running = int(state["running"])
            completed = int(state["completed"])
            queued = max(0, total - completed - running)
            return queued, running, completed

        def progress(
            phase: str,
            current: int,
            total: int,
            message: str,
            *,
            current_mod_name: str = "",
            phase_detail: str = "",
            in_progress: bool = False,
        ) -> None:
            queued, running, completed = _counts(total)
            emit_progress(
                on_progress,
                phase,
                current,
                total,
                message,
                current_mod_name=current_mod_name,
                phase_detail=phase_detail,
                in_progress=in_progress,
                queued_count=queued,
                running_count=running,
                completed_count=completed,
            )

        def _mod_label(meta: ModMetadata, folder: Path) -> str:
            game = (meta.game_display_name or "").strip()
            if not game:
                try:
                    parent = folder.parent.name
                    if parent and parent not in {".", ""}:
                        game = parent
                except Exception:  # noqa: BLE001
                    game = ""
            title = meta.display_name
            return f"[{game}] {title}" if game else title

        def _summary() -> str:
            with result_lock:
                return (
                    f"离线网页同步完成：成功 {len(result.success)}，"
                    f"失败 {len(result.failed)}，"
                    f"429 {len(result.rate_limited)}，"
                    f"跳过 {len(result.skipped)}"
                )

        def _emit_queue(
            total: int,
            *,
            phase_detail: str,
            current_mod_name: str = "",
            in_progress: bool = True,
        ) -> None:
            with progress_lock:
                queued, running, completed = _counts(total)
                names = list(state["running_names"].values())
            if not current_mod_name and names:
                current_mod_name = "\n".join(names[:OFFLINE_ARCHIVE_MOD_WORKERS])
            progress(
                "offline",
                completed,
                max(total, 1),
                "Steam 离线网页同步",
                current_mod_name=current_mod_name,
                phase_detail=phase_detail,
                in_progress=in_progress or running > 0,
            )

        # Allow up to N mods inside archive() so asset phases can overlap.
        # HTML remains single-flight via SteamArchiveLimiter (archive.py).
        prev_sem = archive_mod._ARCHIVE_SEMAPHORE
        archive_mod._ARCHIVE_SEMAPHORE = threading.Semaphore(
            OFFLINE_ARCHIVE_MOD_WORKERS
        )

        try:
            id_filter = {str(x) for x in mod_ids} if mod_ids else None
            work: list[tuple[Path, ModMetadata]] = []
            for folder in self.files.list_managed_mods():
                meta = self.files.load_metadata(folder)
                if meta is None:
                    meta = ModMetadata(
                        published_file_id=(
                            folder.name if folder.name.isdigit() else ""
                        ),
                        title=folder.name,
                        managed_path=str(folder),
                    )
                pub = str(meta.published_file_id or "")
                if id_filter is not None and pub not in id_filter:
                    continue
                if not pub:
                    continue
                work.append((folder, meta))

            total = len(work)
            _emit_queue(
                total,
                phase_detail="正在处理…",
                in_progress=True,
            )
            if not work:
                progress("done", 0, 1, "没有可同步的离线网页")
                return result

            def _process_one(folder: Path, meta: ModMetadata) -> None:
                label = _mod_label(meta, folder)
                key = str(folder)

                with progress_lock:
                    state["running"] += 1
                    state["running_names"][key] = label
                    blocked_now = bool(state["blocked"]) or is_archive_globally_blocked()
                    if blocked_now:
                        state["blocked"] = True
                _emit_queue(
                    total,
                    phase_detail="准备处理…",
                    current_mod_name=label,
                    in_progress=True,
                )

                phase_detail = "准备处理…"
                try:
                    if self._has_valid_offline_page(folder):
                        write_index = (
                            self.files.info_dir_for_write(folder) / "index.html"
                        )
                        read_index = self.files.info_dir(folder) / "index.html"
                        meta.offline_page_path = str(
                            write_index if write_index.is_file() else read_index
                        )
                        with result_lock:
                            result.skipped.append(meta)
                        phase_detail = "跳过（已有离线页）"
                        return

                    with progress_lock:
                        blocked_now = bool(state["blocked"]) or (
                            is_archive_globally_blocked()
                        )
                        if blocked_now:
                            state["blocked"] = True
                    if blocked_now:
                        with result_lock:
                            result.rate_limited.append(
                                (meta, RATE_LIMIT_USER_MESSAGE)
                            )
                        phase_detail = RATE_LIMIT_USER_MESSAGE
                        return

                    info_dir = self.files.ensure_info_dir(folder)

                    def _status(kind: str, _label: str = label) -> None:
                        detail_map = {
                            "start": "正在下载页面资源…",
                            "ok": "离线页面完成",
                            "skipped": "跳过（已有离线页）",
                            "rate_limited": RATE_LIMIT_USER_MESSAGE,
                            "fail": "离线页面失败",
                        }
                        _emit_queue(
                            total,
                            phase_detail=detail_map.get(kind, kind),
                            current_mod_name=_label,
                            in_progress=True,
                        )

                    _emit_queue(
                        total,
                        phase_detail="正在下载页面资源…",
                        current_mod_name=label,
                        in_progress=True,
                    )

                    ensured = self.archiver.ensure_offline_page(
                        info_dir,
                        meta.published_file_id,
                        metadata=meta,
                        on_status=_status,
                        force_refresh=False,
                    )
                    path = (
                        ensured.path
                        if hasattr(ensured, "path")
                        else Path(ensured)
                    )

                    meta.offline_page_path = str(path)
                    try:
                        self.files.save_metadata(meta, folder)
                    except OSError as exc:
                        logger.warning(
                            "save_metadata failed for %s: %s", folder, exc
                        )

                    outcome = getattr(ensured, "outcome", None)
                    status = read_archive_status(info_dir)
                    if outcome == "skipped":
                        with result_lock:
                            result.skipped.append(meta)
                        phase_detail = "跳过（已有离线页）"
                    elif outcome == "rate_limited" or (
                        status
                        and status.get("archive_failed_reason")
                        == _RATE_LIMITED_REASON
                    ):
                        # Only freeze the queue when the global HTML fuse is armed.
                        # A single Mod stub / transient 429 must not mark the rest.
                        if is_archive_globally_blocked():
                            with progress_lock:
                                state["blocked"] = True
                            with result_lock:
                                result.rate_limited.append(
                                    (meta, RATE_LIMIT_USER_MESSAGE)
                                )
                            phase_detail = RATE_LIMIT_USER_MESSAGE
                        else:
                            with result_lock:
                                result.failed.append(
                                    (meta, "HTML 429 after retries (no global block)")
                                )
                            phase_detail = "离线页面失败"
                    elif outcome == "failed":
                        with result_lock:
                            result.failed.append(
                                (
                                    meta,
                                    getattr(ensured, "error", None)
                                    or "archive failed",
                                )
                            )
                        phase_detail = "离线页面失败"
                    elif outcome == "success" or self._has_valid_offline_page(
                        folder
                    ):
                        with result_lock:
                            result.success.append(meta)
                        phase_detail = "离线页面完成"
                    elif is_stub_offline_page(path):
                        with result_lock:
                            result.failed.append(
                                (meta, "archive produced stub / cooldown")
                            )
                        phase_detail = "离线页面失败"
                    else:
                        with result_lock:
                            result.failed.append(
                                (meta, "offline page missing after archive")
                            )
                        phase_detail = "离线页面失败"
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "offline archive worker failed for %s: %s", folder, exc
                    )
                    with result_lock:
                        result.failed.append((meta, str(exc)))
                    phase_detail = "离线页面失败"
                finally:
                    with progress_lock:
                        state["running"] = max(0, int(state["running"]) - 1)
                        state["running_names"].pop(key, None)
                        state["completed"] = int(state["completed"]) + 1
                    _emit_queue(
                        total,
                        phase_detail=phase_detail,
                        current_mod_name=label,
                        in_progress=False,
                    )

            workers = max(1, int(OFFLINE_ARCHIVE_MOD_WORKERS))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_process_one, folder, meta)
                    for folder, meta in work
                ]
                for fut in as_completed(futures):
                    # Surface unexpected worker crashes (already logged inside).
                    exc = fut.exception()
                    if exc is not None:
                        logger.warning("offline archive future error: %s", exc)

            progress("done", total, max(total, 1), _summary())
            return result
        finally:
            archive_mod._ARCHIVE_SEMAPHORE = prev_sem
            self._end_archive_batch()
