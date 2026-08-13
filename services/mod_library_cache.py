"""Batch Mod Library card data — one filesystem scan + one SQLite round-trip."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.db_manager import DEPLOY_STATUS_DEPLOYED, get_db
from core.mod_platform import PLATFORM_STEAM, normalize_offline_status, normalize_platform
from core.models import ModMetadata
from services.file_ops import (
    INFO_DIR_NAME,
    LEGACY_INFO_DIR_NAME,
    MISSING_CONTENT_METADATA_KEY,
    ModFileManager,
)
from services.library_status import (
    GAME_STATUS_HEALTHY,
    compute_content_status,
    content_status_to_library_status,
    normalize_library_source,
    row_content_status,
    row_source_type,
)

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_INSTANCE: ModLibraryCache | None = None


def _cache_root_key(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path))


def _missing_content_fast(folder: Path, data: dict[str, Any] | None) -> bool:
    if not folder.is_dir():
        return True
    if data and data.get(MISSING_CONTENT_METADATA_KEY) is True:
        return True
    try:
        for child in folder.iterdir():
            if child.name in {INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, ".cache"}:
                continue
            if child.is_file():
                return False
            if child.is_dir():
                return False
    except OSError:
        return True
    return True


def _folder_mtime(folder: Path) -> float:
    try:
        return float(folder.stat().st_mtime)
    except OSError:
        return 0.0


@dataclass
class ModCardData:
    """Immutable-enough payload for one library card (no SQLite on the widget)."""

    id: str
    title: str
    platform: str
    cover: str
    description: str
    tags: str
    size: int
    updated_time: float
    managed_path: str
    game_folder: str
    steam_name: str = ""
    json_display_name: str = ""
    metadata_title: str = ""
    notes: str = ""
    game_name: str = ""
    favorite: bool = False
    deployed: bool = False
    deploy_status: str = "not_deployed"
    has_offline: bool = False
    offline_status: str = "none"
    invalid: bool = False
    conflict: bool = False
    conflict_status: str = "none"
    enabled: bool = True
    source_url: str = ""
    external_id: str = ""
    category_tags: str = ""
    tag_values: str = ""
    folder_absent: bool = False
    missing_content: bool = False
    library_status: str = ""
    source_type: str = ""
    content_status: str = ""
    relation_deps: int = 0
    relation_conflicts: int = 0

    @property
    def mod_id(self) -> str:
        return self.id


@dataclass
class GameSidebarEntry:
    folder: str
    display: str
    app_id: int
    count: int
    categories: list[str] = field(default_factory=list)
    game_status: str = GAME_STATUS_HEALTHY
    status_summary: object | None = None
    category_summaries: dict = field(default_factory=dict)
    status_summary: object | None = None
    category_summaries: dict = field(default_factory=dict)


@dataclass
class LibrarySnapshot:
    cards: list[ModCardData]
    games: list[GameSidebarEntry]
    total_count: int
    library_root: str = ""


def card_data_to_metadata(data: ModCardData) -> ModMetadata:
    return ModMetadata(
        published_file_id=str(data.id or ""),
        title=str(data.metadata_title or data.title or ""),
        description=str(data.description or ""),
        managed_path=str(data.managed_path or ""),
        local_path=str(data.managed_path or ""),
        cover_path=str(data.cover or "") or None,
        url=str(data.source_url or ""),
        game_name=str(data.game_folder or data.game_name or ""),
        source_type=normalize_platform(data.platform or PLATFORM_STEAM),
        json_display_name=str(data.json_display_name or ""),
        offline_page_path=None,
    )


class ModLibraryCache:
    """Process-wide card snapshot. ``load_all_mod_cards`` rebuilds from disk+DB."""

    def __init__(self) -> None:
        self._by_id: dict[str, ModCardData] = {}
        self._all: list[ModCardData] = []
        self._root: str = ""
        self._snapshot: LibrarySnapshot | None = None

    def load_all_mod_cards(
        self,
        library_root: str | Path,
        game_name: str | None = None,
        *,
        force: bool = True,
    ) -> list[ModCardData]:
        snap = self.load_snapshot(library_root, force=force)
        game = str(game_name or "").strip()
        if not game:
            return list(snap.cards)
        return [c for c in snap.cards if c.game_folder == game]

    def peek_snapshot(self, library_root: str | Path) -> LibrarySnapshot | None:
        """Return warm snapshot for *library_root* without rebuilding."""
        root_key = _cache_root_key(library_root)
        if self._snapshot is not None and self._root == root_key:
            return self._snapshot
        return None

    def load_snapshot(
        self,
        library_root: str | Path,
        *,
        force: bool = True,
    ) -> LibrarySnapshot:
        root = Path(library_root)
        root_key = _cache_root_key(root)
        if (
            not force
            and self._snapshot is not None
            and self._root == root_key
        ):
            return self._snapshot
        snapshot = build_library_snapshot(root)
        self._root = root_key
        self._snapshot = snapshot
        self._all = list(snapshot.cards)
        self._by_id = {c.id: c for c in self._all if c.id}
        return snapshot

    def get_card_data(self, mod_id: str) -> ModCardData | None:
        return self._by_id.get(str(mod_id or "").strip())

    def invalidate(self, mod_id: str | None = None) -> None:
        if mod_id is None:
            self._by_id.clear()
            self._all.clear()
            self._snapshot = None
            self._root = ""
            return
        mid = str(mod_id).strip()
        self._by_id.pop(mid, None)
        self._all = [c for c in self._all if c.id != mid]
        if self._snapshot is not None:
            cards = [c for c in self._snapshot.cards if c.id != mid]
            self._snapshot = LibrarySnapshot(
                cards=cards,
                games=self._snapshot.games,
                total_count=len(cards),
                library_root=self._snapshot.library_root,
            )

    def put_card_data(self, data: ModCardData) -> None:
        if not data.id:
            return
        self._by_id[data.id] = data
        self._all = [c for c in self._all if c.id != data.id]
        self._all.append(data)


def get_library_cache() -> ModLibraryCache:
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is None:
            _INSTANCE = ModLibraryCache()
        return _INSTANCE


def reset_library_cache() -> None:
    global _INSTANCE
    with _LOCK:
        _INSTANCE = None


def build_library_snapshot(library_root: str | Path) -> LibrarySnapshot:
    """
    Scan the library once via Metadata Resolver, batch-read SQLite user state.

    Safe to call from a worker thread (no QWidget).
    """
    from services.mod_metadata_resolver import list_visible_mods

    root = Path(library_root)
    root.mkdir(parents=True, exist_ok=True)
    manager = ModFileManager(root)
    resolved_list = list_visible_mods(root, None)

    mod_ids: list[str] = []
    for item in resolved_list:
        mid = str(item.published_file_id or "").strip()
        if mid.isdigit():
            mod_ids.append(mid)

    fields_map: dict[str, Any] = {}
    tag_flags_map: dict[str, Any] = {}
    rel_counts: dict[str, tuple[int, int]] = {}
    backup_rows: dict[str, Any] = {}
    db = None
    try:
        db = get_db()
        if mod_ids:
            fields_map = db.get_mods_search_fields(mod_ids)
            tag_flags_map = db.get_mods_tag_flags(mod_ids)
            rel_counts = db.get_relationship_counts(mod_ids)
            backup_rows = db.get_mods_backup_rows(mod_ids)
    except Exception:  # noqa: BLE001
        logger.debug("batch library DB read failed", exc_info=True)

    cards: list[ModCardData] = []
    for resolved in resolved_list:
        folder = Path(resolved.managed_path or "")
        mid = str(resolved.published_file_id or "").strip()
        fields = fields_map.get(mid) if mid else None
        flags = tag_flags_map.get(mid) if mid else None
        deps, confs = rel_counts.get(mid, (0, 0)) if mid else (0, 0)
        game_folder = str(resolved.game_name or "").strip()
        try:
            from_path = manager.game_name_for_path(folder)
            if from_path:
                game_folder = from_path
        except Exception:  # noqa: BLE001
            if not game_folder and folder.parent:
                game_folder = folder.parent.name

        notes = ""
        favorite = False
        deployed = False
        deploy_status = "not_deployed"
        game_db = ""
        steam = str(resolved.title or "").strip()
        platform = normalize_platform(resolved.platform or PLATFORM_STEAM)
        source_url = str(resolved.source_url or "").strip()
        external_id = ""
        is_invalid = False
        conflict_status = "none"
        enabled = True
        category_tags = ""
        if fields is not None:
            notes = str(fields.user_notes or "")
            favorite = bool(fields.favorite)
            deploy_status = str(fields.deploy_status or "not_deployed")
            deployed = deploy_status == DEPLOY_STATUS_DEPLOYED
            game_db = str(fields.game_name or "").strip()
            if not steam:
                steam = str(fields.steam_name or "").strip()
            external_id = str(fields.external_id or "").strip()
            is_invalid = bool(fields.is_invalid)
            conflict_status = str(fields.conflict_status or "none")
            enabled = bool(fields.enabled)
            category_tags = str(fields.category_tags or "")

        title = str(resolved.display_name or resolved.title or folder.name or "—").strip()
        json_display = str(resolved.display_name or "").strip()
        meta_title = str(resolved.title or "").strip()
        invalid = is_invalid or bool(getattr(flags, "invalid", False)) if flags else is_invalid
        conflict = (conflict_status in ("conflict", "warning")) or (
            bool(getattr(flags, "conflict", False)) if flags else False
        )
        tag_values = ""
        if flags is not None:
            values = getattr(flags, "tag_values", ()) or ()
            reason = getattr(flags, "invalid_reason", "") or ""
            tag_values = " ".join(p for p in (*values, reason) if str(p).strip())

        folder_absent = not bool(resolved.folder_present)
        cover = str(resolved.cover_path or "").strip()
        desc = str(resolved.description or "").strip()
        off_ref = str(resolved.offline_path or "").strip()
        has_offline = False
        if off_ref:
            try:
                has_offline = Path(off_ref).is_file()
            except OSError:
                has_offline = False
        missing = True if folder_absent else _missing_content_fast(folder, None)
        offline_status = "none"
        if fields is not None:
            offline_status = normalize_offline_status(
                str(getattr(fields, "offline_status", "") or "none")
            )
        brow = backup_rows.get(mid) if mid else None
        source_type = row_source_type(brow) if brow else normalize_library_source(platform)
        if normalize_library_source(source_type) == "unknown":
            source_type = normalize_library_source(platform)
        backup_status = str((brow or {}).get("backup_status") or "")
        db_content = row_content_status(brow) if brow else ""
        identity_conflict = conflict or db_content == "identity_conflict"
        content_status = compute_content_status(
            folder_present=not folder_absent,
            identity_conflict=identity_conflict,
            backup_status=backup_status
            or ("invalid" if db_content == "backup_invalid" else ""),
            missing_content=bool(missing) and not folder_absent,
        )
        library_status = content_status_to_library_status(content_status)

        cards.append(
            ModCardData(
                id=mid,
                title=title,
                platform=platform,
                cover=cover,
                description=desc,
                tags=" ".join(p for p in (category_tags, tag_values) if p),
                size=0,
                updated_time=_folder_mtime(folder) if not folder_absent else 0.0,
                managed_path=str(folder),
                game_folder=game_folder,
                steam_name=steam,
                json_display_name=json_display,
                metadata_title=meta_title,
                notes=notes,
                game_name=" ".join(p for p in (game_db, game_folder) if p),
                favorite=favorite,
                deployed=deployed,
                deploy_status=deploy_status,
                has_offline=has_offline,
                offline_status=offline_status,
                invalid=invalid,
                conflict=conflict,
                conflict_status=conflict_status,
                enabled=enabled,
                source_url=source_url,
                external_id=external_id,
                category_tags=category_tags,
                tag_values=tag_values,
                folder_absent=folder_absent,
                missing_content=missing,
                library_status=library_status,
                source_type=source_type,
                content_status=content_status,
                relation_deps=int(deps),
                relation_conflicts=int(confs),
            )
        )

    games = _build_game_entries(root, cards)
    return LibrarySnapshot(
        cards=cards,
        games=games,
        total_count=len(cards),
        library_root=str(root),
    )


def _build_game_entries(
    library_root: Path,
    cards: list[ModCardData],
) -> list[GameSidebarEntry]:
    from services.game_library import resolve_games
    from services.game_status import ModStatusHint

    counts: dict[str, int] = {}
    hints: list[ModStatusHint] = []
    for card in cards:
        key = str(card.game_folder or "").strip()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        cat = ""
        tags = str(card.category_tags or "").split()
        if tags:
            cat = str(tags[0]).strip()
        hints.append(
            ModStatusHint(
                game_folder=key,
                content_status=str(card.content_status or "") or "healthy",
                category=cat,
                folder_absent=bool(card.folder_absent),
            )
        )

    resolved = resolve_games(library_root, mod_counts=counts, mod_hints=hints)
    return [
        GameSidebarEntry(
            folder=g.folder,
            display=g.display,
            app_id=int(g.app_id),
            count=int(g.count),
            categories=list(g.categories),
            game_status=str(g.game_status or GAME_STATUS_HEALTHY),
            status_summary=g.status_summary,
            category_summaries=dict(g.category_summaries or {}),
        )
        for g in resolved
    ]
