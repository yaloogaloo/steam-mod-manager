"""Importer ABC + result / context types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from core.db_manager import DatabaseManager, ModDisplayInfo, get_db

MISSING_GAME_CONTEXT = "Missing game context"

# Folder / display names that must never become a library game.
INVALID_GAME_NAMES = frozenset(
    {
        "github",
        "nexus",
        "nexus mods",
        "steam",
        "steam workshop",
        "imported",
        "archive",
        "unknown game",
        "unknown",
    }
)


def is_invalid_game_name(name: str | None) -> bool:
    text = str(name or "").strip().casefold()
    return (not text) or text in INVALID_GAME_NAMES


@dataclass
class ImportContext:
    """Current library game selection — required for Nexus / GitHub imports."""

    game_id: int = 0
    game_name: str = ""
    offline_html_path: str | None = None

    def normalized(self) -> ImportContext:
        html = str(self.offline_html_path or "").strip() or None
        return ImportContext(
            game_id=int(self.game_id or 0),
            game_name=str(self.game_name or "").strip(),
            offline_html_path=html,
        )

    def is_complete(self) -> bool:
        ctx = self.normalized()
        return (
            ctx.game_id > 0
            and bool(ctx.game_name)
            and not is_invalid_game_name(ctx.game_name)
        )

    def as_dict(self) -> dict[str, Any]:
        ctx = self.normalized()
        out: dict[str, Any] = {"game_id": ctx.game_id, "game_name": ctx.game_name}
        if ctx.offline_html_path:
            out["offline_html_path"] = ctx.offline_html_path
        return out


@dataclass
class ImportResult:
    success: bool
    mod_id: str = ""
    platform: str = ""
    external_id: str = ""
    source_url: str = ""
    title: str = ""
    error: str = ""
    # "" | "duplicate" — duplicate is a skip, not a hard failure.
    status: str = ""
    display: ModDisplayInfo | None = None
    files_count: int = 0
    managed_path: str = ""
    game_id: int = 0
    game_name: str = ""
    # Multi-directory batch summary (0 / 1 = single-mod import).
    imported_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0

    @property
    def is_duplicate(self) -> bool:
        return str(self.status or "").strip() == "duplicate"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "success": self.success,
            "mod_id": self.mod_id,
            "platform": self.platform,
            "external_id": self.external_id,
            "source_url": self.source_url,
            "title": self.title,
            "status": self.status,
            "files_count": self.files_count,
            "managed_path": self.managed_path,
            "game_id": self.game_id,
            "game_name": self.game_name,
            "imported_count": self.imported_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
        }
        if self.error:
            out["error"] = self.error
        return out


def coerce_import_context(
    context: ImportContext | dict[str, Any] | None = None,
    *,
    game_id: int = 0,
    game_name: str = "",
    app_id: int = 0,
    offline_html_path: str | None = None,
) -> ImportContext | None:
    """Build an :class:`ImportContext` from a dict / kwargs, or ``None``."""
    html_from_kw = str(offline_html_path or "").strip() or None
    if isinstance(context, ImportContext):
        ctx = context.normalized()
        if html_from_kw and not ctx.offline_html_path:
            ctx = ImportContext(
                game_id=ctx.game_id,
                game_name=ctx.game_name,
                offline_html_path=html_from_kw,
            ).normalized()
        if ctx.game_id or ctx.game_name or ctx.offline_html_path:
            return ctx
        context = None
    if isinstance(context, dict):
        gid = int(context.get("game_id") or context.get("app_id") or 0)
        gname = str(context.get("game_name") or "").strip()
        html = (
            str(context.get("offline_html_path") or "").strip()
            or html_from_kw
            or None
        )
        if gid or gname or html:
            return ImportContext(
                game_id=gid, game_name=gname, offline_html_path=html
            ).normalized()
    gid = int(game_id or app_id or 0)
    gname = str(game_name or "").strip()
    if gid or gname or html_from_kw:
        return ImportContext(
            game_id=gid, game_name=gname, offline_html_path=html_from_kw
        ).normalized()
    return None


def require_import_context(
    context: ImportContext | dict[str, Any] | None = None,
    *,
    game_id: int = 0,
    game_name: str = "",
    app_id: int = 0,
) -> ImportContext | ImportResult:
    """Return a complete context, or an error :class:`ImportResult`."""
    ctx = coerce_import_context(
        context, game_id=game_id, game_name=game_name, app_id=app_id
    )
    if ctx is None or not ctx.is_complete():
        return ImportResult(success=False, error=MISSING_GAME_CONTEXT)
    return ctx


def resolve_game_for_import(
    *,
    context: ImportContext | None,
    game_name: str = "",
    app_id: int = 0,
    require_context: bool = True,
) -> tuple[int, str] | ImportResult:
    """
    Resolve ``(game_id, game_name)`` for materialize / DB.

    Invalid platform-as-game names are discarded in favour of *context*.
    """
    preferred = str(game_name or "").strip()
    if is_invalid_game_name(preferred):
        preferred = ""
    ctx = context.normalized() if context is not None else None
    gid = int(app_id or 0) or (ctx.game_id if ctx else 0)
    name = preferred or ((ctx.game_name if ctx else "") or "")
    if is_invalid_game_name(name):
        name = ""
    if require_context:
        if gid <= 0 or not name:
            return ImportResult(success=False, error=MISSING_GAME_CONTEXT)
        return gid, name
    # Soft path (Steam): prefer context, else keep caller game_name even if
    # historically "Steam Workshop".
    if not name:
        name = str(game_name or "").strip() or (
            (ctx.game_name if ctx else "") or "Steam Workshop"
        )
    return gid, name


class ModImporter(ABC):
    """Detect + import one Mod from an external platform into SQLite."""

    platform: str = ""

    def __init__(self, db: DatabaseManager | None = None) -> None:
        self._db = db

    def _database(self) -> DatabaseManager:
        return self._db if self._db is not None else get_db()

    @abstractmethod
    def detect(self, value: str) -> bool:
        """True when *value* looks like this importer's input."""

    @abstractmethod
    def import_mod(self, **kwargs: Any) -> ImportResult:
        """Import / register the Mod."""
