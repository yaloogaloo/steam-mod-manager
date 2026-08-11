"""Copy Mod folders, persist metadata, and locate cover images."""

from __future__ import annotations

import json
import logging
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any, Mapping

from core.models import ModMetadata
from core.sanitize import sanitize_folder_name, unique_destination

logger = logging.getLogger(__name__)

INFO_DIR_NAME = ".info"
LEGACY_INFO_DIR_NAME = "info"
METADATA_FILENAME = "metadata.json"
LEGACY_METADATA_FILENAME = "mod.json"
COVER_BASENAME = "cover"
LEGACY_COVER_BASENAME = "preview"

_IGNORE_CONTENT_DIRS = frozenset({INFO_DIR_NAME, LEGACY_INFO_DIR_NAME})
_ARCHIVE_SUFFIXES = frozenset({".zip", ".7z", ".rar"})
MISSING_CONTENT_METADATA_KEY = "is_missing_content"


def _zip_has_payload(archive_path: Path) -> bool:
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            return any(
                bool(name) and not str(name).endswith("/") for name in zf.namelist()
            )
    except (OSError, zipfile.BadZipFile):
        # Unreadable zip → do not mark missing (avoid false positives).
        return True


def is_missing_mod_content(managed_path: str | Path) -> bool:
    """
    True when the managed folder has no deployable payload outside ``.info`` / ``info``.

    Empty folders, metadata-only stubs, and trees that only contain empty ``.zip``
    archives are treated as missing content. Unreadable non-zip archives are
    treated as having content (conservative).
    """
    root = Path(managed_path)
    if not root.is_dir():
        return True
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                parts = path.relative_to(root).parts
            except ValueError:
                continue
            if parts and parts[0] in _IGNORE_CONTENT_DIRS:
                continue
            suffix = path.suffix.lower()
            if suffix in _ARCHIVE_SUFFIXES:
                if suffix == ".zip" and not _zip_has_payload(path):
                    continue
                return False
            return False
    except OSError:
        return True
    return True


def set_is_missing_content(managed_path: str | Path, missing: bool) -> None:
    """Persist ``is_missing_content`` into ``.info/metadata.json``."""
    root = Path(managed_path)
    if not root.is_dir():
        return
    data = read_info_metadata_dict(root) or {}
    data[MISSING_CONTENT_METADATA_KEY] = bool(missing)
    persist_unified_metadata_dict(root, data)


def apply_missing_content_marker(managed_path: str | Path) -> bool:
    """Detect empty payload, write ``is_missing_content``, return the flag."""
    missing = is_missing_mod_content(managed_path)
    set_is_missing_content(managed_path, missing)
    return missing


def clear_missing_content_if_present(managed_path: str | Path) -> bool:
    """
    If the managed folder now has payload files, persist ``is_missing_content=False``.

    Returns True when the flag was cleared. Does not mark empty folders as missing.
    """
    if is_missing_mod_content(managed_path):
        return False
    set_is_missing_content(managed_path, False)
    return True


def read_is_missing_content(managed_path: str | Path) -> bool:
    """True when metadata flag or filesystem payload check says content is missing."""
    data = read_info_metadata_dict(managed_path) or {}
    if data.get(MISSING_CONTENT_METADATA_KEY) is True:
        return True
    return is_missing_mod_content(managed_path)


