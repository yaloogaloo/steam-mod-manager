"""Mod.io REST API client + URL parsing (read-only API key auth).

Isolated from Playwright offline archive. Does not scrape HTML pages.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse, urlunparse

import requests

logger = logging.getLogger(__name__)

MODIO_API_BASE = "https://api.mod.io/v1"
DEFAULT_TIMEOUT = (10.0, 30.0)  # connect, read

_SETTINGS_ORG = "SteamModManager"
_SETTINGS_APP = "WorkshopLibrary"
_SETTINGS_API_KEY = "modio/api_key"
_SETTINGS_GAME_ID_PREFIX = "modio/game_id/"

_ENV_API_KEYS = ("MODIO_API_KEY", "MOD_IO_API_KEY")

_USER_AGENT = (
    "SteamModManager/0.1 (+https://github.com/local/steam-mod-manager; desktop)"
)


def game_scoped_api_base(game_id: int | str) -> str:
    """
    Per-game Mod.io API host used by many titles (``g-{id}.modapi.io``).

    Some games resolve in the global catalog (``api.mod.io``) but expose Mod
    Objects only on the game-scoped host. Callers should fall back here when
    the global host returns HTTP 404 for ``games/{id}/…`` resources.
    """
    gid = int(game_id or 0)
    if gid <= 0:
        raise ValueError("game_id must be positive")
    return f"https://g-{gid}.modapi.io/v1"


def _resolve_proxies(
    proxies: Mapping[str, str | None] | None = None,
    *,
    proxy_url: str | None = None,
) -> dict[str, str] | None:
    """
    Reuse Sync Center / archive proxy config (``network/proxy_url``).

    Explicit ``proxies`` or ``proxy_url`` wins; otherwise read QSettings via
    ``archive_proxies_dict``.
    """
    if proxies is not None:
        cleaned = {
            str(k): str(v)
            for k, v in proxies.items()
            if v is not None and str(v).strip()
        }
        return cleaned or None
    from services.archive import archive_proxies_dict

    return archive_proxies_dict(proxy_url)


def _request_hostname(url: str) -> str:
    try:
        return urlparse(url).hostname or "api.mod.io"
    except Exception:  # noqa: BLE001
        return "api.mod.io"


def _format_connection_error(exc: BaseException, *, hostname: str) -> str:
    """Readable connection error without credentials or full URLs."""
    name = type(exc).__name__
    # Unwrap nested urllib3 / requests timeout names when present.
    cause = getattr(exc, "__cause__", None) or getattr(exc, "args", [None])[0]
    for candidate in (exc, cause):
        cname = type(candidate).__name__ if candidate is not None else ""
        if "ConnectTimeout" in cname or "ConnectTimeout" in str(candidate):
            name = "ConnectTimeout"
            break
        if "ReadTimeout" in cname or "ReadTimeout" in str(candidate):
            name = "ReadTimeout"
            break
        if "ProxyError" in cname:
            name = "ProxyError"
            break
    return f"Mod.io API connection failed: {name} {hostname}"


def _redact_secrets(text: str, *, api_key: str = "") -> str:
    """Strip API keys from exception / log text."""
    import re

    out = str(text or "")
    key = str(api_key or "").strip()
    if key and key in out:
        out = out.replace(key, "***")
    out = re.sub(r"(api_key=)[^&\s)\"']+", r"\1***", out, flags=re.IGNORECASE)
    return out


class ModioAPIError(Exception):
    """Raised when the Mod.io API cannot satisfy a read request."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ModioUrlParts:
    """Parsed official Mod.io Mod page URL."""

    game_slug: str
    mod_name_id: str
    canonical_url: str


