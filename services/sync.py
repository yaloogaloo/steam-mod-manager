"""End-to-end sync: scan → metadata → copy/rename → cover → offline page."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core.models import ModMetadata
from core.scanner import WorkshopScanner
from core.steam_api import SteamWorkshopClient

from .archive import OfflinePageArchiver
from .file_ops import COVER_BASENAME, ModFileManager

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int, str], None]
# signature: (phase, current, total, message)


@dataclass
class SyncResult:
    """Outcome of a full or partial library sync."""

    success: list[ModMetadata] = field(default_factory=list)
    skipped: list[ModMetadata] = field(default_factory=list)
    failed: list[tuple[ModMetadata | None, str]] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        return len(self.success) + len(self.skipped) + len(self.failed)


@dataclass
class SyncOptions:
    skip_existing: bool = True
    archive_pages: bool = True
    download_covers: bool = True
    overwrite_files: bool = False
    recursive_scan: bool = True


class ModSyncService:
    """
    Orchestrate backing up Steam Workshop mods into a managed library.

    Pipeline per Mod:
      1. Scan source for numeric ID folders
      2. Fetch title / preview via Steam API (HTML fallback)
      3. Copy folder → target, renamed to sanitized real name
      4. Write ``info/mod.json``
      5. Download cover into ``info/preview.*``
      6. Archive Workshop page into ``info/index.html``
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
        self.archiver = archiver or OfflinePageArchiver(session=self.client._session)

    def close(self) -> None:
        if self._owns_archiver:
            self.archiver.close()
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> ModSyncService:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def sync(
        self,
        options: SyncOptions | None = None,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> SyncResult:
        opts = options or SyncOptions()
        result = SyncResult()

        def progress(phase: str, current: int, total: int, message: str) -> None:
            if on_progress:
                on_progress(phase, current, total, message)

        # --- Phase 1: scan ---
        progress("scan", 0, 1, "Scanning workshop directory…")
        scanner = WorkshopScanner(self.workshop_dir)
        scanned = scanner.scan(recursive=opts.recursive_scan)
        progress("scan", 1, 1, f"Found {len(scanned)} mod folder(s)")

        if not scanned:
            return result

        # --- Phase 2: metadata ---
        ids = [m.published_file_id for m in scanned]
        path_map = {m.published_file_id: m.path for m in scanned}

        def meta_progress(done: int, total: int) -> None:
            progress("metadata", done, total, f"Fetching metadata {done}/{total}")

        metas = self.client.get_details_batch(ids, on_progress=meta_progress)
        for meta in metas:
            meta.source_path = str(path_map.get(meta.published_file_id, ""))

        # --- Phase 3–6: per-mod file ops ---
        total = len(metas)
        for index, meta in enumerate(metas, start=1):
            label = meta.display_name
            progress("sync", index - 1, total, f"Processing {label}")

            try:
                outcome = self._sync_one(meta, opts)
                if outcome == "skipped":
                    result.skipped.append(meta)
                else:
                    result.success.append(meta)
            except Exception as exc:  # noqa: BLE001 — collect & continue
                logger.exception("Failed syncing mod %s", meta.published_file_id)
                result.failed.append((meta, str(exc)))

            progress("sync", index, total, f"Finished {label}")

        progress("done", total, total, "Sync complete")
        return result

    def _sync_one(self, meta: ModMetadata, opts: SyncOptions) -> str:
        existing = self.files.find_by_published_id(meta.published_file_id)

        if existing and opts.skip_existing:
            meta.managed_path = str(existing)
            # Refresh metadata / cover / page if missing
            self.files.save_metadata(meta, existing)
            self._ensure_cover(meta, existing, opts)
            self._ensure_archive(meta, existing, opts)
            return "skipped"

        dest = existing if (existing and opts.overwrite_files) else None
        managed = self.files.copy_mod(
            meta,
            overwrite_existing=bool(existing and opts.overwrite_files),
            destination=dest,
        )

        self._ensure_cover(meta, managed, opts)
        self._ensure_archive(meta, managed, opts)
        self.files.save_metadata(meta, managed)
        return "success"

    def _ensure_cover(
        self,
        meta: ModMetadata,
        managed: Path,
        opts: SyncOptions,
    ) -> None:
        local = self.files.find_local_cover(managed)
        if local:
            meta.cover_path = str(local)
            if not opts.download_covers:
                return

        if not opts.download_covers or not meta.preview_url:
            return

        info = self.files.ensure_info_dir(managed)
        # Skip download when a preview already sits in info/
        if local and local.parent == info and not opts.overwrite_files:
            meta.cover_path = str(local)
            return

        saved = self.client.fetch_and_save_cover(
            meta,
            info,
            filename=COVER_BASENAME,
        )
        if saved:
            meta.cover_path = str(saved)

    def _ensure_archive(
        self,
        meta: ModMetadata,
        managed: Path,
        opts: SyncOptions,
    ) -> None:
        if not opts.archive_pages:
            return

        info = self.files.ensure_info_dir(managed)
        index = info / "index.html"
        if index.is_file() and not opts.overwrite_files:
            meta.offline_page_path = str(index)
            return

        try:
            path = self.archiver.archive(
                meta.published_file_id,
                info,
                overwrite=True,
            )
            meta.offline_page_path = str(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Offline archive failed for %s: %s",
                meta.published_file_id,
                exc,
            )
