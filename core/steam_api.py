"""Steam Web API client with SQLite snapshot interception."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, TypeVar

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .db_manager import DatabaseManager, get_db
from .game_info import GameInfo
from .models import ModMetadata
from .sanitize import sanitize_folder_name
from .scraper import WorkshopPageScraper

logger = logging.getLogger(__name__)

T = TypeVar("T")

STEAM_PUBLISHED_FILE_DETAILS_URL = (
    "https://api.steampowered.com/ISteamRemoteStorage/"
    "GetPublishedFileDetails/v1/"
)
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"

DEFAULT_BATCH_SIZE = 100
DEFAULT_TIMEOUT = 10
DEFAULT_USER_AGENT = (
    "SteamModManager/0.1 (+https://github.com/local/steam-mod-manager; desktop)"
)
NO_PROXY: dict[str, str | None] = {"http": None, "https": None}


class SteamAPIError(Exception):
    """Raised when the Steam Web API returns an unexpected response."""


def _build_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.proxies.update({"http": None, "https": None})
    session.headers.setdefault("User-Agent", user_agent)
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class SteamWorkshopClient:
    """
    Fetch Workshop / Store metadata with SQLite-first caching.

    Flow: **query DB → network only for misses → upsert DB**.
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        batch_size: int = DEFAULT_BATCH_SIZE,
        request_interval: float = 0.2,
        user_agent: str = DEFAULT_USER_AGENT,
        enable_scrape_fallback: bool = True,
        db: DatabaseManager | None = None,
    ) -> None:
        self.timeout = timeout
        self.batch_size = max(1, min(batch_size, 100))
        self.request_interval = max(0.0, request_interval)
        self.enable_scrape_fallback = enable_scrape_fallback
        self._owns_session = session is None
        self._session = session or _build_session(user_agent)
        if session is not None:
            self._session.trust_env = False
            self._session.headers.setdefault("User-Agent", user_agent)
        self._last_request_at = 0.0
        self._scraper: WorkshopPageScraper | None = None
        self._failed_app_ids: set[str] = set()
        self.db = db or get_db()

    @property
    def session(self) -> requests.Session:
        return self._session

    def close(self) -> None:
        if self._scraper is not None:
            self._scraper.close()
            self._scraper = None
        if self._owns_session:
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

    def _request(
        self,
        method: str,
        url: str,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        kwargs.setdefault("proxies", NO_PROXY)
        kwargs.setdefault("timeout", timeout if timeout is not None else self.timeout)
        self._throttle()
        return self._session.request(method, url, **kwargs)

    # ------------------------------------------------------------------
    # Games — SQLite first
    # ------------------------------------------------------------------

    def get_app_name(self, app_id: int | str, *, english: bool = True) -> str | None:
        info = self.get_game_info(app_id, english=english)
        if info.fetch_error and not info.name:
            return None
        name = info.name.strip()
        if name.startswith("App_") and info.fetch_error:
            return None
        return name or None

    def get_game_folder_name(self, app_id: int | str) -> str:
        info = self.get_game_info(app_id)
        return info.folder_name or info.display_name

    def get_game_info(self, app_id: int | str, *, english: bool = True) -> GameInfo:
        """
        Resolve Store metadata. Never raises.

        1. ``SELECT`` from ``games``
        2. On miss → Store API
        3. On success → ``INSERT`` / upsert
        """
        key = str(app_id).strip()
        if not key or key == "0" or not key.isdigit():
            return GameInfo.fallback(0, "missing app id")

        numeric_id = int(key)
        fallback = GameInfo.fallback(numeric_id)

        try:
            cached = self.db.get_game(numeric_id)
            if cached and cached.name:
                return cached

            if key in self._failed_app_ids:
                return fallback

            payload = self._fetch_appdetails_payload(key, english=english)
            if payload is None:
                self._failed_app_ids.add(key)
                return fallback

            name = str(payload.get("name") or "").strip()
            header = str(payload.get("header_image") or "").strip()
            desc = str(payload.get("short_description") or "").strip()
            if not name:
                self._failed_app_ids.add(key)
                return fallback

            info = GameInfo(
                app_id=numeric_id,
                name=name,
                header_image=header,
                short_description=desc,
                folder_name=sanitize_folder_name(name, fallback=fallback.name),
            )
            self.db.upsert_game(info)
            self._failed_app_ids.discard(key)
            return info
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "get_game_info(%s) failed (%s) — using %s",
                key,
                exc,
                fallback.name,
            )
            self._failed_app_ids.add(key)
            return fallback

    def resolve_game_names(
        self,
        metas: Sequence[ModMetadata],
        *,
        on_progress: Callable[[int, int], None] | None = None,
        max_workers: int = 8,
    ) -> None:
        """Fill ``meta.game_name`` using DB-first unique AppID resolution."""
        if not metas:
            return

        for meta in metas:
            if not meta.app_id and meta.source_path:
                inferred = _infer_app_id_from_path(meta.source_path)
                if inferred:
                    meta.app_id = inferred

        resolved: dict[int, str] = {}
        missing: list[int] = []
        for meta in metas:
            app_id = int(meta.app_id or 0)
            if not app_id:
                resolved[0] = "Unknown Game"
                continue
            if app_id in resolved:
                continue
            cached = self.db.get_game(app_id)
            if cached and cached.name:
                resolved[app_id] = cached.folder_name or cached.display_name
            else:
                missing.append(app_id)

        if missing:
            workers = max(1, min(max_workers, len(missing)))

            def _fetch(app_id: int) -> tuple[int, GameInfo]:
                with SteamWorkshopClient(
                    timeout=self.timeout,
                    enable_scrape_fallback=False,
                    request_interval=0.05,
                    db=self.db,
                ) as client:
                    return app_id, client.get_game_info(app_id)

            done = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_fetch, aid): aid for aid in missing}
                for fut in as_completed(futures):
                    app_id, info = fut.result()
                    resolved[app_id] = info.folder_name or info.display_name
                    done += 1
                    if on_progress:
                        on_progress(done, len(missing))

        for meta in metas:
            app_id = int(meta.app_id or 0)
            meta.game_name = resolved.get(
                app_id,
                f"App_{app_id}" if app_id else "Unknown Game",
            )
        if on_progress and not missing:
            on_progress(len(metas), len(metas))

    def _fetch_appdetails_payload(
        self, app_id: str, *, english: bool
    ) -> dict[str, Any] | None:
        params: dict[str, str] = {"appids": app_id}
        if english:
            params["l"] = "english"
        try:
            response = self._request(
                "GET",
                STEAM_APPDETAILS_URL,
                params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            logger.warning(
                "Store appdetails failed for %s (%s) — will use App_%s",
                app_id,
                exc,
                app_id,
            )
            return None
        except ValueError as exc:
            logger.warning("Store appdetails JSON error for %s: %s", app_id, exc)
            return None

        entry = payload.get(str(app_id)) if isinstance(payload, dict) else None
        if not isinstance(entry, dict) or not entry.get("success"):
            logger.info("Store returned no data for app %s", app_id)
            return None
        data = entry.get("data")
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # Mods — SQLite first, network only for misses
    # ------------------------------------------------------------------

    def get_details(self, published_file_id: str | int) -> ModMetadata:
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
        scrape_workers: int = 12,
    ) -> list[ModMetadata]:
        """
        Resolve many Mod IDs with DB interception.

        Already-known IDs are served from SQLite; only missing IDs are
        batched to ``GetPublishedFileDetails`` and then upserted.
        """
        ids = [str(i) for i in published_file_ids if str(i).strip()]
        if not ids:
            return []

        cached = self.db.get_mods_by_ids(ids)
        missing = [
            mid
            for mid in ids
            if mid not in cached or not cached[mid].title
        ]

        fetched: dict[str, ModMetadata] = {}
        if missing:
            total_missing = len(missing)
            done = 0
            for chunk in _chunked(missing, self.batch_size):
                batch = self._request_published_file_details(list(chunk))
                if self.enable_scrape_fallback:
                    batch = self._parallel_scrape_fallback(
                        batch, max_workers=scrape_workers
                    )
                to_store: list[ModMetadata] = []
                for meta in batch:
                    fetched[meta.published_file_id] = meta
                    if meta.title and not meta.fetch_error:
                        to_store.append(meta)
                if to_store:
                    self.db.upsert_mods(to_store)
                done += len(chunk)
                if on_progress:
                    # Progress over full requested set (cached + fetched)
                    hit = len(ids) - total_missing
                    on_progress(hit + done, len(ids))
        elif on_progress:
            on_progress(len(ids), len(ids))

        ordered: list[ModMetadata] = []
        for mid in ids:
            if mid in fetched and fetched[mid].title:
                ordered.append(fetched[mid])
            elif mid in cached and cached[mid].title:
                ordered.append(cached[mid])
            elif mid in fetched:
                ordered.append(fetched[mid])
            else:
                ordered.append(
                    ModMetadata(
                        published_file_id=mid,
                        fetch_error="Not in local DB and network fetch failed",
                    )
                )
        return ordered

    def _parallel_scrape_fallback(
        self,
        batch: list[ModMetadata],
        *,
        max_workers: int = 12,
    ) -> list[ModMetadata]:
        need_idx = [
            i for i, meta in enumerate(batch) if meta.fetch_error or not meta.title
        ]
        if not need_idx:
            return batch

        results = list(batch)
        workers = max(1, min(max_workers, len(need_idx)))

        def _scrape_one(meta: ModMetadata) -> ModMetadata:
            with WorkshopPageScraper(timeout=self.timeout) as scraper:
                try:
                    scraped = scraper.fetch(meta.published_file_id)
                except requests.RequestException as exc:
                    logger.warning(
                        "Workshop page scrape failed for %s: %s",
                        meta.published_file_id,
                        exc,
                    )
                    return meta
                if scraped.title and not scraped.fetch_error:
                    scraped.source_path = meta.source_path
                    return scraped
                if scraped.fetch_error and meta.fetch_error:
                    scraped.fetch_error = (
                        f"{meta.fetch_error}; scrape: {scraped.fetch_error}"
                    )
                return scraped if scraped.title else meta

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {pool.submit(_scrape_one, batch[i]): i for i in need_idx}
            for fut in as_completed(future_map):
                idx = future_map[fut]
                try:
                    results[idx] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Scrape worker failed: %s", exc)
        return results

    def download_preview(
        self,
        preview_url: str,
        dest_path: str | Path,
        *,
        overwrite: bool = True,
    ) -> Path | None:
        if not preview_url:
            return None

        dest = Path(dest_path)
        if dest.exists() and not overwrite:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)

        try:
            response = self._request(
                "GET",
                preview_url,
                timeout=self.timeout,
                stream=True,
            )
            response.raise_for_status()
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
        if not metadata.preview_url:
            return None
        dest_dir = Path(dest_dir)
        url_path = metadata.preview_url.split("?", 1)[0]
        suffix = Path(url_path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            suffix = ""
        target = dest_dir / f"{filename}{suffix}"
        saved = self.download_preview(metadata.preview_url, target)
        if saved:
            metadata.cover_path = str(saved)
        return saved

    def _request_published_file_details(
        self, published_file_ids: Sequence[str]
    ) -> list[ModMetadata]:
        form: dict[str, str | int] = {"itemcount": len(published_file_ids)}
        for index, file_id in enumerate(published_file_ids):
            form[f"publishedfileids[{index}]"] = file_id

        try:
            response = self._request(
                "POST",
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
            logger.error("Invalid JSON from Steam API: %s", exc)
            return [
                ModMetadata(
                    published_file_id=fid,
                    fetch_error=f"Invalid JSON from Steam API: {exc}",
                )
                for fid in published_file_ids
            ]

        try:
            details = payload["response"]["publishedfiledetails"]
        except (KeyError, TypeError):
            logger.error("Unexpected Steam API payload shape")
            return [
                ModMetadata(
                    published_file_id=fid,
                    fetch_error="Unexpected Steam API payload shape",
                )
                for fid in published_file_ids
            ]

        by_id = {
            str(item.get("publishedfileid", "")): ModMetadata.from_api_response(item)
            for item in details
            if isinstance(item, dict)
        }
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


def _infer_app_id_from_path(source_path: str | Path) -> int:
    path = Path(source_path)
    parent = path.parent.name
    if parent.isdigit():
        return int(parent)
    return 0


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
        client.resolve_game_names(metas)
    finally:
        if own_client:
            client.close()

    path_map = {pid: path for pid, path in pairs}
    for meta in metas:
        meta.source_path = path_map.get(meta.published_file_id)
    return metas
