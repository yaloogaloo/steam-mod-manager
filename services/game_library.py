"""Unified Game Library resolution (Phase 6 Part 3 + 6-C status).

Priority (highest first):

1. Real on-disk game folders under the Mod library
2. Historical paths from backup / ``last_known_path`` (via mod counts)
3. ``games`` table configuration

Status summaries aggregate existing Mod ``content_status`` only — no second
status system, no .info / backup file re-reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from core.sanitize import sanitize_folder_name
from services.file_ops import ModFileManager
from services.game_status import (
    GameStatusSummary,
    ModStatusHint,
    aggregate_category_status,
    aggregate_from_hints,
)
from services.library_status import (
    CONTENT_FOLDER_MISSING,
    CONTENT_HEALTHY,
    GAME_STATUS_HEALTHY,
    GAME_STATUS_MISSING_FOLDER,
    compute_game_status,
    row_content_status,
)

ORIGIN_FILESYSTEM = "filesystem"
ORIGIN_BACKUP = "backup"
ORIGIN_GAMES_TABLE = "games_table"


@dataclass
class GameLibraryEntry:
    folder: str
    display: str
    app_id: int = 0
    count: int = 0
    categories: list[str] = field(default_factory=list)
    game_status: str = GAME_STATUS_HEALTHY
    origin: str = ORIGIN_FILESYSTEM
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
            "origin": self.origin,
            "overall_status": (
                self.status_summary.overall_status
                if self.status_summary is not None
                else "healthy"
            ),
            "status_summary": (
                self.status_summary.to_dict() if self.status_summary is not None else None
            ),
        }


def resolve_games(
    library_root: str | Path,
    *,
    mod_counts: dict[str, int] | None = None,
    mod_hints: Sequence[ModStatusHint] | None = None,
) -> list[GameLibraryEntry]:
    """
    Build the canonical Game Library list + status summaries.

    ``mod_hints`` should come from snapshot cards (preferred). When omitted,
    hints are derived from ``list_visible_mods`` + SQLite ``content_status``
    (no .info / backup file I/O).
    """
    from core.db_manager import get_db

    root = Path(library_root)
    counts = dict(mod_counts or {})
    hints = list(mod_hints) if mod_hints is not None else _derive_mod_hints(root)
    if not counts:
        for h in hints:
            key = str(h.game_folder or "").strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
        if not counts:
            counts = _derive_mod_counts(root)

    # --- Source maps ---
    disk_names: list[str] = []
    try:
        if root.is_dir():
            disk_names = list(ModFileManager(root).list_games())
    except Exception:  # noqa: BLE001
        disk_names = [p.name for p in root.iterdir() if p.is_dir()] if root.is_dir() else []

    db_by_folder: dict[str, tuple[str, int, list[str]]] = {}
    try:
        db_games = [g for g in get_db().list_games() if int(g.app_id or 0) > 0]
    except Exception:  # noqa: BLE001
        db_games = []
    for game in db_games:
        app_id = int(game.app_id)
        display = (game.display_name or "").strip() or f"App_{app_id}"
        folder = (game.folder_name or "").strip() or sanitize_folder_name(
            display, fallback=f"App_{app_id}"
        )
        categories: list[str] = []
        try:
            categories = list(get_db().list_game_categories(app_id))
        except Exception:  # noqa: BLE001
            categories = []
        db_by_folder[folder] = (display, app_id, categories)

    backup_folders = {
        name for name, n in counts.items() if str(name).strip() and int(n) > 0
    }

    entries: dict[str, GameLibraryEntry] = {}

    # 1) Filesystem first
    for name in disk_names:
        key = str(name or "").strip()
        if not key:
            continue
        display, app_id, categories = db_by_folder.get(key, (key, 0, []))
        entries[key] = GameLibraryEntry(
            folder=key,
            display=display or key,
            app_id=int(app_id),
            count=int(counts.get(key, 0)),
            categories=list(categories),
            game_status=compute_game_status(root, key),
            origin=ORIGIN_FILESYSTEM,
        )

    # 2) Backup / historical paths (missing folders still listed)
    for key in sorted(backup_folders, key=str.casefold):
        if key in entries:
            entries[key].count = max(int(entries[key].count), int(counts.get(key, 0)))
            continue
        display, app_id, categories = db_by_folder.get(key, (key, 0, []))
        entries[key] = GameLibraryEntry(
            folder=key,
            display=display or key,
            app_id=int(app_id),
            count=int(counts.get(key, 0)),
            categories=list(categories),
            game_status=compute_game_status(root, key),
            origin=ORIGIN_BACKUP,
        )

    # 3) games table only
    for key, (display, app_id, categories) in db_by_folder.items():
        if key in entries:
            ent = entries[key]
            if display:
                ent.display = display
            if app_id:
                ent.app_id = int(app_id)
            if categories:
                ent.categories = list(categories)
            continue
        entries[key] = GameLibraryEntry(
            folder=key,
            display=display or key,
            app_id=int(app_id),
            count=int(counts.get(key, 0)),
            categories=list(categories),
            game_status=compute_game_status(root, key),
            origin=ORIGIN_GAMES_TABLE,
        )

    # Attach aggregated status (from hints only — no disk re-scan)
    for key, ent in entries.items():
        summary = aggregate_from_hints(key, hints, game_status=ent.game_status)
        if summary.total_mods == 0 and ent.count > 0:
            # Counts exist but hints missing — keep game_status signal only
            summary = aggregate_from_hints(
                key,
                [
                    ModStatusHint(
                        game_folder=key,
                        content_status=(
                            CONTENT_FOLDER_MISSING
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


def _derive_mod_hints(library_root: Path) -> list[ModStatusHint]:
    """Build hints from visible mods + SQLite content_status (no backup files)."""
    hints: list[ModStatusHint] = []
    try:
        from core.db_manager import get_db
        from services.mod_metadata_resolver import list_visible_mods

        db = get_db()
        for item in list_visible_mods(library_root, None):
            folder = ""
            path = Path(str(getattr(item, "managed_path", "") or ""))
            if path.parent and path.parent.name:
                folder = path.parent.name
            if not folder:
                folder = str(getattr(item, "game_name", "") or "").strip()
            if not folder:
                continue
            mid = str(getattr(item, "published_file_id", "") or "").strip()
            cs = CONTENT_HEALTHY
            absent = False
            try:
                if mid:
                    brow = db.get_mod_backup_row(mid)
                    if brow is not None:
                        cs = row_content_status(brow) or CONTENT_HEALTHY
                        absent = not bool(int(brow.get("folder_present") or 0))
                        if absent and cs == CONTENT_HEALTHY:
                            cs = CONTENT_FOLDER_MISSING
            except Exception:  # noqa: BLE001
                pass
            if not bool(getattr(item, "folder_present", True)):
                absent = True
                if cs == CONTENT_HEALTHY:
                    cs = CONTENT_FOLDER_MISSING
            hints.append(
                ModStatusHint(
                    game_folder=folder,
                    content_status=cs,
                    folder_absent=absent,
                )
            )
    except Exception:  # noqa: BLE001
        return []
    return hints


def _derive_mod_counts(library_root: Path) -> dict[str, int]:
    """Best-effort counts without changing resolver APIs."""
    counts: dict[str, int] = {}
    for h in _derive_mod_hints(library_root):
        key = str(h.game_folder or "").strip()
        if key:
            counts[key] = counts.get(key, 0) + 1
    if counts:
        return counts
    try:
        from core.db_manager import get_db

        for row in get_db().iter_mod_backup_rows():
            lkp = str(row.get("last_known_path") or "").strip()
            if not lkp:
                continue
            parent = Path(lkp).parent.name
            if parent:
                counts[parent] = counts.get(parent, 0) + 1
    except Exception:  # noqa: BLE001
        pass
    return counts


def resolve_games_as_dicts(
    library_root: str | Path,
    *,
    mod_counts: dict[str, int] | None = None,
    mod_hints: Sequence[ModStatusHint] | None = None,
) -> list[dict[str, object]]:
    return [
        e.to_dict()
        for e in resolve_games(
            library_root, mod_counts=mod_counts, mod_hints=mod_hints
        )
    ]
