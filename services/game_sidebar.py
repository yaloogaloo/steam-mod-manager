"""Library game sidebar — Database Read Projection only.

ARCHITECTURE RULE
-----------------
``GameSidebarViewModel`` is built from the ``games`` table + ``mods``
aggregation. Filesystem scans (``list_games`` / ``iterdir`` / ``resolve_games``)
belong to Sync/Reconcile — never to Library snapshot / sidebar.

Flow::

    Database → GameSidebarViewModel → UI
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from core.sanitize import sanitize_folder_name
from services.game_status import (
    GameStatusSummary,
    ModStatusHint,
    aggregate_category_status,
    aggregate_from_hints,
)
from services.library_status import (
    CONTENT_CONTENT_MISSING,
    CONTENT_HEALTHY,
    GAME_STATUS_HEALTHY,
    GAME_STATUS_MISSING_FOLDER,
)

logger = logging.getLogger(__name__)


@dataclass
class GameSidebarViewModel:
    """Library sidebar row — DB projection, no filesystem origin."""

    folder: str
    display: str
    app_id: int = 0
    count: int = 0
    categories: list[str] = field(default_factory=list)
    game_status: str = GAME_STATUS_HEALTHY
    status_summary: GameStatusSummary | None = None
    category_summaries: dict[str, GameStatusSummary] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "folder": self.folder,
            "display": self.display,
            "app_id": int(self.app_id),
            "count": int(self.count),
            "categories": list(self.categories),
            "game_status": self.game_status,
            "origin": "games_table",
            "overall_status": (
                self.status_summary.overall_status
                if self.status_summary is not None
                else "healthy"
            ),
            "status_summary": (
                self.status_summary.to_dict() if self.status_summary is not None else None
            ),
        }


def build_game_sidebar_view_models(
    *,
    db: Any | None = None,
    mod_hints: Sequence[ModStatusHint] | None = None,
    mod_counts: dict[str, int] | None = None,
) -> list[GameSidebarViewModel]:
    """
    Build sidebar games from ``games`` + ``mods`` SQL aggregates only.

    Must not call ``ModFileManager.list_games``, ``Path.iterdir``, or
    ``resolve_games`` (those are Sync/Reconcile concerns).
    """
    from core.db_manager import get_db

    database = db if db is not None else get_db()
    hints = list(mod_hints or [])
    counts = dict(mod_counts or {})

    aggregates = database.list_game_sidebar_aggregates()
    entries: dict[str, GameSidebarViewModel] = {}

    for row in aggregates:
        app_id = int(row.get("app_id") or 0)
        name = str(row.get("name") or "").strip()
        folder = str(row.get("folder") or "").strip()
        if not folder:
            folder = sanitize_folder_name(name, fallback=f"App_{app_id}" if app_id else "Unknown")
        display = name or folder
        count = int(row.get("mod_count") or 0)
        if folder in counts:
            count = max(count, int(counts[folder]))
        absent = int(row.get("absent_count") or 0)
        if count > 0 and absent >= count:
            game_status = GAME_STATUS_MISSING_FOLDER
        else:
            game_status = GAME_STATUS_HEALTHY
        categories: list[str] = []
        if app_id > 0:
            try:
                categories = list(database.list_game_categories(app_id))
            except Exception:  # noqa: BLE001
                categories = []
        entries[folder] = GameSidebarViewModel(
            folder=folder,
            display=display,
            app_id=app_id,
            count=count,
            categories=categories,
            game_status=game_status,
        )

    # Hints / counts may surface game folders not yet in games table.
    for key, n in counts.items():
        key = str(key or "").strip()
        if not key:
            continue
        if key in entries:
            entries[key].count = max(int(entries[key].count), int(n))
            continue
        entries[key] = GameSidebarViewModel(
            folder=key,
            display=key,
            app_id=0,
            count=int(n),
            game_status=GAME_STATUS_HEALTHY,
        )

    for key, ent in entries.items():
        summary = aggregate_from_hints(key, hints, game_status=ent.game_status)
        if summary.total_mods == 0 and ent.count > 0:
            summary = aggregate_from_hints(
                key,
                [
                    ModStatusHint(
                        game_folder=key,
                        content_status=(
                            CONTENT_CONTENT_MISSING
                            if ent.game_status == GAME_STATUS_MISSING_FOLDER
                            else CONTENT_HEALTHY
                        ),
                        folder_absent=ent.game_status == GAME_STATUS_MISSING_FOLDER,
                    )
                    for _ in range(int(ent.count))
                ],
                game_status=ent.game_status,
            )
        ent.status_summary = summary
        if summary.total_mods > 0:
            ent.count = max(int(ent.count), int(summary.total_mods))
        cat_map: dict[str, GameStatusSummary] = {}
        for cat in ent.categories:
            cat_map[cat] = aggregate_category_status(cat, hints, game_folder=key)
        ent.category_summaries = cat_map

    return [entries[k] for k in sorted(entries.keys(), key=str.casefold)]
