"""HTML scraping fallback for Steam Workshop item pages."""

from __future__ import annotations

import logging
import re
from typing import Any

import requests
from bs4 import BeautifulSoup

from .models import ModMetadata

logger = logging.getLogger(__name__)

WORKSHOP_PAGE_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id={id}"


class WorkshopPageScraper:
    """
    Scrape a Workshop item page when the Web API is unavailable or returns
    a non-OK result (common behind unstable networks / GFW).
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 30,
        user_agent: str | None = None,
    ) -> None:
        self.timeout = timeout
        self._session = session or requests.Session()
        self._owns_session = session is None
        ua = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
        self._session.headers.setdefault("User-Agent", ua)
        self._session.headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> WorkshopPageScraper:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch(self, published_file_id: str | int) -> ModMetadata:
        file_id = str(published_file_id)
        url = WORKSHOP_PAGE_URL.format(id=file_id)
        try:
            response = self._session.get(url, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Workshop page fetch failed for %s: %s", file_id, exc)
            return ModMetadata(published_file_id=file_id, fetch_error=str(exc))

        return self._parse(file_id, response.text, url)

    def _parse(self, file_id: str, html: str, url: str) -> ModMetadata:
        soup = BeautifulSoup(html, "lxml")

        # Age-gate / error pages
        if soup.select_one(".error_ctn") or "error_ctn" in html:
            return ModMetadata(
                published_file_id=file_id,
                fetch_error="Workshop page returned an error / age gate",
            )

        title = _first_text(
            soup,
            [
                ("meta", {"property": "og:title"}, "content"),
                ("div", {"class": "workshopItemTitle"}, None),
                ("title", {}, None),
            ],
        )
        if title:
            title = title.replace(":: Steam Workshop", "").strip(" :-")

        description = _first_text(
            soup,
            [
                ("meta", {"property": "og:description"}, "content"),
                ("div", {"class": "workshopItemDescription"}, None),
            ],
        )

        preview_url = _first_attr(
            soup,
            [
                ("meta", {"property": "og:image"}, "content"),
                ("img", {"id": "previewImage"}, "src"),
                ("img", {"class": "workshopItemPreviewImage"}, "src"),
            ],
        )

        app_id = _extract_app_id(soup, html)
        tags = [
            t.get_text(strip=True)
            for t in soup.select(".workshopTags a, .workshopItemTag")
            if t.get_text(strip=True)
        ]

        if not title:
            return ModMetadata(
                published_file_id=file_id,
                fetch_error=f"Could not parse title from {url}",
            )

        return ModMetadata(
            published_file_id=file_id,
            title=title,
            description=description or "",
            preview_url=preview_url or "",
            app_id=app_id,
            tags=tags,
        )


def _first_text(
    soup: BeautifulSoup,
    selectors: list[tuple[str, dict[str, Any], str | None]],
) -> str:
    for tag, attrs, attr_name in selectors:
        node = soup.find(tag, attrs=attrs) if attrs else soup.find(tag)
        if not node:
            continue
        if attr_name:
            value = node.get(attr_name)
            if value:
                return str(value).strip()
        else:
            text = node.get_text(" ", strip=True)
            if text:
                return text
    return ""


def _first_attr(
    soup: BeautifulSoup,
    selectors: list[tuple[str, dict[str, Any], str]],
) -> str:
    for tag, attrs, attr_name in selectors:
        node = soup.find(tag, attrs=attrs)
        if not node:
            continue
        value = node.get(attr_name)
        if value:
            return str(value).strip()
    return ""


def _extract_app_id(soup: BeautifulSoup, html: str) -> int:
    link = soup.select_one('a[href*="steamcommunity.com/app/"]')
    if link and link.get("href"):
        match = re.search(r"/app/(\d+)", link["href"])
        if match:
            return int(match.group(1))

    match = re.search(r'"AppId"\s*:\s*"?(\d+)', html)
    if match:
        return int(match.group(1))
    return 0
