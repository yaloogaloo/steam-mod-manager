"""Platform importers for generic Mod Manager."""

from __future__ import annotations

from core.db_manager import DatabaseManager
from services.importers.archive import (
    ArchiveImporter,
    cleanup_import_cache,
    extract_archive,
    find_mod_root,
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
from services.importers.local_scanner import IMAGE_EXTENSIONS, scan_mod_directory
from services.importers.materialize import materialize_imported_mod
from services.importers.nexus import NexusImporter
from services.importers.steam import SteamImporter

__all__ = [
    "ArchiveImporter",
    "GithubImporter",
    "IMAGE_EXTENSIONS",
    "ImportContext",
    "ImportResult",
    "MISSING_GAME_CONTEXT",
    "ModImporter",
    "NexusImporter",
    "SteamImporter",
    "cleanup_image_entries_in_mod_files",
    "cleanup_import_cache",
    "cleanup_mod_files_images",
    "detect_importer",
    "extract_archive",
    "find_mod_root",
    "materialize_imported_mod",
    "scan_mod_directory",
]


def detect_importer(
    value: str, db: DatabaseManager | None = None
) -> ModImporter | None:
    for cls in (SteamImporter, NexusImporter, GithubImporter):
        imp = cls(db=db)
        if imp.detect(value):
            return imp
    return None
