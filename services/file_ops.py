"""Copy Mod folders, persist metadata, and locate cover images."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from core.models import ModMetadata
from core.sanitize import sanitize_folder_name, unique_destination

logger = logging.getLogger(__name__)

INFO_DIR_NAME = ".info"
LEGACY_INFO_DIR_NAME = "info"
METADATA_FILENAME = "mod.json"
COVER_BASENAME = "preview"

# Common cover / preview filenames found inside Workshop downloads
_LOCAL_COVER_CANDIDATES = (
    "preview.png",
    "preview.jpg",
    "preview.jpeg",
    "preview.webp",
    "headerimage.png",
    "headerimage.jpg",
    "thumb.png",
    "thumb.jpg",
    "thumbnail.png",
    "thumbnail.jpg",
    "icon.png",
    "icon.jpg",
)


class ModFileManager:
    """
    Filesystem helpers for the managed library::

        <target_root>/
            <Game English Name>/
                <Mod Title>/
                    .info/mod.json
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
        return self.info_dir(managed_path) / METADATA_FILENAME

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

        Uses ``.info/mod.json`` title first, then SQLite snapshot. Returns
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
                logger.warning("Renamed but failed to update mod.json: %s", exc)

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
    ) -> Path:
        """
        Copy the source Mod folder into the target library.

        Returns the managed destination path. Updates
        ``metadata.managed_path``.
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
        shutil.copytree(source, dest)
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
        """Write ``.info/mod.json`` next to the managed Mod."""
        path = Path(managed_path or metadata.managed_path or "")
        if not path:
            raise ValueError("managed_path is required to save metadata")

        info = self.ensure_info_dir(path)
        meta_file = info / METADATA_FILENAME
        payload = metadata.to_dict()
        meta_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return meta_file

    def load_metadata(self, managed_path: Path) -> ModMetadata | None:
        meta_file = self.metadata_path(managed_path)
        if not meta_file.is_file():
            return None
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read metadata %s: %s", meta_file, exc)
            return None
        return _metadata_from_dict(data, managed_path)

    def find_local_cover(self, managed_path: Path) -> Path | None:
        """Search the managed Mod tree for a likely cover / preview image."""
        root = Path(managed_path)
        info = self.info_dir(root)

        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            candidate = info / f"{COVER_BASENAME}{ext}"
            if candidate.is_file():
                return candidate

        for name in _LOCAL_COVER_CANDIDATES:
            direct = root / name
            if direct.is_file():
                return direct

        try:
            for child in root.iterdir():
                if child.is_file() and child.name.lower() in {
                    n.lower() for n in _LOCAL_COVER_CANDIDATES
                }:
                    return child
                if not child.is_dir() or child.name in {
                    INFO_DIR_NAME,
                    LEGACY_INFO_DIR_NAME,
                }:
                    continue
                for nested in child.iterdir():
                    if nested.is_file() and nested.name.lower() in {
                        n.lower() for n in _LOCAL_COVER_CANDIDATES
                    }:
                        return nested
        except PermissionError:
            return None
        return None

    def list_games(self) -> list[str]:
        """Sorted game folder names under the library root (excludes legacy flat mods)."""
        if not self.target_root.is_dir():
            return []
        names: list[str] = []
        for p in self.target_root.iterdir():
            if not p.is_dir():
                continue
            if self._is_legacy_flat_mod(p):
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
        """Locate an already-managed Mod by ID stored in ``.info/mod.json``."""
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
        return (
            (path / INFO_DIR_NAME / METADATA_FILENAME).is_file()
            or (path / LEGACY_INFO_DIR_NAME / METADATA_FILENAME).is_file()
        )


def _metadata_from_dict(data: dict, managed_path: Path) -> ModMetadata:
    return ModMetadata(
        published_file_id=str(data.get("published_file_id", "")),
        title=str(data.get("title") or ""),
        description=str(data.get("description") or ""),
        preview_url=str(data.get("preview_url") or ""),
        file_size=int(data.get("file_size") or 0),
        time_created=int(data.get("time_created") or 0),
        time_updated=int(data.get("time_updated") or 0),
        creator_steam_id=str(data.get("creator_steam_id") or ""),
        app_id=int(data.get("app_id") or 0),
        game_name=str(data.get("game_name") or ""),
        tags=list(data.get("tags") or []),
        source_path=data.get("source_path"),
        managed_path=str(managed_path),
        cover_path=data.get("cover_path"),
        offline_page_path=data.get("offline_page_path"),
        fetch_error=data.get("fetch_error"),
        custom_notes=str(data.get("custom_notes") or ""),
    )
