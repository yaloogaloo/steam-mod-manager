"""Platform importers for generic Mod Manager."""

from __future__ import annotations

from core.db_manager import DatabaseManager
from services.importers.archive import (
    ArchiveImporter,
    cleanup_import_cache,
    extract_archive,
    find_mod_root,
)
from services.importers.directory_batch import (
    discover_mod_directories,
    extract_directory_sidecars,
)
from services.importers.github import GithubImporter
from services.importers.importer_base import (
    ImportContext,
    ImportResult,
    MISSING_GAME_CONTEXT,
    ModImporter,
)
from services.importers.cleanup import (
    cleanup_image_entries_in_mod_files,
    cleanup_mod_files_images,
)
from services.importers.image_picker import (
    cleanup_old_auto_cover,
    suggest_sibling_covers,
)
from services.importers.local_scanner import IMAGE_EXTENSIONS, scan_mod_directory
from services.importers.materialize import materialize_imported_mod
from services.importers.modio import ModioImporter
from services.importers.nexus import NexusImporter
from services.importers.other import OtherImporter
from services.importers.steam import SteamImporter

__all__ = [
    "ArchiveImporter",
    "GithubImporter",
    "IMAGE_EXTENSIONS",
    "ImportContext",
    "ImportResult",
    "MISSING_GAME_CONTEXT",
    "ModImporter",
    "ModioImporter",
    "NexusImporter",
    "OtherImporter",
    "SteamImporter",
    "cleanup_image_entries_in_mod_files",
    "cleanup_import_cache",
    "cleanup_mod_files_images",
    "cleanup_old_auto_cover",
    "detect_importer",
    "discover_mod_directories",
    "extract_archive",
    "extract_directory_sidecars",
    "find_mod_root",
    "materialize_imported_mod",
    "scan_mod_directory",
    "suggest_sibling_covers",
]


def detect_importer(
    value: str, db: DatabaseManager | None = None
) -> ModImporter | None:
    for cls in (
        SteamImporter,
        NexusImporter,
        GithubImporter,
        ModioImporter,
        OtherImporter,
    ):
        imp = cls(db=db)
        if imp.detect(value):
            return imp
    return None
