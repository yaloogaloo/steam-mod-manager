"""Steam Workshop Mod Manager — core package."""

from .db_manager import DatabaseManager, get_db
from .game_info import GameInfo
from .models import ModMetadata
from .paths import (
    database_path,
    default_mod_library,
    extract_app_id_from_workshop_path,
    project_root,
)
from .scanner import WorkshopScanner
from .scraper import WorkshopPageScraper
from .steam_api import SteamWorkshopClient

__all__ = [
    "DatabaseManager",
    "GameInfo",
    "ModMetadata",
    "WorkshopScanner",
    "WorkshopPageScraper",
    "SteamWorkshopClient",
    "database_path",
    "default_mod_library",
    "extract_app_id_from_workshop_path",
    "get_db",
    "project_root",
]
