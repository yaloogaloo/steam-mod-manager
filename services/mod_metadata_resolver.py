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


def _pick_canonical_visible_folder(mod_id: str, folders: list[Path]) -> Path:
    """Choose one filesystem observation for a single Internal entity."""
    from services.identity_service import is_empty_mod_placeholder

    if len(folders) == 1:
        return folders[0]
    lkp = ""
    try:
        from core.db_manager import get_db

        row = get_db().get_mod_backup_row(mod_id) or {}
        lkp = str(row.get("last_known_path") or "").strip()
    except Exception:  # noqa: BLE001
        lkp = ""
    ranked: list[tuple[int, int, int, str, Path]] = []
    for folder in folders:
        placeholder = 1 if is_empty_mod_placeholder(folder.name) else 0
        path_match = 0
        if lkp:
            try:
                path_match = 0 if str(folder.resolve()) == str(Path(lkp).resolve()) else 1
            except OSError:
                path_match = 1
        content_missing = 1
        try:
            from services.local_file_index import has_local_mod_payload

            content_missing = 0 if has_local_mod_payload(folder) else 1
        except Exception:  # noqa: BLE001
            try:
                content_missing = 0 if any(folder.iterdir()) else 1
            except OSError:
                content_missing = 1
        ranked.append((placeholder, content_missing, path_match, folder.name.lower(), folder))
    ranked.sort()
    return ranked[0][4]


