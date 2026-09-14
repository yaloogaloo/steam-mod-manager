"""Batch Mod Library list index — Database-first Layer-1 rows.

ARCHITECTURE RULE
-----------------
Library list / game-switch must **not** call ``list_visible_mods``, walk the
filesystem, or ``load_backup``. Snapshot rows are ``ModListItem`` / light
``ModCardData`` (no description body).

``conflict`` / ``conflict_status`` are user annotation mirrors from SQLite.
``content_status`` comes from DB columns maintained by Refresh / content eval —
list load does not re-scan payloads.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from core.db_manager import get_db
from core.mod_platform import PLATFORM_STEAM, normalize_platform
from core.models import ModMetadata
from services.mod_list_item import ModListItem

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_INSTANCE: ModLibraryCache | None = None


def _cache_root_key(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser().resolve())
    except OSError:
        return str(Path(path))


def _folder_mtime(folder: Path) -> float:
    try:
        return float(folder.stat().st_mtime)
    except OSError:
        return 0.0


@dataclass
class ModCardData:
    """UI bind adapter for Layer-1 list rows (description always empty on list path)."""

    id: str
    title: str
    platform: str
    cover: str
    description: str
    tags: str
    size: int | None
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
    abandoned: bool = False
    conflict: bool = False
    conflict_status: str = "none"
    enabled: bool = True
    source_url: str = ""
    external_id: str = ""
    workspace_id: str = ""
    category_tags: str = ""
    tag_values: str = ""
    folder_absent: bool = False
    missing_content: bool = False
    library_status: str = ""
    source_type: str = ""
    content_status: str = ""
    identity_status: str = "ok"
    relation_deps: int = 0
    relation_conflicts: int = 0
    size_status: str = "unknown"
    type_id: int | None = None

    @property
    def internal_id(self) -> str:
        return self.id

    @property
    def mod_id(self) -> str:
        """Legacy UI alias for entity id (same as ``id`` / ``internal_id``)."""
        return self.id


@dataclass
class GameSidebarEntry:
    folder: str
    display: str
    app_id: int
    count: int
    categories: list[str] = field(default_factory=list)
    game_status: str = "healthy"
    status_summary: object | None = None
    category_summaries: dict = field(default_factory=dict)


@dataclass
class LibrarySnapshot:
    cards: list[ModCardData]
    games: list[GameSidebarEntry]
    total_count: int
    library_root: str = ""
    list_items: list[ModListItem] = field(default_factory=list)


def card_data_to_metadata(data: ModCardData) -> ModMetadata:
    """Adapt Projection → legacy ModMetadata for Detail/Card paint.

    Identity axes (never merge)::

        data.id            → ModMetadata.internal_id   (session PK handle)
        Frozen TEXT identity is ``mods.internal_id``; resolve via resolve_mod_pk.
        data.external_id   → ModMetadata.published_file_id when Steam Workshop
        data.workspace_id  → not copied (display-only; lives in DB/sidecar)
    """
    from services.identity_service import sidecar_published_file_id

    mid = str(data.id or "").strip()
    plat = normalize_platform(data.platform or PLATFORM_STEAM)
    pub = sidecar_published_file_id(
        mod_id=mid,
        platform=plat,
        external_id=str(data.external_id or "").strip(),
    )
    return ModMetadata(
        published_file_id=pub,
        internal_id=mid,
        title=str(data.metadata_title or data.title or ""),
        description="",  # Layer-1: never ship description on list bind
        managed_path=str(data.managed_path or ""),
        local_path=str(data.managed_path or ""),
        cover_path=str(data.cover or "") or None,
        url=str(data.source_url or ""),
        game_name=str(data.game_folder or data.game_name or ""),
        source_type=plat,
        json_display_name=str(data.json_display_name or ""),
        offline_page_path=None,
    )


def _row_type_id(row: dict[str, Any]) -> int | None:
    raw = row.get("type_id")
    if raw is None or raw == "":
        return None
    try:
        tid = int(raw)
    except (TypeError, ValueError):
        return None
    return tid if tid > 0 else None


def _row_local_size_bytes(row: dict[str, Any]) -> int | None:
    raw = row.get("local_size_bytes")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _row_local_size_status(row: dict[str, Any]) -> str:
    text = str(row.get("local_size_status") or "unknown").strip() or "unknown"
    return text


def mod_list_item_from_row(row: dict[str, Any]) -> ModListItem:
    return ModListItem(
        internal_id=str(row.get("internal_id") or ""),
        workspace_id=str(row.get("workspace_id") or ""),
        game_id=int(row.get("game_id") or 0),
        game_folder=str(row.get("game_folder") or ""),
        name=str(row.get("name") or ""),
        favorite=bool(row.get("favorite")),
        cover_path=str(row.get("cover_path") or ""),
        status_badge=str(row.get("status_badge") or ""),
        managed_path=str(row.get("managed_path") or ""),
        platform=str(row.get("platform") or ""),
        deployed=bool(row.get("deployed")),
        folder_absent=bool(row.get("folder_absent")),
        content_status=str(row.get("content_status") or ""),
        identity_status=str(row.get("identity_status") or "ok"),
        conflict=bool(row.get("conflict")),
        conflict_status=str(row.get("conflict_status") or "none"),
        invalid=bool(row.get("invalid")),
        abandoned=bool(row.get("abandoned")),
        enabled=bool(row.get("enabled", True)),
        has_offline=bool(row.get("has_offline")),
        mtime=float(row.get("mtime") or 0.0),
        category_tags=str(row.get("category_tags") or ""),
        external_id=str(row.get("external_id") or ""),
        source_url=str(row.get("source_url") or ""),
        source_type=str(row.get("source_type") or ""),
        deploy_status=str(row.get("deploy_status") or "not_deployed"),
        offline_status=str(row.get("offline_status") or "none"),
        library_status=str(row.get("library_status") or ""),
        steam_name=str(row.get("steam_name") or ""),
        relation_deps=int(row.get("relation_deps") or 0),
        relation_conflicts=int(row.get("relation_conflicts") or 0),
        notes_preview=str(row.get("notes_preview") or ""),
        local_size_bytes=_row_local_size_bytes(row),
        local_size_status=_row_local_size_status(row),
        type_id=_row_type_id(row),
    )


def list_item_to_card_data(item: ModListItem) -> ModCardData:
    """Adapt Layer-1 item → UI card DTO (description always empty)."""
    content = str(item.content_status or "")
    missing = content == "content_missing"
    status = str(getattr(item, "local_size_status", "") or "unknown")
    measured = item.local_size_bytes if status == "ok" else None
    data = ModCardData(
        id=item.internal_id,
        title=item.name,
        platform=item.platform or PLATFORM_STEAM,
        cover=item.cover_path,
        description="",
        tags=item.category_tags,
        size=measured,
        updated_time=float(item.mtime or 0.0),
        managed_path=item.managed_path,
        game_folder=item.game_folder,
        steam_name=item.steam_name or item.name,
        json_display_name=item.name,
        metadata_title=item.steam_name or item.name,
        notes=item.notes_preview,
        game_name=item.game_folder,
        favorite=item.favorite,
        deployed=item.deployed,
        deploy_status=item.deploy_status,
        has_offline=item.has_offline,
        offline_status=item.offline_status,
        invalid=item.invalid,
        abandoned=bool(getattr(item, "abandoned", False)),
        conflict=item.conflict,
        conflict_status=str(item.conflict_status or "none"),
        enabled=item.enabled,
        source_url=item.source_url,
        external_id=item.external_id,
        workspace_id=item.workspace_id,
        category_tags=item.category_tags,
        tag_values="",
        folder_absent=item.folder_absent,
        missing_content=missing,
        library_status=item.library_status or content,
        source_type=item.source_type or item.platform,
        content_status=content,
        identity_status=str(getattr(item, "identity_status", "") or "ok"),
        relation_deps=item.relation_deps,
        relation_conflicts=item.relation_conflicts,
        size_status=status,
        type_id=item.type_id,
    )
    if item.type_id:
        try:
            from services.mod_type_catalog import get_mod_type_catalog

            type_name = get_mod_type_catalog().resolve_name(item.game_id, item.type_id)
        except Exception:  # noqa: BLE001
            type_name = ""
        if type_name:
            data = replace(data, category_tags=type_name, tags=type_name)
    return data


def apply_content_status_to_card_data(
    data: ModCardData,
    *,
    content_status: str,
    folder_absent: bool | None = None,
    library_status: str | None = None,
) -> ModCardData:
    """Return a copy of *data* with authoritative content_status fields updated."""
    from services.library_status import CONTENT_CONTENT_MISSING
    from services.status_authority import normalize_content_axis

    cs = normalize_content_axis(content_status)
    absent = bool(data.folder_absent if folder_absent is None else folder_absent)
    if absent and not str(content_status or "").strip():
        cs = CONTENT_CONTENT_MISSING
    return replace(
        data,
        content_status=cs,
        library_status=str(library_status or data.library_status or cs),
        folder_absent=absent,
        missing_content=(cs == CONTENT_CONTENT_MISSING),
    )


def fetch_mod_list_item(internal_id: str | int) -> ModListItem | None:
    """Load one Layer-1 row from SQLite (full projection source of truth).

    Accepts Frozen TEXT ``internal_id`` or a PK session handle. The Layer-1
    ``ModListItem.internal_id`` field remains the session row key
    (``mods.mod_id``) — not Frozen identity.
    """
    from services.identity_service import resolve_mod_pk

    token = str(internal_id or "").strip()
    if not token:
        return None
    try:
        mid = resolve_mod_pk(token, db=get_db())
    except Exception:  # noqa: BLE001
        mid = token if token.isdigit() else ""
    if not mid.isdigit():
        return None
    try:
        rows = get_db().list_mod_list_items(mod_id=mid)
    except Exception:  # noqa: BLE001
        logger.debug("fetch_mod_list_item failed internal_id=%s", mid, exc_info=True)
        return None
    if not rows:
        return None
    item = mod_list_item_from_row(rows[0])
    try:
        fields = get_db().get_mods_search_fields([mid]).get(mid)
        if fields is not None:
            if str(fields.category_tags or "").strip():
                item = replace(item, category_tags=str(fields.category_tags or ""))
            if getattr(fields, "type_id", None) is not None:
                item = replace(item, type_id=fields.type_id)
    except Exception:  # noqa: BLE001
        pass
    try:
        rel = get_db().get_relationship_counts([mid]).get(mid, (0, 0))
        item = replace(
            item,
            relation_deps=int(rel[0] or 0),
            relation_conflicts=int(rel[1] or 0),
        )
    except Exception:  # noqa: BLE001
        pass
    return item


class ModLibraryCache:
    """Process-wide card snapshot. ``load_snapshot`` rebuilds from SQLite Layer-1."""

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

    def get_card_data(self, internal_id: str) -> ModCardData | None:
        return self._by_id.get(str(internal_id or "").strip())

    def invalidate(self, internal_id: str | None = None) -> None:
        if internal_id is None:
            self._by_id.clear()
            self._all.clear()
            self._snapshot = None
            self._root = ""
            return
        mid = str(internal_id).strip()
        self._by_id.pop(mid, None)
        self._all = [c for c in self._all if c.id != mid]
        if self._snapshot is not None:
            cards = [c for c in self._snapshot.cards if c.id != mid]
            self._snapshot = LibrarySnapshot(
                cards=cards,
                games=self._snapshot.games,
                total_count=len(cards),
                library_root=self._snapshot.library_root,
                list_items=[
                    i
                    for i in (self._snapshot.list_items or [])
                    if str(getattr(i, "internal_id", "") or "") != mid
                ],
            )

    def put_card_data(self, data: ModCardData) -> None:
        if not data.id:
            return
        self._by_id[data.id] = data
        self._all = [c for c in self._all if c.id != data.id]
        self._all.append(data)

    def patch_local_size(
        self,
        internal_id: str | int,
        *,
        size_bytes: int | None,
        status: str,
    ) -> ModCardData | None:
        """
        Replace warm size scalars only — never ``notify_mod_changed``.

        ``ok`` + 0 is a real empty directory. Other statuses do not expose bytes
        as a sortable size. Returns None when the Mod is not in the warm cache.
        """
        mid = str(internal_id or "").strip()
        if not mid:
            return None
        existing = self._by_id.get(mid)
        if existing is None:
            return None
        text = str(status or "unknown").strip() or "unknown"
        measured = size_bytes if text == "ok" else None
        if text == "ok" and measured is not None:
            try:
                measured = int(measured)
            except (TypeError, ValueError):
                measured = None
                text = "unknown"
        updated = replace(existing, size=measured, size_status=text)
        self.put_card_data(updated)
        if self._snapshot is not None:
            cards: list[ModCardData] = []
            found = False
            for card in self._snapshot.cards:
                if str(card.id) == mid:
                    cards.append(updated)
                    found = True
                else:
                    cards.append(card)
            if not found:
                cards.append(updated)
            items: list[ModListItem] = []
            for existing_item in list(self._snapshot.list_items or []):
                if str(existing_item.internal_id) == mid:
                    items.append(
                        replace(
                            existing_item,
                            local_size_bytes=size_bytes,
                            local_size_status=text,
                        )
                    )
                else:
                    items.append(existing_item)
            self._snapshot = LibrarySnapshot(
                cards=cards,
                games=self._snapshot.games,
                total_count=len(cards),
                library_root=self._snapshot.library_root,
                list_items=items,
            )
            self._all = list(cards)
        return updated

    def refresh_projection(self, internal_id: str | int) -> ModCardData | None:
        """
        Re-read one Mod's full Layer-1 row from SQLite and replace warm projection.

        ARCHITECTURE RULE: single-row replace only — never rebuild the Library
        snapshot, scan the filesystem, or run reconcile.
        """
        mid = str(internal_id or "").strip()
        if not mid:
            return None
        item = fetch_mod_list_item(mid)
        if item is None:
            return None
        updated = list_item_to_card_data(item)
        self.put_card_data(updated)
        if self._snapshot is not None:
            found = False
            cards: list[ModCardData] = []
            for card in self._snapshot.cards:
                if str(card.id) == mid:
                    cards.append(updated)
                    found = True
                else:
                    cards.append(card)
            if not found:
                cards.append(updated)
            items: list[ModListItem] = []
            item_found = False
            for existing in list(self._snapshot.list_items or []):
                if str(existing.internal_id) == mid:
                    items.append(item)
                    item_found = True
                else:
                    items.append(existing)
            if not item_found:
                items.append(item)
            self._snapshot = LibrarySnapshot(
                cards=cards,
                games=self._snapshot.games,
                total_count=len(cards),
                library_root=self._snapshot.library_root,
                list_items=items,
            )
            self._all = list(cards)
        return updated

    def patch_content_status(
        self,
        internal_id: str,
        *,
        content_status: str = "",
        folder_absent: bool | None = None,
        library_status: str | None = None,
    ) -> ModCardData | None:
        """
        After content_status is persisted to DB, refresh that Mod's projection.

        Keyword args are ignored — warm cache always re-reads SQLite.
        """
        del content_status, folder_absent, library_status
        return self.refresh_projection(internal_id)


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
    Build Library list index from SQLite (DB-first).

    ARCHITECTURE RULE: must not call ``list_visible_mods``, walk managed
    folders, ``load_backup``, ``resolve_games``, or filesystem ``list_games``.
    Game sidebar comes from ``games`` + ``mods`` aggregates only.
    Safe on a worker thread (no QWidget).
    """
    import time

    from services.library_perf_metrics import get_library_perf_metrics
    from services.perf_stage import log_perf_stage, perf_stage

    root = Path(library_root)
    root.mkdir(parents=True, exist_ok=True)
    t_all = time.perf_counter()
    query_ms = 0.0
    vm_ms = 0.0

    list_items: list[ModListItem] = []
    cards: list[ModCardData] = []
    try:
        db = get_db()
        with perf_stage("library_query") as qbag:
            t_q = time.perf_counter()
            rows = db.list_mod_list_items()
            qbag["rows"] = len(rows)
            mod_ids = [
                str(r.get("internal_id") or "")
                for r in rows
                if str(r.get("internal_id") or "").isdigit()
            ]
            cat_map: dict[str, str] = {}
            type_map: dict[str, int | None] = {}
            rel_counts: dict[str, tuple[int, int]] = {}
            if mod_ids:
                try:
                    t_tag = time.perf_counter()
                    fields_map = db.get_mods_search_fields(mod_ids)
                    for mid, fields in fields_map.items():
                        cat_map[mid] = str(getattr(fields, "category_tags", "") or "")
                        type_map[mid] = getattr(fields, "type_id", None)
                    qbag["tag_join_ms"] = round(
                        (time.perf_counter() - t_tag) * 1000.0, 1
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("category batch failed", exc_info=True)
                try:
                    t_rel = time.perf_counter()
                    rel_counts = db.get_relationship_counts(mod_ids)
                    qbag["relationship_join_ms"] = round(
                        (time.perf_counter() - t_rel) * 1000.0, 1
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("relationship batch failed", exc_info=True)
            query_ms = (time.perf_counter() - t_q) * 1000.0
            qbag["sql_total_ms"] = round(query_ms, 1)

        with perf_stage("snapshot_viewmodel", rows=len(rows)) as vbag:
            t_vm = time.perf_counter()
            try:
                from services.managed_path_cache import put_managed_path

                warm_paths = True
            except Exception:  # noqa: BLE001
                warm_paths = False
                put_managed_path = None  # type: ignore[assignment]
            for row in rows:
                mid = str(row.get("internal_id") or "")
                row_out = dict(row)
                if mid in cat_map and cat_map[mid]:
                    row_out["category_tags"] = cat_map[mid]
                if mid in type_map:
                    row_out["type_id"] = type_map[mid]
                if mid in rel_counts:
                    deps, confs = rel_counts[mid]
                    row_out["relation_deps"] = int(deps)
                    row_out["relation_conflicts"] = int(confs)
                item = mod_list_item_from_row(row_out)
                list_items.append(item)
                cards.append(list_item_to_card_data(item))
                if warm_paths and mid.isdigit() and str(item.managed_path or "").strip():
                    try:
                        put_managed_path(mid, item.managed_path, library_root=root)
                    except Exception:  # noqa: BLE001
                        pass
            vm_ms = (time.perf_counter() - t_vm) * 1000.0
            vbag["card_data_count"] = len(cards)
            vbag["viewmodel_ms"] = round(vm_ms, 1)
    except Exception:  # noqa: BLE001
        logger.exception("DB-first library snapshot failed")
        list_items = []
        cards = []

    try:
        from services.startup_io_trace import log_io_event

        log_io_event("library_load", "snapshot", mods_seen=len(cards))
    except Exception:  # noqa: BLE001
        pass

    games = _build_game_entries(cards)
    total_ms = (time.perf_counter() - t_all) * 1000.0
    try:
        get_library_perf_metrics().record_index_load(
            database_query_ms=query_ms,
            viewmodel_create_ms=vm_ms,
            total_ms=total_ms,
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        log_perf_stage(
            "library_data_load",
            total_ms,
            cards=len(cards),
            query_ms=round(query_ms, 1),
            viewmodel_ms=round(vm_ms, 1),
        )
    except Exception:  # noqa: BLE001
        pass
    return LibrarySnapshot(
        cards=cards,
        games=games,
        total_count=len(cards),
        library_root=str(root),
        list_items=list_items,
    )


def _build_game_entries(
    cards: list[ModCardData],
) -> list[GameSidebarEntry]:
    """
    Library sidebar from Database projection only.

    ARCHITECTURE RULE: must not call ``resolve_games`` / ``list_games`` /
    filesystem ``iterdir``. Filesystem discovery belongs to Sync/Reconcile.
    """
    from services.game_sidebar import build_game_sidebar_view_models
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
                identity_status=str(getattr(card, "identity_status", "") or "ok"),
                category=cat,
                folder_absent=bool(card.folder_absent),
                conflict_status=str(getattr(card, "conflict_status", "") or "none"),
                invalid=bool(getattr(card, "invalid", False)),
            )
        )

    resolved = build_game_sidebar_view_models(mod_counts=counts, mod_hints=hints)
    return [
        GameSidebarEntry(
            folder=g.folder,
            display=g.display,
            app_id=int(g.app_id),
            count=int(g.count),
            categories=list(g.categories),
            game_status=str(getattr(g, "game_status", "") or "healthy"),
            status_summary=getattr(g, "status_summary", None),
            category_summaries=dict(getattr(g, "category_summaries", {}) or {}),
        )
        for g in resolved
    ]