@dataclass
class ModioModDetails:
    """Mapped fields from a Mod.io Mod Object."""

    mod_id: int
    game_id: int
    name: str
    name_id: str = ""
    summary: str = ""
    description: str = ""
    profile_url: str = ""
    logo_url: str = ""
    author: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def get_modio_api_key() -> str:
    """
    Resolve API key without embedding secrets in source.

    Order:
    1. ``MODIO_API_KEY`` / ``MOD_IO_API_KEY`` env
    2. ``config/modio.json`` via ``load_modio_config``
    3. QSettings ``modio/api_key`` (legacy fallback)
    """
    for env_name in _ENV_API_KEYS:
        value = str(os.environ.get(env_name) or "").strip()
        if value:
            return value

    try:
        from services.modio_config import ModioConfigError, load_modio_config

        cfg = load_modio_config(require_api_key=False)
        if cfg.api_key:
            return cfg.api_key
    except ModioConfigError as exc:
        # Missing/invalid file — fall through; callers that need a key use
        # load_modio_config(require_api_key=True) for a clear error.
        logger.debug("Mod.io config file not used: %s", exc)

    try:
        from PySide6.QtCore import QSettings

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        return str(settings.value(_SETTINGS_API_KEY, "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def set_modio_api_key(api_key: str) -> None:
    """Persist API key to QSettings (never logged)."""
    from PySide6.QtCore import QSettings

    settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
    settings.setValue(_SETTINGS_API_KEY, str(api_key or "").strip())
    settings.sync()


def _cached_game_id(game_slug: str) -> int | None:
    slug = str(game_slug or "").strip().lower()
    if not slug:
        return None
    try:
        from PySide6.QtCore import QSettings

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        raw = settings.value(f"{_SETTINGS_GAME_ID_PREFIX}{slug}", "")
        text = str(raw or "").strip()
        if text.isdigit():
            return int(text)
    except Exception:  # noqa: BLE001
        return None
    return None


def _store_game_id(game_slug: str, game_id: int) -> None:
    slug = str(game_slug or "").strip().lower()
    if not slug or int(game_id) <= 0:
        return
    try:
        from PySide6.QtCore import QSettings

        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        settings.setValue(f"{_SETTINGS_GAME_ID_PREFIX}{slug}", int(game_id))
        settings.sync()
    except Exception:  # noqa: BLE001
        pass


def parse_modio_url(url: str) -> ModioUrlParts | None:
    """
    Parse ``https://mod.io/g/<game>/m/<name_id>#…`` into slug parts.

    Fragments and query strings are stripped from the canonical URL.
    """
    text = str(url or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    host = (parsed.netloc or "").lower()
    if "mod.io" not in host and "modapi.io" not in host:
        return None

    parts = [p for p in (parsed.path or "").split("/") if p]
    game_slug = ""
    mod_name_id = ""
    if "g" in parts and "m" in parts:
        try:
            gi = parts.index("g")
            mi = parts.index("m")
            game_slug = parts[gi + 1].strip()
            mod_name_id = parts[mi + 1].strip()
        except (ValueError, IndexError):
            return None
    else:
        return None

    game_slug = game_slug.split("?")[0].strip()
    mod_name_id = mod_name_id.split("?")[0].strip()
    if not game_slug or not mod_name_id:
        return None

    canonical = urlunparse(
        ("https", "mod.io", f"/g/{game_slug}/m/{mod_name_id}", "", "", "")
    )
    return ModioUrlParts(
        game_slug=game_slug,
        mod_name_id=mod_name_id,
        canonical_url=canonical,
    )


def map_mod_object(payload: Mapping[str, Any] | None) -> ModioModDetails:
    """Map a Mod.io Mod Object dict into ``ModioModDetails``."""
    data = dict(payload or {})
    mod_id = int(data.get("id") or 0)
    game_id = int(data.get("game_id") or 0)
    name = str(data.get("name") or "").strip()
    name_id = str(data.get("name_id") or "").strip()
    summary = str(data.get("summary") or "").strip()
    description = str(data.get("description") or "").strip()
    profile_url = str(data.get("profile_url") or "").strip()

    logo = data.get("logo") if isinstance(data.get("logo"), dict) else {}
    logo_url = ""
    for key in ("original", "thumb_1280x720", "thumb_640x360", "thumb_320x180"):
        candidate = str((logo or {}).get(key) or "").strip()
        if candidate:
            logo_url = candidate
            break

    author = ""
    submitted = data.get("submitted_by")
    if isinstance(submitted, dict):
        author = str(
            submitted.get("username")
            or submitted.get("name")
            or submitted.get("display_name")
            or ""
        ).strip()

    return ModioModDetails(
        mod_id=mod_id,
        game_id=game_id,
        name=name,
        name_id=name_id,
        summary=summary,
        description=description or summary,
        profile_url=profile_url,
        logo_url=logo_url,
        author=author,
        raw=data,
    )


class ModioClient:
    """Thin read-only Mod.io REST client."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        session: requests.Session | None = None,
        timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
        base_url: str = MODIO_API_BASE,
        proxies: Mapping[str, str | None] | None = None,
        proxy_url: str | None = None,
    ) -> None:
        self.api_key = str(
            api_key if api_key is not None else get_modio_api_key()
        ).strip()
        # Always use separated connect/read timeouts when a bare float is passed.
        if isinstance(timeout, (int, float)):
            self.timeout = (float(timeout), float(timeout))
        else:
            self.timeout = (float(timeout[0]), float(timeout[1]))
        self.base_url = str(base_url or MODIO_API_BASE).rstrip("/")
        self._owns_session = session is None
        self._session = session or requests.Session()
        # Prefer Sync Center proxy (same as archive); do not force NO_PROXY.
        self._proxies = _resolve_proxies(proxies, proxy_url=proxy_url)
        if self._owns_session:
            self._session.trust_env = False
            if self._proxies:
                self._session.proxies.clear()
                self._session.proxies.update(self._proxies)
        self._session.headers.setdefault("User-Agent", _USER_AGENT)
        self._session.headers.setdefault("Accept", "application/json")
        logger.info(
            "Mod.io HTTP proxy %s",
            "enabled" if self._proxies else "disabled",
        )

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> ModioClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def require_api_key(self) -> str:
        if self.api_key:
            return self.api_key
        # Prefer the dedicated config loader error messages when available.
        try:
            from services.modio_config import load_modio_config

            self.api_key = load_modio_config(require_api_key=True).api_key
            return self.api_key
        except Exception as exc:  # noqa: BLE001
            from services.modio_config import ModioConfigError

            if isinstance(exc, ModioConfigError):
                raise ModioAPIError(str(exc)) from exc
        raise ModioAPIError(
            "Mod.io API key 未配置。请在 config/modio.json 填写 api_key，"
            "或设置环境变量 MODIO_API_KEY。"
        )

    def _request_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if self._proxies:
            kwargs["proxies"] = self._proxies
        return kwargs

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        key = self.require_api_key()
        query = dict(params or {})
        query["api_key"] = key
        url = f"{self.base_url}/{path.lstrip('/')}"
        hostname = _request_hostname(self.base_url)
        # Log endpoint without secrets.
        safe_params = {k: v for k, v in query.items() if k != "api_key"}
        logger.info("Calling Mod.io API")
        logger.info(
            "Calling Mod.io API endpoint: %s params=%s proxy=%s",
            url,
            safe_params,
            "enabled" if self._proxies else "disabled",
        )
        try:
            response = self._session.get(
                url,
                params=query,
                **self._request_kwargs(),
            )
        except requests.Timeout as exc:
            msg = _format_connection_error(exc, hostname=hostname)
            logger.exception(msg)
            raise ModioAPIError(msg) from exc
        except requests.RequestException as exc:
            msg = _format_connection_error(exc, hostname=hostname)
            logger.exception(msg)
            raise ModioAPIError(msg) from exc

        logger.info("Mod.io response status=%s", response.status_code)
        logger.info("Mod.io API response status: %s", response.status_code)

        if response.status_code == 401:
            raise ModioAPIError("Mod.io API key 无效或未授权", status_code=401)
        if response.status_code == 404:
            raise ModioAPIError("Mod.io 未找到该资源", status_code=404)
        if response.status_code == 429:
            raise ModioAPIError(
                "Mod.io API 请求过于频繁（rate limit）", status_code=429
            )
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                err = body.get("error") if isinstance(body, dict) else None
                if isinstance(err, dict):
                    detail = str(err.get("message") or "").strip()
                elif isinstance(body, dict):
                    detail = str(body.get("message") or "").strip()
            except Exception:  # noqa: BLE001
                detail = (response.text or "")[:200]
            detail = _redact_secrets(detail, api_key=key)
            raise ModioAPIError(
                detail or f"Mod.io API HTTP {response.status_code}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ModioAPIError(f"Mod.io API 返回了无效 JSON: {exc}") from exc

    def _get_with_base(
        self,
        base_url: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Issue ``_get`` against a temporary API base URL."""
        previous = self.base_url
        try:
            self.base_url = str(base_url or "").rstrip("/") or previous
            return self._get(path, params=params)
        finally:
            self.base_url = previous

    def _get_game_resource(
        self,
        game_id: int,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """
        Fetch a ``games/{id}/…`` resource, falling back to the game-scoped host.

        Global ``api.mod.io`` can list a game by ``name_id`` while still returning
        HTTP 404 for that game's Mod Objects. Retrying on
        ``https://g-{game_id}.modapi.io/v1`` is the portable fix for those titles.
        """
        gid = int(game_id or 0)
        try:
            return self._get(path, params=params)
        except ModioAPIError as exc:
            if exc.status_code != 404 or gid <= 0:
                raise
            scoped = game_scoped_api_base(gid)
            if self.base_url.rstrip("/") == scoped.rstrip("/"):
                raise
            logger.info(
                "Mod.io global host 404 for game_id=%s; retrying scoped host %s",
                gid,
                scoped,
            )
            try:
                return self._get_with_base(scoped, path, params=params)
            except ModioAPIError as scoped_exc:
                if scoped_exc.status_code == 404:
                    raise ModioAPIError(
                        "Mod.io 未找到该游戏/Mod（全局与游戏专属 API 均返回 404）。"
                        "请确认链接正确，或检查 API Key 是否有权访问该游戏。",
                        status_code=404,
                    ) from scoped_exc
                raise

    def resolve_game_id(self, game_slug: str) -> int:
        slug = str(game_slug or "").strip()
        if not slug:
            raise ModioAPIError("Mod.io game slug 为空")
        cached = _cached_game_id(slug)
        if cached:
            logger.info(
                "Using cached Mod.io game_id=%s for slug=%s", cached, slug
            )
            return cached
        payload = self._get("games", params={"name_id": slug, "limit": 1})
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ModioAPIError(f"未找到 Mod.io 游戏: {slug}", status_code=404)
        game_id = int(rows[0].get("id") or 0)
        if game_id <= 0:
            raise ModioAPIError(f"Mod.io 游戏 ID 无效: {slug}")
        _store_game_id(slug, game_id)
        return game_id

    def get_mod(self, game_id: int, mod_id: int) -> ModioModDetails:
        gid = int(game_id)
        payload = self._get_game_resource(
            gid, f"games/{gid}/mods/{int(mod_id)}"
        )
        if not isinstance(payload, dict):
            raise ModioAPIError("Mod.io Get Mod 返回格式无效")
        details = map_mod_object(payload)
        if details.mod_id <= 0:
            raise ModioAPIError("Mod.io Mod Object 缺少 id")
        return details

    def find_mod_by_name_id(self, game_id: int, name_id: str) -> ModioModDetails:
        slug = str(name_id or "").strip()
        if not slug:
            raise ModioAPIError("Mod.io name_id 为空")
        gid = int(game_id)
        if slug.isdigit():
            return self.get_mod(gid, int(slug))
        payload = self._get_game_resource(
            gid,
            f"games/{gid}/mods",
            params={"name_id": slug, "limit": 5},
        )
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ModioAPIError(f"未找到 Mod.io Mod: {slug}", status_code=404)
        chosen = None
        for row in rows:
            if str(row.get("name_id") or "").strip().lower() == slug.lower():
                chosen = row
                break
        if chosen is None:
            chosen = rows[0]
        details = map_mod_object(chosen)
        if details.mod_id <= 0:
            raise ModioAPIError(f"Mod.io Mod 解析失败: {slug}")
        return details

    def resolve_mod(
        self,
        *,
        game_slug: str = "",
        mod_name_id: str = "",
        game_id: int = 0,
        mod_id: int = 0,
    ) -> ModioModDetails:
        """Resolve a Mod by numeric ids when known, otherwise by slug + name_id."""
        gid = int(game_id or 0)
        mid = int(mod_id or 0)
        if gid <= 0 and game_slug:
            gid = self.resolve_game_id(game_slug)
        if gid <= 0:
            raise ModioAPIError("无法解析 Mod.io game_id")
        if mid > 0:
            return self.get_mod(gid, mid)
        return self.find_mod_by_name_id(gid, mod_name_id)

    def download_file(self, url: str, dest: str | Path) -> Path:
        """Download a binary URL (logo) to *dest*."""
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        hostname = _request_hostname(url)
        try:
            response = self._session.get(
                url,
                stream=True,
                **self._request_kwargs(),
            )
            response.raise_for_status()
        except requests.Timeout as exc:
            msg = _format_connection_error(exc, hostname=hostname)
            logger.exception(msg)
            raise ModioAPIError(msg) from exc
        except requests.RequestException as exc:
            msg = _format_connection_error(exc, hostname=hostname)
            logger.exception(msg)
            raise ModioAPIError(msg) from exc
        with target.open("wb") as fh:
            for chunk in response.iter_content(64 * 1024):
                if chunk:
                    fh.write(chunk)
        return target