class ModFileManager:
    """
    Filesystem helpers for the managed library::

        <target_root>/
            <Game English Name>/
                <Mod Title>/
                    .info/metadata.json
                    .info/index.html
                    …
    """

    def __init__(self, target_root: str | Path) -> None:
        self.target_root = Path(target_root).expanduser().resolve()

    def ensure_target_root(self) -> Path:
        self.target_root.mkdir(parents=True, exist_ok=True)
        return self.target_root

    def info_dir(self, managed_path: Path) -> Path:
        """
        Resolve the metadata directory for reading.

        Prefers ``.info``; falls back to legacy ``info`` when present so old
        libraries keep working.
        """
        managed = Path(managed_path)
        modern = managed / INFO_DIR_NAME
        if modern.is_dir():
            return modern
        legacy = managed / LEGACY_INFO_DIR_NAME
        if legacy.is_dir():
            return legacy
        return modern

    def info_dir_for_write(self, managed_path: Path) -> Path:
        """Always use ``.info`` for new writes."""
        return Path(managed_path) / INFO_DIR_NAME

    def metadata_path(self, managed_path: Path) -> Path:
        """Canonical write path: ``.info/metadata.json``."""
        return self.info_dir_for_write(managed_path) / METADATA_FILENAME

    @staticmethod
    def _metadata_read_candidates(info_dir: Path) -> list[Path]:
        """Prefer ``metadata.json``; fall back to legacy ``mod.json``."""
        return [
            info_dir / METADATA_FILENAME,
            info_dir / LEGACY_METADATA_FILENAME,
        ]

    def game_folder_name(self, metadata: ModMetadata) -> str:
        """Sanitized English game folder under the library root."""
        raw = metadata.game_name or metadata.game_display_name
        fallback = f"App_{metadata.app_id}" if metadata.app_id else "Unknown Game"
        name = sanitize_folder_name(raw, fallback=fallback)
        # Never keep a bare numeric game folder if we can help it
        if name.isdigit():
            name = sanitize_folder_name(
                f"App_{metadata.app_id or name}",
                fallback=f"App_{metadata.app_id or name}",
            )
        return name

    def mod_folder_name(self, metadata: ModMetadata) -> str:
        """
        Sanitized Mod folder name from the real title.

        Never returns a pure numeric ID. Missing titles become
        ``Unknown_Mod_<id>``.
        """
        fallback = f"Unknown_Mod_{metadata.published_file_id}"
        raw = metadata.effective_title()
        name = sanitize_folder_name(raw, fallback=fallback)
        if name.isdigit() or name == metadata.published_file_id:
            name = sanitize_folder_name(fallback, fallback=fallback)
        return name

    def allocate_destination(self, metadata: ModMetadata) -> Path:
        """
        Choose ``<target>/<Game>/<ModTitle>/`` (unique). Does not create it yet.
        """
        self.ensure_target_root()
        game_dir = self.target_root / self.game_folder_name(metadata)
        game_dir.mkdir(parents=True, exist_ok=True)

        safe_mod = self.mod_folder_name(metadata)
        return unique_destination(
            game_dir,
            safe_mod,
            published_file_id=metadata.published_file_id,
        )

    def migrate_numeric_mod_folders(self) -> list[tuple[Path, Path]]:
        """
        Rename legacy ``…/<Game>/<digits>/`` folders to real Mod titles.

        Uses ``.info/metadata.json`` title first, then SQLite snapshot. Returns
        list of ``(old_path, new_path)`` renames performed.
        """
        from core.db_manager import get_db

        renames: list[tuple[Path, Path]] = []
        db = get_db()

        for folder in list(self.list_managed_mods()):
            if not folder.name.isdigit():
                continue

            meta = self.load_metadata(folder)
            title = (meta.title if meta else "") or ""
            pub_id = (
                meta.published_file_id
                if meta and meta.published_file_id
                else folder.name
            )

            if not title.strip() or title.strip().isdigit():
                db_meta = db.get_mod(pub_id)
                if db_meta and db_meta.title.strip() and not db_meta.title.strip().isdigit():
                    title = db_meta.title.strip()
                    if meta is None:
                        meta = ModMetadata(published_file_id=str(pub_id), title=title)
                    else:
                        meta.title = title
                        if db_meta.description and not meta.description:
                            meta.description = db_meta.description
                        if db_meta.preview_url and not meta.preview_url:
                            meta.preview_url = db_meta.preview_url
                        if db_meta.app_id and not meta.app_id:
                            meta.app_id = db_meta.app_id

            if not title.strip() or title.strip().isdigit():
                logger.info(
                    "Skip numeric folder rename (no title): %s", folder
                )
                continue

            if meta is None:
                meta = ModMetadata(published_file_id=str(pub_id), title=title)
            else:
                meta.title = title

            # Keep game folder; only rename the Mod leaf
            desired_name = self.mod_folder_name(meta)
            if desired_name == folder.name:
                continue

            target = unique_destination(
                folder.parent,
                desired_name,
                published_file_id=str(pub_id),
            )
            # unique_destination skips existing paths — if it picked the
            # current numeric folder somehow, bail
            if target.resolve() == folder.resolve():
                continue

            try:
                folder.rename(target)
            except OSError as exc:
                logger.warning("Failed to rename %s -> %s: %s", folder, target, exc)
                continue

            meta.managed_path = str(target)
            try:
                self.save_metadata(meta, target)
            except OSError as exc:
                logger.warning("Renamed but failed to update metadata.json: %s", exc)

            logger.info("Renamed numeric mod folder: %s -> %s", folder.name, target.name)
            renames.append((folder, target))

        return renames

    def enrich_title_from_db(self, metadata: ModMetadata) -> ModMetadata:
        """Fill empty / numeric titles from the SQLite snapshot when possible."""
        title = (metadata.title or "").strip()
        if title and not title.isdigit():
            return metadata
        try:
            from core.db_manager import get_db

            cached = get_db().get_mod(metadata.published_file_id)
        except Exception:  # noqa: BLE001
            return metadata
        if cached and cached.title.strip() and not cached.title.strip().isdigit():
            metadata.title = cached.title.strip()
            if cached.description and not metadata.description:
                metadata.description = cached.description
            if cached.preview_url and not metadata.preview_url:
                metadata.preview_url = cached.preview_url
            if cached.app_id and not metadata.app_id:
                metadata.app_id = cached.app_id
        return metadata

    def copy_mod(
        self,
        metadata: ModMetadata,
        *,
        overwrite_existing: bool = False,
        destination: Path | None = None,
        ignore_files: list[Path] | tuple[Path, ...] | None = None,
    ) -> Path:
        """
        Copy the source Mod folder into the target library.

        *ignore_files* are excluded from the copy (e.g. cover images / ``.mhtml``
        that belong in ``.info`` only). Returns the managed destination path.
        Updates ``metadata.managed_path``.
        """
        if not metadata.source_path:
            raise ValueError(
                f"Mod {metadata.published_file_id} has no source_path"
            )

        source = Path(metadata.source_path)
        if not source.is_dir():
            raise FileNotFoundError(f"Source Mod folder not found: {source}")

        dest = Path(destination) if destination else self.allocate_destination(metadata)

        if dest.exists():
            if not overwrite_existing:
                metadata.managed_path = str(dest)
                logger.info("Destination already exists, reusing: %s", dest)
                return dest
            shutil.rmtree(dest)

        dest.parent.mkdir(parents=True, exist_ok=True)
        ignore_resolved: set[Path] = set()
        for raw in ignore_files or ():
            try:
                ignore_resolved.add(Path(raw).resolve())
            except OSError:
                ignore_resolved.add(Path(raw))

        def _ignore(directory: str, names: list[str]) -> set[str]:
            if not ignore_resolved:
                return set()
            base = Path(directory)
            skipped: set[str] = set()
            for name in names:
                candidate = base / name
                try:
                    resolved = candidate.resolve()
                except OSError:
                    resolved = candidate
                if resolved in ignore_resolved:
                    skipped.add(name)
            return skipped

        shutil.copytree(source, dest, ignore=_ignore if ignore_resolved else None)
        metadata.managed_path = str(dest)
        logger.info(
            "Copied mod %s -> %s",
            metadata.published_file_id,
            dest,
        )
        return dest

    def ensure_info_dir(self, managed_path: Path) -> Path:
        info = self.info_dir_for_write(managed_path)
        info.mkdir(parents=True, exist_ok=True)
        return info

    def save_metadata(self, metadata: ModMetadata, managed_path: Path | None = None) -> Path:
        """Write unified ``.info/metadata.json`` and remove legacy ``mod.json``."""
        path = Path(managed_path or metadata.managed_path or "")
        if not path:
            raise ValueError("managed_path is required to save metadata")

        existing = read_info_metadata_dict(path) or {}
        info = self.ensure_info_dir(path)
        payload = _build_unified_payload(metadata)
        merged = dict(existing)
        for key, value in payload.items():
            if key == "file_roles":
                if value:
                    merged["file_roles"] = value
                continue
            if value in (None, "", {}, []):
                continue
            merged[key] = value
        return _write_unified_metadata(info, merged)

    def load_metadata(self, managed_path: Path) -> ModMetadata | None:
        data = read_info_metadata_dict(managed_path)
        if not data:
            return None
        meta = _metadata_from_dict(data, managed_path)
        _backfill_mod_runtime_paths(meta, managed_path)
        return meta

    def find_local_cover(self, managed_path: Path) -> Path | None:
        """
        Resolve an installed cover under ``.info/`` only.

        Does **not** scan Mod package images (icon / screenshot / texture).
        Looks for ``cover.*`` then legacy ``preview.*``.
        """
        root = Path(managed_path)
        info = self.info_dir(root)

        for basename in (COVER_BASENAME, LEGACY_COVER_BASENAME):
            for ext in (".jpg", ".jpeg", ".jfif", ".png", ".webp", ".gif"):
                candidate = info / f"{basename}{ext}"
                if candidate.is_file():
                    return candidate
        return None

    def list_games(self) -> list[str]:
        """Sorted game folder names under the library root (excludes legacy flat mods)."""
        if not self.target_root.is_dir():
            return []
        from services.importers.importer_base import is_invalid_game_name

        names: list[str] = []
        for p in self.target_root.iterdir():
            if not p.is_dir():
                continue
            if self._is_legacy_flat_mod(p):
                continue
            if is_invalid_game_name(p.name):
                continue
            names.append(p.name)
        return sorted(names, key=str.lower)

    def list_managed_mods(self, game_name: str | None = None) -> list[Path]:
        """
        List Mod folders using the two-level layout.

        If *game_name* is set, only that game's mods are returned.
        Also tolerates a legacy flat layout (mod folders directly under root).
        """
        if not self.target_root.is_dir():
            return []

        if game_name:
            game_dir = self.target_root / game_name
            if not game_dir.is_dir():
                return []
            return self._list_mod_dirs(game_dir)

        mods: list[Path] = []
        for entry in sorted(self.target_root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir():
                continue
            if self._is_legacy_flat_mod(entry):
                mods.append(entry)
                continue
            mods.extend(self._list_mod_dirs(entry))
        return mods

    def find_by_published_id(self, published_file_id: str) -> Path | None:
        """Locate an already-managed Mod by ID stored in ``.info/metadata.json``."""
        for folder in self.list_managed_mods():
            meta = self.load_metadata(folder)
            if meta and meta.published_file_id == str(published_file_id):
                return folder
            if folder.name == str(published_file_id):
                return folder
        return None

    def game_name_for_path(self, managed_path: Path) -> str:
        """Infer game folder name from ``…/<game>/<mod>`` layout."""
        managed_path = Path(managed_path)
        try:
            relative = managed_path.resolve().relative_to(self.target_root)
        except ValueError:
            return managed_path.parent.name
        parts = relative.parts
        if len(parts) >= 2:
            return parts[0]
        meta = self.load_metadata(managed_path)
        if meta and meta.game_name:
            return meta.game_name
        return "Unknown Game"

    def _list_mod_dirs(self, game_dir: Path) -> list[Path]:
        try:
            return sorted(
                (p for p in game_dir.iterdir() if p.is_dir()),
                key=lambda p: p.name.lower(),
            )
        except PermissionError:
            return []

    @staticmethod
    def _is_legacy_flat_mod(path: Path) -> bool:
        """True when a library-root child is itself a mod (old flat layout)."""
        for info_name in (INFO_DIR_NAME, LEGACY_INFO_DIR_NAME):
            info = path / info_name
            if (info / METADATA_FILENAME).is_file():
                return True
            if (info / LEGACY_METADATA_FILENAME).is_file():
                return True
        return False


def read_info_metadata_dict(managed_path: str | Path) -> dict[str, Any] | None:
    """
    Read ``.info/metadata.json``; fall back to legacy ``mod.json``.

    Returns the parsed dict or ``None``.
    """
    root = Path(managed_path)
    modern = root / INFO_DIR_NAME
    legacy = root / LEGACY_INFO_DIR_NAME
    info = modern if modern.is_dir() else (legacy if legacy.is_dir() else modern)
    for candidate in ModFileManager._metadata_read_candidates(info):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read metadata %s: %s", candidate, exc)
            continue
        if isinstance(data, dict):
            return data
    return None


def _remove_legacy_mod_json(info_dir: Path) -> None:
    legacy = info_dir / LEGACY_METADATA_FILENAME
    if legacy.is_file():
        try:
            os.remove(legacy)
        except OSError as exc:
            logger.warning("Failed to remove legacy mod.json %s: %s", legacy, exc)


def _write_unified_metadata(info_dir: Path, payload: Mapping[str, Any]) -> Path:
    info_dir.mkdir(parents=True, exist_ok=True)
    meta_file = info_dir / METADATA_FILENAME
    meta_file.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _remove_legacy_mod_json(info_dir)
    return meta_file


def _backfill_mod_runtime_paths(metadata: ModMetadata, managed_path: Path) -> None:
    """Restore filesystem paths that are never authoritative in ``metadata.json``."""
    path_str = str(Path(managed_path).expanduser().resolve())
    metadata.managed_path = path_str
    metadata.local_path = path_str


def _read_metadata_url(data: Mapping[str, Any]) -> str:
    return str(
        data.get("url")
        or data.get("source_url")
        or data.get("website")
        or ""
    ).strip()


def _read_metadata_offline_page(data: Mapping[str, Any]) -> str:
    return str(
        data.get("offline_page_path")
        or data.get("offline_page")
        or ""
    ).strip()


def _build_unified_payload(metadata: ModMetadata) -> dict[str, Any]:
    """Merge ModMetadata with optional DB snapshot fields for portable sidecar."""
    payload: dict[str, Any] = dict(metadata.to_dict())
    url = str(getattr(metadata, "url", "") or "").strip()
    offline = _read_metadata_offline_page(payload) or str(
        metadata.offline_page_path or ""
    ).strip()
    if url:
        payload["url"] = url
    if offline:
        payload["offline_page_path"] = offline
        payload["offline_page"] = offline
    mid = str(metadata.published_file_id or "").strip()
    if mid.isdigit():
        try:
            from core.db_manager import get_db
            from services.info_sidecar import build_sidecar_from_db

            sidecar = build_sidecar_from_db(
                mid,
                metadata.managed_path,
                db=get_db(),
            )
            extra = sidecar.to_dict()
            for key, value in extra.items():
                if key == "file_roles":
                    if value:
                        payload["file_roles"] = value
                    continue
                if key in ("url", "offline_page_path", "offline_page"):
                    continue
                if value not in (None, "", {}, []):
                    payload[key] = value
            if sidecar.url and not url:
                payload["url"] = sidecar.url
            sidecar_offline = str(sidecar.offline_page_path or "").strip()
            if sidecar_offline and not offline:
                payload["offline_page_path"] = sidecar_offline
                payload["offline_page"] = sidecar_offline
            if sidecar.display_name and not str(payload.get("title") or "").strip():
                payload["title"] = sidecar.display_name
            if sidecar.description and not str(payload.get("description") or "").strip():
                payload["description"] = sidecar.description
        except Exception:  # noqa: BLE001
            pass
    return payload


def persist_unified_metadata_dict(
    managed_path: str | Path,
    payload: Mapping[str, Any],
) -> Path:
    """Write ``metadata.json`` at *managed_path* and delete legacy ``mod.json``."""
    root = Path(managed_path)
    info = root / INFO_DIR_NAME
    return _write_unified_metadata(info, payload)
def _metadata_from_dict(data: dict, managed_path: Path) -> ModMetadata:
    from core.mod_platform import parse_metadata_platform

    path_str = str(Path(managed_path).expanduser().resolve())
    title = str(data.get("title") or "")
    display_name = str(data.get("display_name") or "").strip()
    if not title.strip():
        title = display_name
    description = str(
        data.get("description") or data.get("custom_description") or ""
    )
    offline = _read_metadata_offline_page(data)
    meta = ModMetadata(
        published_file_id=str(data.get("published_file_id", "")),
        title=title,
        description=description,
        preview_url=str(data.get("preview_url") or ""),
        file_size=int(data.get("file_size") or 0),
        time_created=int(data.get("time_created") or 0),
        time_updated=int(data.get("time_updated") or 0),
        creator_steam_id=str(data.get("creator_steam_id") or ""),
        app_id=int(data.get("app_id") or 0),
        game_name=str(data.get("game_name") or ""),
        tags=list(data.get("tags") or []),
        source_path=data.get("source_path"),
        managed_path=path_str,
        local_path=path_str,
        url=_read_metadata_url(data),
        cover_path=data.get("cover_path"),
        offline_page_path=offline or None,
        fetch_error=data.get("fetch_error"),
        custom_notes=str(data.get("custom_notes") or ""),
        author=str(data.get("author") or "").strip(),
        source_type=parse_metadata_platform(data),
        json_display_name=display_name,
    )
    return meta
