"""GitHub offline provider — local HTML only (no GitHub API)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.db_manager import get_db
from core.mod_platform import PLATFORM_GITHUB, normalize_platform
from core.paths import default_mod_library
from services.file_ops import ModFileManager
from services.importers.materialize import find_managed_mod_path
from services.offline.base import (
    OFFLINE_STATUS_FAILED,
    OFFLINE_STATUS_GENERATED,
    OfflineProvider,
    OfflineUpdateResult,
    PROVIDER_GITHUB_GENERATOR,
)
from services.offline.generator import write_offline_html

_README_NAMES = ("README.md", "Readme.md", "readme.md", "README.MD")


def _read_local_readme(managed_path: Path) -> str:
    root = Path(managed_path)
    for name in _README_NAMES:
        candidate = root / name
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
    # Shallow search one level down (common repo layout)
    try:
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            for name in _README_NAMES:
                candidate = child / name
                if candidate.is_file():
                    try:
                        return candidate.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        return ""
    except OSError:
        pass
    return ""


class GithubOfflineProvider(OfflineProvider):
    """Generate ``.info/index.html`` from DB + local files / README."""

    def can_handle(self, mod: Any) -> bool:
        platform = getattr(mod, "platform", None)
        if platform is None and isinstance(mod, dict):
            platform = mod.get("platform")
        return normalize_platform(str(platform or "")) == PLATFORM_GITHUB

    def get_provider_name(self) -> str:
        return PROVIDER_GITHUB_GENERATOR

    def update_offline_page(
        self,
        mod_id: str | int,
        *,
        managed_path: str | Path | None = None,
        library_root: str | Path | None = None,
        metadata: Any | None = None,
    ) -> OfflineUpdateResult:
        mid = str(mod_id).strip()
        root = Path(library_root) if library_root else default_mod_library()
        path = Path(managed_path) if managed_path else find_managed_mod_path(root, mid)
        if path is None:
            raise FileNotFoundError(f"Managed Mod folder not found for mod_id={mid}")

        info = get_db().get_mod_display_info(mid)
        if info is None:
            raise ValueError(f"Mod not found in database: {mid}")

        mgr = ModFileManager(root)
        info_dir = mgr.ensure_info_dir(path)
        cover = mgr.find_local_cover(path)
        files = get_db().get_mod_files(mid)
        readme = _read_local_readme(path)
        repo = (info.external_id or "").strip()

        try:
            index = write_offline_html(
                info_dir,
                title=info.display_name or info.steam_name or repo or mid,
                platform=PLATFORM_GITHUB,
                metadata={
                    "external_id": info.external_id,
                    "source_url": info.source_url,
                    "repository": repo,
                    "readme": readme,
                    "description": info.steam_description or info.custom_description,
                },
                files=files,
                cover=cover,
                description=info.custom_description or info.steam_description,
            )
            status = OFFLINE_STATUS_GENERATED
            error = ""
        except Exception as exc:  # noqa: BLE001
            get_db().update_mod_offline_status(
                mid,
                status=OFFLINE_STATUS_FAILED,
                provider=self.get_provider_name(),
            )
            raise RuntimeError(str(exc)) from exc

        get_db().update_mod_offline_status(
            mid,
            status=status,
            provider=self.get_provider_name(),
        )
        return OfflineUpdateResult(
            mod_id=mid,
            index_path=index,
            status=status,
            provider=self.get_provider_name(),
            error=error,
        )
