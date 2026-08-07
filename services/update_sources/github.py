"""GitHub Releases update source (public API, optional token)."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from services.update_sources.base import UpdateSource, VersionCheckResult

_GITHUB_REPO_RE = re.compile(
    r"github\.com[/:](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?",
    re.IGNORECASE,
)


def parse_github_repo(source_url: str = "", external_id: str = "") -> tuple[str, str] | None:
    ext = (external_id or "").strip()
    if "/" in ext and not ext.startswith("http"):
        parts = ext.strip("/").split("/")
        if len(parts) >= 2:
            return parts[0], parts[1]
    url = (source_url or "").strip()
    if not url:
        return None
    m = _GITHUB_REPO_RE.search(url)
    if m:
        return m.group("owner"), m.group("repo")
    # owner/repo path style
    try:
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        if len(parts) >= 2:
            return parts[0], parts[1]
    except Exception:  # noqa: BLE001
        return None
    return None


class GithubUpdateSource(UpdateSource):
    platform = "github"

    def __init__(self, *, token: str | None = None, opener=None) -> None:
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN", "")
        self._opener = opener

    def check_version(
        self,
        *,
        mod_id: str,
        source_url: str = "",
        external_id: str = "",
        **kwargs: Any,
    ) -> VersionCheckResult:
        repo = parse_github_repo(source_url, external_id)
        if repo is None:
            return VersionCheckResult(
                supported=True,
                latest="",
                source="github",
                error="missing github repo (source_url / external_id)",
            )
        owner, name = repo
        api = f"https://api.github.com/repos/{owner}/{name}/releases/latest"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "steam-mod-manager-update-check",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            req = urllib.request.Request(api, headers=headers)
            if self._opener is not None:
                raw = self._opener(req)
            else:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read()
            data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except urllib.error.HTTPError as exc:
            return VersionCheckResult(
                supported=True,
                latest="",
                source="github",
                error=f"http {exc.code}",
            )
        except Exception as exc:  # noqa: BLE001
            return VersionCheckResult(
                supported=True,
                latest="",
                source="github",
                error=str(exc),
            )
        if not isinstance(data, dict):
            return VersionCheckResult(
                supported=True, latest="", source="github", error="invalid response"
            )
        tag = str(data.get("tag_name") or data.get("name") or "").strip()
        if tag.lower().startswith("v") and len(tag) > 1 and tag[1].isdigit():
            tag = tag[1:]
        return VersionCheckResult(
            supported=True,
            latest=tag,
            source="github",
            raw=data if isinstance(data, dict) else None,
        )