@dataclass
class ResolvedModMetadata:
    """Display payload after applying .info / backup / SQLite priority.

    ``internal_id`` is ``mods.mod_id``. Never store Workshop ID here.
    """

    internal_id: str
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
    external_id: str = ""

    @property
    def published_file_id(self) -> str:
        """Deprecated alias of ``internal_id`` — do not use for Steam Workshop."""
        return self.internal_id

    def to_mod_metadata(self) -> ModMetadata:
        from services.identity_service import sidecar_published_file_id

        mid = str(self.internal_id or "").strip()
        ext = str(self.external_id or "").strip()
        if not ext and self.platform == PLATFORM_STEAM:
            # Steam workspace_id is often the Workshop ID (display/registration).
            ext = str(self.workspace_id or "").strip()
        pub = sidecar_published_file_id(
            mod_id=mid, platform=self.platform, external_id=ext
        )
        meta = ModMetadata(
            published_file_id=pub,
            internal_id=mid,
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
        mid = _first_text(mod_id)
        if not str(mid).isdigit():
            # Detail / resolver require caller-supplied internal_id — no path invent.
            return None
        # Bound folder must prove the same entity when .info is present.
        proof = _first_text(info.get("internal_id"))
        if proof:
            from services.mod_identity import ensure_mod_identity

            bound, _payload, _changed = ensure_mod_identity(
                root, dict(info), db=None
            )
            if str(bound).isdigit() and str(bound) != str(mid):
                return None

        backup = load_backup(mid) if str(mid).isdigit() else None
        sqlite = self._sqlite_row(mid)
        display = self._sqlite_display(mid)
        entity_app = int((sqlite or {}).get("app_id") or 0)
        if entity_app <= 0:
            entity_app = int(getattr(display, "app_id", 0) or 0)

        from services.metadata_owner_guard import metadata_payload_is_foreign

        info_foreign = metadata_payload_is_foreign(info, entity_app_id=entity_app)
        backup_foreign = bool(
            backup is not None
            and metadata_payload_is_foreign(backup.metadata, entity_app_id=entity_app)
        )
        info_owned = info if not info_foreign else {}
        backup_meta = (
            (backup.metadata if backup is not None else {})
            if not backup_foreign
            else {}
        )

        display_name = _first_text(
            info_owned.get("display_name"),
            getattr(display, "user_display_name", "") if display else "",
            getattr(display, "display_name", "") if display else "",
            info_owned.get("title"),
            backup_meta.get("display_name"),
            backup_meta.get("title"),
            getattr(display, "steam_name", "") if display else "",
        )
        description = _first_text(
            info_owned.get("description"),
            info_owned.get("custom_description"),
            backup_meta.get("description"),
            getattr(display, "custom_description", "") if display else "",
            getattr(display, "steam_description", "") if display else "",
        )
        platform = parse_metadata_platform(info_owned) or _first_text(
            backup_meta.get("source_type"),
            backup_meta.get("platform"),
            getattr(display, "platform", "") if display else "",
            PLATFORM_STEAM,
        )
        source_url = _first_text(
            info_owned.get("url"),
            info_owned.get("source_url"),
            info_owned.get("website"),
            backup_meta.get("url"),
            backup_meta.get("source_url"),
            getattr(display, "source_url", "") if display else "",
        )
        workspace_id = _first_text(
            info_owned.get("workspace_id"),
            backup_meta.get("workspace_id"),
            getattr(display, "workspace_id", "") if display else "",
        )
        external_id = _first_text(
            info_owned.get("external_id"),
            backup_meta.get("external_id"),
            getattr(display, "external_id", "") if display else "",
        )
        cover_backup = None if backup_foreign else backup
        cover = self._cover_existing(root, info_owned, cover_backup, sqlite)
        offline_backup = None if backup_foreign else backup
        offline = self._offline_existing(root, offline_backup)
        deps = _dependencies_from_mapping(info_owned)
        if not deps and backup_meta:
            deps = _dependencies_from_mapping(backup_meta)
        tags, category = self._tags_from_sqlite(mid)
        author = _first_text(
            info_owned.get("author"),
            backup_meta.get("author"),
        )
        game_name = _first_text(
            info_owned.get("game_name"),
            backup_meta.get("game_name"),
            root.parent.name,
        )
        # Entity app_id is authoritative — never adopt foreign payload app_id.
        app_id = entity_app or int(
            info_owned.get("app_id")
            or backup_meta.get("app_id")
            or 0
        )
        return ResolvedModMetadata(
            internal_id=str(mid),
            display_name=display_name,
            description=description,
            platform=normalize_platform(platform),
            source_url=source_url,
            workspace_id=workspace_id,
            external_id=external_id,
            cover_path=str(cover) if cover else "",
            offline_path=str(offline) if offline else "",
            tags=tags,
            dependencies=deps,
            folder_present=True,
            managed_path=str(root),
            author=author,
            category=category,
            title=_first_text(info_owned.get("title"), display_name),
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
        if not mid.isdigit():
            return None
        sqlite = self._sqlite_row(mid)
        backup = load_backup(mid) if mid.isdigit() else None
        if backup is None and sqlite is None:
            return None

        bmeta: dict[str, Any] = dict(backup.metadata) if backup else {}
        display = self._sqlite_display(mid)
        entity_app = int((sqlite or {}).get("app_id") or 0)
        if entity_app <= 0:
            entity_app = int(getattr(display, "app_id", 0) or 0)

        from services.metadata_owner_guard import metadata_payload_is_foreign

        if backup is not None and metadata_payload_is_foreign(
            bmeta, entity_app_id=entity_app
        ):
            bmeta = {}
            backup = None

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
        external_id = _first_text(
            bmeta.get("external_id"),
            getattr(display, "external_id", "") if display else "",
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
        app_id = entity_app or int(bmeta.get("app_id") or 0)
        return ResolvedModMetadata(
            internal_id=str(mid),
            display_name=display_name,
            description=description,
            platform=normalize_platform(platform),
            source_url=source_url,
            workspace_id=workspace_id,
            external_id=external_id,
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
        """Resource locator: prefer on-disk ``.info/cover.*``; never invent entity."""
        if managed_path is not None:
            root = Path(managed_path)
            if root.is_dir():
                found = _find_info_cover(root / INFO_DIR_NAME)
                if found is not None:
                    return found
                info = read_info_metadata_dict(root) or {}
                ref = _first_text(info.get("cover_path"))
                if ref:
                    nested = root / ref
                    if nested.is_file():
                        return nested.resolve()
                    if Path(ref).is_file():
                        return Path(ref).resolve()
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
        """On-disk mods plus backup-only missing mods.

        One Internal entity produces at most one user-facing card. Multiple
        filesystem observations of the same entity are grouped; unresolved
        folders (including Empty Mod placeholders without official identity)
        are not extra cards.
        """
        from core.db_manager import get_db
        from services.file_ops import ModFileManager
        from services.mod_identity import resolve_existing_mod_id

        root = Path(library_root)
        manager = ModFileManager(root)
        folders = manager.list_managed_mods(game_name=game_name)
        try:
            database = get_db()
        except Exception:  # noqa: BLE001
            database = None
        grouped: dict[str, list[Path]] = {}
        for folder in folders:
            info = dict(read_info_metadata_dict(folder) or {})
            info["_managed_path"] = str(folder.resolve())
            info["_folder_name"] = folder.name
            mid = resolve_existing_mod_id(info, db=database)
            if not mid.isdigit():
                continue
            grouped.setdefault(mid, []).append(folder)

        seen: set[str] = set()
        out: list[ResolvedModMetadata] = []
        for mid, paths in grouped.items():
            folder = _pick_canonical_visible_folder(mid, paths)
            resolved = self.resolve(mid, folder)
            if resolved is None:
                continue
            out.append(resolved)
            seen.add(mid)

        try:
            rows = (database or get_db()).list_folder_missing_mods(
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
        """Require caller internal_id. Never invent identity from path/folder/published."""
        path = Path(managed_path) if managed_path is not None else None
        mid = _first_text(mod_id)
        if not mid.isdigit():
            return "", path
        if not _folder_exists(path):
            try:
                from services.path_lifecycle import resolve_managed_folder

                healed = resolve_managed_folder(mid, hint_path=path, db=None)
                if healed.path is not None and healed.path.is_dir():
                    path = healed.path
            except Exception:  # noqa: BLE001
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
