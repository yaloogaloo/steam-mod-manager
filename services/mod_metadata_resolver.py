"""Unified Mod metadata resolver — single read path for Library / Detail / assets."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.mod_platform import PLATFORM_STEAM, normalize_platform, parse_metadata_platform
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, read_info_metadata_dict
from services.metadata_backup import load_backup

logger = logging.getLogger(__name__)


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _usable_file(
    path: Path | str | None, *, allow_empty: bool = False
) -> Path | None:
    if path is None:
        return None
    try:
        candidate = Path(path)
        if not candidate.is_file():
            return None
        if not allow_empty and candidate.stat().st_size <= 0:
            return None
        return candidate.resolve()
    except OSError:
        return None
    return None


def _folder_exists(path: str | Path | None) -> bool:
    if path is None:
        return False
    try:
        return Path(path).is_dir()
    except OSError:
        return False


@dataclass
class ResolvedModMetadata:
    """Display payload after applying .info / backup / SQLite priority."""

    published_file_id: str
    display_name: str = ""
    description: str = ""
    platform: str = PLATFORM_STEAM
    source_url: str = ""
    workspace_id: str = ""
    cover_path: str = ""
    offline_path: str = ""
    tags: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    folder_present: bool = True
    managed_path: str = ""
    author: str = ""
    category: str = ""
    title: str = ""
    game_name: str = ""
    app_id: int = 0
    favorite: bool = False
    user_notes: str = ""

    def to_mod_metadata(self) -> ModMetadata:
        meta = ModMetadata(
            published_file_id=self.published_file_id,
            title=self.title or self.display_name,
            description=self.description,
            app_id=int(self.app_id or 0),
            game_name=self.game_name,
            managed_path=self.managed_path or None,
            local_path=self.managed_path or None,
            url=self.source_url,
            cover_path=self.cover_path or None,
            offline_page_path=self.offline_path or None,
            author=self.author,
            source_type=self.platform,
            json_display_name=self.display_name,
            tags=list(self.tags or []),
        )
        return meta


class ModMetadataResolver:
    """Single entry for UI metadata / cover / offline resolution."""

    def resolve(
        self,
        mod_id: int | str | None = None,
        managed_path: str | Path | None = None,
    ) -> ResolvedModMetadata | None:
        """Pure read: never writes backup / .info / SQLite backup status."""
        mid, path = self._resolve_identity(mod_id, managed_path)
        if _folder_exists(path):
            return self.resolve_existing_folder(mid, path)
        return self.resolve_missing_folder(mid, path)

    def resolve_existing_folder(
        self,
        mod_id: int | str | None,
        managed_path: str | Path,
    ) -> ResolvedModMetadata | None:
        """Directory exists: ``.info`` > backup > SQLite (SQLite only supplements)."""
        root = Path(managed_path)
        if not root.is_dir():
            return self.resolve_missing_folder(mod_id, root)

        info = read_info_metadata_dict(root) or {}
        mid = _first_text(
            mod_id,
            info.get("published_file_id"),
            root.name if root.name.isdigit() else "",
        )

        backup = load_backup(mid) if str(mid).isdigit() else None
        sqlite = self._sqlite_row(mid)
        display = self._sqlite_display(mid)

        display_name = _first_text(
            info.get("display_name"),
            getattr(display, "user_display_name", "") if display else "",
            info.get("title"),
            (backup.metadata.get("display_name") if backup else ""),
            (backup.metadata.get("title") if backup else ""),
        )
        description = _first_text(
            info.get("description"),
            info.get("custom_description"),
            (backup.metadata.get("description") if backup else ""),
        )
        platform = parse_metadata_platform(info) or _first_text(
            (backup.metadata.get("source_type") if backup else ""),
            (backup.metadata.get("platform") if backup else ""),
            getattr(display, "platform", "") if display else "",
            PLATFORM_STEAM,
        )
        source_url = _first_text(
            info.get("url"),
            info.get("source_url"),
            info.get("website"),
            (backup.metadata.get("url") if backup else ""),
            (backup.metadata.get("source_url") if backup else ""),
            getattr(display, "source_url", "") if display else "",
        )
        workspace_id = _first_text(
            info.get("workspace_id"),
            (backup.metadata.get("workspace_id") if backup else ""),
            getattr(display, "workspace_id", "") if display else "",
        )
        cover = self._cover_existing(root, info, backup, sqlite)
        offline = self._offline_existing(root, backup)
        deps = _dependencies_from_mapping(info)
        if not deps and backup is not None:
            deps = _dependencies_from_mapping(backup.metadata)
        tags, category = self._tags_from_sqlite(mid)
        author = _first_text(
            info.get("author"),
            (backup.metadata.get("author") if backup else ""),
        )
        game_name = _first_text(
            info.get("game_name"),
            (backup.metadata.get("game_name") if backup else ""),
            root.parent.name,
        )
        app_id = int(
            info.get("app_id")
            or (backup.metadata.get("app_id") if backup else 0)
            or (sqlite.get("app_id") if sqlite else 0)
            or 0
        )
        return ResolvedModMetadata(
            published_file_id=str(mid),
            display_name=display_name,
            description=description,
            platform=normalize_platform(platform),
            source_url=source_url,
            workspace_id=workspace_id,
            cover_path=str(cover) if cover else "",
            offline_path=str(offline) if offline else "",
            tags=tags,
            dependencies=deps,
            folder_present=True,
            managed_path=str(root),
            author=author,
            category=category,
            title=_first_text(info.get("title"), display_name),
            game_name=game_name,
            app_id=app_id,
            favorite=bool(getattr(display, "favorite", False)) if display else False,
            user_notes=str(getattr(display, "user_notes", "") or "") if display else "",
        )

    def resolve_missing_folder(
        self,
        mod_id: int | str | None,
        managed_path: str | Path | None = None,
    ) -> ResolvedModMetadata | None:
        """Directory missing: backup files > SQLite cache. Never read dead ``.info``."""
        mid = _first_text(mod_id)
        sqlite = self._sqlite_row(mid) if mid.isdigit() else None
        if not mid.isdigit() and managed_path is not None:
            sqlite = self._sqlite_row_by_path(managed_path) or sqlite
            if sqlite is not None:
                mid = str(sqlite.get("mod_id") or mid)
        backup = load_backup(mid) if mid.isdigit() else None
        if backup is None and sqlite is None and not mid:
            return None

        bmeta: dict[str, Any] = dict(backup.metadata) if backup else {}
        display = self._sqlite_display(mid)
        path = ""
        if managed_path is not None:
            path = str(managed_path)
        if not path and sqlite is not None:
            path = str(sqlite.get("last_known_path") or "")
        if not path and backup is not None:
            path = str(backup.last_known_path or "")

        display_name = _first_text(
            bmeta.get("display_name"),
            bmeta.get("title"),
            getattr(display, "display_name", "") if display else "",
            getattr(display, "steam_name", "") if display else "",
        )
        description = _first_text(
            bmeta.get("description"),
            bmeta.get("custom_description"),
            getattr(display, "custom_description", "") if display else "",
            getattr(display, "steam_description", "") if display else "",
        )
        platform = parse_metadata_platform(bmeta) or _first_text(
            bmeta.get("source_type"),
            bmeta.get("platform"),
            getattr(display, "platform", "") if display else "",
            PLATFORM_STEAM,
        )
        source_url = _first_text(
            bmeta.get("url"),
            bmeta.get("source_url"),
            bmeta.get("website"),
            getattr(display, "source_url", "") if display else "",
        )
        workspace_id = _first_text(
            bmeta.get("workspace_id"),
            getattr(display, "workspace_id", "") if display else "",
        )
        cover = self._cover_missing(mid, backup, sqlite, display)
        offline = self._offline_missing(backup, sqlite)
        deps = _dependencies_from_mapping(bmeta)
        tags, category = self._tags_from_sqlite(mid)
        author = _first_text(bmeta.get("author"))
        game_name = _first_text(
            bmeta.get("game_name"),
            Path(path).parent.name if path else "",
        )
        app_id = int(
            bmeta.get("app_id")
            or (sqlite.get("app_id") if sqlite else 0)
            or 0
        )
        return ResolvedModMetadata(
            published_file_id=str(mid),
            display_name=display_name,
            description=description,
            platform=normalize_platform(platform),
            source_url=source_url,
            workspace_id=workspace_id,
            cover_path=str(cover) if cover else "",
            offline_path=str(offline) if offline else "",
            tags=tags,
            dependencies=deps,
            folder_present=False,
            managed_path=path,
            author=author,
            category=category,
            title=_first_text(bmeta.get("title"), display_name),
            game_name=game_name,
            app_id=app_id,
            favorite=bool(getattr(display, "favorite", False)) if display else False,
            user_notes=str(getattr(display, "user_notes", "") or "") if display else "",
        )

    def resolve_cover_path(
        self,
        mod_id: int | str | None = None,
        managed_path: str | Path | None = None,
    ) -> Path | None:
        resolved = self.resolve(mod_id, managed_path)
        if resolved is None or not resolved.cover_path:
            return None
        return _usable_file(resolved.cover_path)

    def resolve_offline_page(
        self,
        mod_id: int | str | None = None,
        managed_path: str | Path | None = None,
    ) -> Path | None:
        resolved = self.resolve(mod_id, managed_path)
        if resolved is None or not resolved.offline_path:
            return None
        return _usable_file(resolved.offline_path, allow_empty=True)

    def list_visible_mods(
        self,
        library_root: str | Path,
        game_name: str | None = None,
    ) -> list[ResolvedModMetadata]:
        """On-disk mods plus backup-only missing mods."""
        from services.file_ops import ModFileManager

        root = Path(library_root)
        manager = ModFileManager(root)
        folders = manager.list_managed_mods(game_name=game_name)
        seen: set[str] = set()
        out: list[ResolvedModMetadata] = []
        for folder in folders:
            info = read_info_metadata_dict(folder) or {}
            mid = _first_text(
                info.get("published_file_id"),
                folder.name if folder.name.isdigit() else "",
            )
            resolved = self.resolve(mid or None, folder)
            if resolved is None:
                continue
            out.append(resolved)
            if resolved.published_file_id.isdigit():
                seen.add(resolved.published_file_id)

        try:
            from core.db_manager import get_db

            rows = get_db().list_folder_missing_mods(
                game_folder=game_name,
                library_root=root,
            )
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            mid = str(row.get("mod_id") or "").strip()
            if not mid.isdigit() or mid in seen:
                continue
            lkp = str(row.get("last_known_path") or "").strip() or None
            resolved = self.resolve(mid, lkp)
            if resolved is None or resolved.folder_present:
                continue
            out.append(resolved)
            seen.add(mid)
        return out

    def _resolve_identity(
        self,
        mod_id: int | str | None,
        managed_path: str | Path | None,
    ) -> tuple[str, Path | None]:
        path = Path(managed_path) if managed_path is not None else None
        mid = _first_text(mod_id)
        if not mid.isdigit() and path is not None and path.is_dir():
            info = read_info_metadata_dict(path) or {}
            mid = _first_text(
                info.get("published_file_id"),
                path.name if path.name.isdigit() else "",
            )
        if not mid.isdigit() and path is not None and not path.is_dir():
            row = self._sqlite_row_by_path(path)
            if row is not None:
                mid = str(row.get("mod_id") or "")
        if not _folder_exists(path) and mid.isdigit():
            sqlite = self._sqlite_row(mid)
            lkp = str((sqlite or {}).get("last_known_path") or "").strip()
            if lkp and Path(lkp).is_dir():
                path = Path(lkp)
            elif path is None and lkp:
                path = Path(lkp)
        return mid, path

    def _sqlite_row(self, mod_id: str) -> dict[str, Any] | None:
        if not str(mod_id).isdigit():
            return None
        try:
            from core.db_manager import get_db

            return get_db().get_mod_backup_row(mod_id)
        except Exception:  # noqa: BLE001
            return None

    def _sqlite_row_by_path(self, path: Path) -> dict[str, Any] | None:
        try:
            from core.db_manager import get_db

            db = get_db()
            for candidate in (str(path), str(path.resolve())):
                row = db.get_mod_backup_row_by_path(candidate)
                if row is not None:
                    return row
        except Exception:  # noqa: BLE001
            return None
        return None

    def _sqlite_display(self, mod_id: str):
        if not str(mod_id).isdigit():
            return None
        try:
            from core.db_manager import get_db

            return get_db().get_mod_display_info(mod_id)
        except Exception:  # noqa: BLE001
            return None

    def _tags_from_sqlite(self, mod_id: str) -> tuple[list[str], str]:
        if not str(mod_id).isdigit():
            return [], ""
        try:
            from core.db_manager import get_db

            tags = [str(t).strip() for t in get_db().get_category_tags(mod_id) if str(t).strip()]
            return tags, (tags[0] if tags else "")
        except Exception:  # noqa: BLE001
            return [], ""

    def _cover_existing(
        self,
        root: Path,
        info: dict[str, Any],
        backup,
        sqlite: dict[str, Any] | None,
    ) -> Path | None:
        info_dir = root / INFO_DIR_NAME
        found = _find_info_cover(info_dir)
        if found is not None:
            return found
        ref = _first_text(info.get("cover_path"))
        if ref:
            direct = Path(ref)
            if direct.is_file():
                return direct.resolve()
            nested = root / ref
            if nested.is_file():
                return nested.resolve()
        if backup is not None:
            found = _usable_file(backup.cover_path)
            if found is not None:
                return found
        if sqlite is not None:
            return _usable_file(sqlite.get("backup_cover_path"))
        return None

    def _cover_missing(
        self,
        mod_id: str,
        backup,
        sqlite: dict[str, Any] | None,
        display,
    ) -> Path | None:
        if backup is not None:
            found = _usable_file(backup.cover_path)
            if found is not None:
                return found
        if sqlite is not None:
            found = _usable_file(sqlite.get("backup_cover_path"))
            if found is not None:
                return found
        sqlite_cover = str(getattr(display, "cover_path", "") or "").strip()
        found = _usable_file(sqlite_cover)
        if found is not None and INFO_DIR_NAME not in found.parts:
            return found
        return None

    def _offline_existing(self, root: Path, backup) -> Path | None:
        from services.file_ops import LEGACY_INFO_DIR_NAME
        from services.offline.paths import resolve_offline_page as resolve_info_offline

        found = resolve_info_offline(root)
        if found is not None:
            return found.resolve()
        for info_name in (INFO_DIR_NAME, LEGACY_INFO_DIR_NAME):
            for candidate in (
                root / info_name / "offline" / "index.html",
                root / info_name / "index.html",
            ):
                try:
                    if candidate.is_file():
                        return candidate.resolve()
                except OSError:
                    continue
        if backup is not None:
            return _usable_file(backup.offline_path, allow_empty=True)
        return None

    def _offline_missing(self, backup, sqlite: dict[str, Any] | None = None) -> Path | None:
        if backup is not None:
            found = _usable_file(backup.offline_path, allow_empty=True)
            if found is not None:
                return found
        if sqlite is not None:
            return _usable_file(sqlite.get("backup_offline_path"), allow_empty=True)
        return None


def _find_info_cover(info_dir: Path) -> Path | None:
    if not info_dir.is_dir():
        return None
    for pattern in ("cover.*", "preview.*"):
        for candidate in sorted(info_dir.glob(pattern)):
            found = _usable_file(candidate)
            if found is not None:
                return found
    return None


def _dependencies_from_mapping(data: dict[str, Any] | None) -> list[str]:
    raw = (data or {}).get("dependencies") or []
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x or "").strip()]


_RESOLVER = ModMetadataResolver()


def resolve_mod_metadata(
    mod_id: int | str | None = None,
    managed_path: str | Path | None = None,
) -> ResolvedModMetadata | None:
    """Pure-read resolve. Never syncs backup."""
    return _RESOLVER.resolve(mod_id, managed_path)


def resolve_cover_path(
    mod_id: int | str | None = None,
    managed_path: str | Path | None = None,
) -> Path | None:
    return _RESOLVER.resolve_cover_path(mod_id, managed_path)


def resolve_offline_page(
    mod_id: int | str | None = None,
    managed_path: str | Path | None = None,
) -> Path | None:
    return _RESOLVER.resolve_offline_page(mod_id, managed_path)


def list_visible_mods(
    library_root: str | Path,
    game_name: str | None = None,
) -> list[ResolvedModMetadata]:
    return _RESOLVER.list_visible_mods(library_root, game_name)
