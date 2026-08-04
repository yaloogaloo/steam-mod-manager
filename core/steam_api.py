"""Steam Web API client for Workshop published-file details."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import TypeVar

import requests

from .models import ModMetadata
from .scraper import WorkshopPageScraper

logger = logging.getLogger(__name__)

T = TypeVar("T")

STEAM_PUBLISHED_FILE_DETAILS_URL = (
    "https://api.steampowered.com/ISteamRemoteStorage/"
    "GetPublishedFileDetails/v1/"
)

# Steam accepts up to ~100 IDs per GetPublishedFileDetails call
DEFAULT_BATCH_SIZE = 50
DEFAULT_TIMEOUT = 30
DEFAULT_USER_AGENT = (
    "SteamModManager/0.1 (+https://github.com/local/steam-mod-manager; desktop)"
)


class SteamAPIError(Exception):
    """Raised when the Steam Web API returns an unexpected response."""


class SteamWorkshopClient:
    """
    Fetch Workshop item metadata via Steam's public Web API.

    Uses ``ISteamRemoteStorage/GetPublishedFileDetails`` which does **not**
    require an API key for published-file details.
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        batch_size: int = DEFAULT_BATCH_SIZE,
        request_interval: float = 0.35,
        user_agent: str = DEFAULT_USER_AGENT,
        enable_scrape_fallback: bool = True,
    ) -> None:
        self.timeout = timeout
        self.batch_size = max(1, min(batch_size, 100))
        self.request_interval = max(0.0, request_interval)
        self.enable_scrape_fallback = enable_scrape_fallback
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", user_agent)
        self._last_request_at = 0.0
        self._scraper: WorkshopPageScraper | None = None

    @property
    def session(self) -> requests.Session:
        """Shared ``requests`` session (for archivers / scrapers)."""
        return self._session

    def close(self) -> None:
        if self._scraper is not None:
            self._scraper.close()
            self._scraper = None
        self._session.close()

    def _get_scraper(self) -> WorkshopPageScraper:
        if self._scraper is None:
            self._scraper = WorkshopPageScraper(
                session=self._session,
                timeout=self.timeout,
            )
        return self._scraper

    def __enter__(self) -> SteamWorkshopClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_details(self, published_file_id: str | int) -> ModMetadata:
        """Fetch metadata for a single Workshop item."""
        results = self.get_details_batch([str(published_file_id)])
        if not results:
            return ModMetadata(
                published_file_id=str(published_file_id),
                fetch_error="Empty response from Steam API",
            )
        return results[0]

    def get_details_batch(
        self,
        published_file_ids: Sequence[str | int],
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[ModMetadata]:
        """
        Fetch metadata for many IDs, automatically chunked into API batches.

        ``on_progress(done, total)`` is called after each batch completes.
        """
        ids = [str(i) for i in published_file_ids if str(i).strip()]
        if not ids:
            return []

        collected: list[ModMetadata] = []
        total = len(ids)
        done = 0

        for chunk in _chunked(ids, self.batch_size):
            batch = self._request_published_file_details(chunk)
            if self.enable_scrape_fallback:
                batch = [self._maybe_fallback_scrape(item) for item in batch]
            collected.extend(batch)
            done += len(chunk)
            if on_progress:
                on_progress(done, total)

        return collected

    def _maybe_fallback_scrape(self, meta: ModMetadata) -> ModMetadata:
        """If API metadata is incomplete, try scraping the Workshop page."""
        needs_fallback = bool(meta.fetch_error) or not meta.title
        if not needs_fallback:
            return meta

        logger.info(
            "Falling back to page scrape for %s (%s)",
            meta.published_file_id,
            meta.fetch_error or "missing title",
        )
        scraped = self._get_scraper().fetch(meta.published_file_id)
        if scraped.title and not scraped.fetch_error:
            scraped.source_path = meta.source_path
            return scraped
        # Keep the richer error message when scrape also fails
        if scraped.fetch_error and meta.fetch_error:
            scraped.fetch_error = f"{meta.fetch_error}; scrape: {scraped.fetch_error}"
        return scraped if scraped.title else meta

    def download_preview(
        self,
        preview_url: str,
        dest_path: str | Path,
        *,
        overwrite: bool = True,
    ) -> Path | None:
        """
        Download a Workshop preview image to *dest_path*.

        Returns the saved path on success, or ``None`` if *preview_url* is
        empty / the download fails.
        """
        if not preview_url:
            return None

        dest = Path(dest_path)
        if dest.exists() and not overwrite:
            return dest

        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._throttle()
            response = self._session.get(preview_url, timeout=self.timeout, stream=True)
            response.raise_for_status()

            # Infer extension from Content-Type when dest has no suffix
            if not dest.suffix:
                content_type = (response.headers.get("Content-Type") or "").lower()
                ext = _ext_from_content_type(content_type) or ".jpg"
                dest = dest.with_suffix(ext)

            with dest.open("wb") as fh:
                for block in response.iter_content(chunk_size=64 * 1024):
                    if block:
                        fh.write(block)
            return dest
        except requests.RequestException as exc:
            logger.warning("Failed to download preview %s: %s", preview_url, exc)
            if dest.exists():
                try:
                    dest.unlink()
                except OSError:
                    pass
            return None

    def fetch_and_save_cover(
        self,
        metadata: ModMetadata,
        dest_dir: str | Path,
        *,
        filename: str = "preview",
    ) -> Path | None:
        """
        Download the cover image for *metadata* into *dest_dir*.

        Updates ``metadata.cover_path`` when successful.
        """
        if not metadata.preview_url:
            return None

        dest_dir = Path(dest_dir)
        # Prefer keeping original extension when URL has one
        url_path = metadata.preview_url.split("?", 1)[0]
        suffix = Path(url_path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            suffix = ""

        target = dest_dir / f"{filename}{suffix}"
        saved = self.download_preview(metadata.preview_url, target)
        if saved:
            metadata.cover_path = str(saved)
        return saved

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _request_published_file_details(
        self, published_file_ids: Sequence[str]
    ) -> list[ModMetadata]:
        form: dict[str, str | int] = {"itemcount": len(published_file_ids)}
        for index, file_id in enumerate(published_file_ids):
            form[f"publishedfileids[{index}]"] = file_id

        self._throttle()
        try:
            response = self._session.post(
                STEAM_PUBLISHED_FILE_DETAILS_URL,
                data=form,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            logger.error("Steam API request failed: %s", exc)
            return [
                ModMetadata(published_file_id=fid, fetch_error=str(exc))
                for fid in published_file_ids
            ]
        except ValueError as exc:
            raise SteamAPIError(f"Invalid JSON from Steam API: {exc}") from exc

        try:
            details = payload["response"]["publishedfiledetails"]
        except (KeyError, TypeError) as exc:
            raise SteamAPIError(
                f"Unexpected Steam API payload shape: {payload!r}"
            ) from exc

        by_id = {
            str(item.get("publishedfileid", "")): ModMetadata.from_api_response(item)
            for item in details
            if isinstance(item, dict)
        }

        # Preserve input order; fill gaps if API omitted an ID
        ordered: list[ModMetadata] = []
        for file_id in published_file_ids:
            meta = by_id.get(file_id)
            if meta is None:
                meta = ModMetadata(
                    published_file_id=file_id,
                    fetch_error="ID missing from Steam API response",
                )
            ordered.append(meta)
        return ordered

    def _throttle(self) -> None:
        if self.request_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.request_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()


def _chunked(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _ext_from_content_type(content_type: str) -> str | None:
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }
    mime = content_type.split(";", 1)[0].strip()
    return mapping.get(mime)


def enrich_scanned_mods(
    ids_or_paths: Iterable[str | tuple[str, str | Path]],
    client: SteamWorkshopClient | None = None,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[ModMetadata]:
    """
    High-level helper: resolve IDs (optionally with source paths) to metadata.

    Accepts either bare ID strings or ``(id, source_path)`` tuples.
    """
    pairs: list[tuple[str, str | None]] = []
    for item in ids_or_paths:
        if isinstance(item, tuple):
            pairs.append((str(item[0]), str(item[1]) if item[1] is not None else None))
        else:
            pairs.append((str(item), None))

    ids = [pid for pid, _ in pairs]
    own_client = client is None
    client = client or SteamWorkshopClient()
    try:
        metas = client.get_details_batch(ids, on_progress=on_progress)
    finally:
        if own_client:
            client.close()

    path_map = {pid: path for pid, path in pairs}
    for meta in metas:
        meta.source_path = path_map.get(meta.published_file_id)
    return metas
