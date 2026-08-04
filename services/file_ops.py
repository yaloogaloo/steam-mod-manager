"""Copy Mod folders, persist metadata, and locate cover images."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

from core.models import ModMetadata

from .sanitize import sanitize_folder_name, unique_destination

logger = logging.getLogger(__name__)

INFO_DIR_NAME = "info"
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
    """Filesystem helpers for managing backed-up Workshop mods."""

    def __init__(self, target_root: str | Path) -> None:
        self.target_root = Path(target_root).expanduser().resolve()

    def ensure_target_root(self) -> Path:
        self.target_root.mkdir(parents=True, exist_ok=True)
        return self.target_root

    def info_dir(self, managed_path: Path) -> Path:
        return Path(managed_path) / INFO_DIR_NAME

    def metadata_path(self, managed_path: Path) -> Path:
        return self.info_dir(managed_path) / METADATA_FILENAME

    def allocate_destination(self, metadata: ModMetadata) -> Path:
        """
        Choose a unique destination folder named after the Mod title
        (sanitized). Does not create the folder yet.
        """
        self.ensure_target_root()
        safe_name = sanitize_folder_name(
            metadata.display_name,
            fallback=metadata.published_file_id,
        )
        return unique_destination(
            self.target_root,
            safe_name,
            published_file_id=metadata.published_file_id,
        )

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
                # Reuse existing managed folder (e.g. re-sync metadata only)
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
        info = self.info_dir(managed_path)
        info.mkdir(parents=True, exist_ok=True)
        return info

    def save_metadata(self, metadata: ModMetadata, managed_path: Path | None = None) -> Path:
        """Write ``info/mod.json`` next to the managed Mod."""
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

        # Prefer covers we already saved into info/
        for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            candidate = info / f"{COVER_BASENAME}{ext}"
            if candidate.is_file():
                return candidate

        for name in _LOCAL_COVER_CANDIDATES:
            direct = root / name
            if direct.is_file():
                return direct

        # Shallow search (depth 2) for common preview names
        try:
            for child in root.iterdir():
                if child.is_file() and child.name.lower() in {
                    n.lower() for n in _LOCAL_COVER_CANDIDATES
                }:
                    return child
                if not child.is_dir() or child.name == INFO_DIR_NAME:
                    continue
                for nested in child.iterdir():
                    if nested.is_file() and nested.name.lower() in {
                        n.lower() for n in _LOCAL_COVER_CANDIDATES
                    }:
                        return nested
        except PermissionError:
            return None
        return None

    def list_managed_mods(self) -> list[Path]:
        """List immediate child directories under the target library."""
        if not self.target_root.is_dir():
            return []
        return sorted(
            (p for p in self.target_root.iterdir() if p.is_dir()),
            key=lambda p: p.name.lower(),
        )

    def find_by_published_id(self, published_file_id: str) -> Path | None:
        """Locate an already-managed Mod by ID stored in ``info/mod.json``."""
        for folder in self.list_managed_mods():
            meta = self.load_metadata(folder)
            if meta and meta.published_file_id == str(published_file_id):
                return folder
            # Also accept folders named exactly as the numeric ID
            if folder.name == str(published_file_id):
                return folder
        return None


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
        tags=list(data.get("tags") or []),
        source_path=data.get("source_path"),
        managed_path=str(managed_path),
        cover_path=data.get("cover_path"),
        offline_page_path=data.get("offline_page_path"),
        fetch_error=data.get("fetch_error"),
    )
