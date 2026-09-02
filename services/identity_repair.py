"""Historical identity repair planner + gated apply.

Read-only by default. Never mints a Mod. Never runs from refresh / offline /
deploy / metadata / reconcile. Display names and folder names are not identity
authority; leftover ``Unknown Mod <id>`` folders are supporting evidence only.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sqlite3
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.mod_platform import (
    PLATFORM_STEAM,
    is_internal_mod_id,
    is_provisional_external_id,
    normalize_platform,
    normalize_platform_if_known,
    steam_workshop_url,
)
from core.paths import data_dir, database_path, default_mod_library
from services.file_ops import (
    INFO_DIR_NAME,
    METADATA_FILENAME,
    ModFileManager,
    persist_unified_metadata_dict,
    read_info_metadata_dict,
)
from services.identity_service import (
    RepairMustNotAllocateError,
    repair_no_allocate_scope,
    sidecar_published_file_id,
)
from services.importers.duplicate_check import normalize_source_url
from services.mod_identity_authority import (
    log_identity_mutation,
    sanitize_platform_external_id,
)

logger = logging.getLogger(__name__)

REL_SAME_EXTERNAL_ID = "SAME_EXTERNAL_ID"
REL_SAME_CANONICAL_URL = "SAME_CANONICAL_URL"
REL_SAME_WORKSHOP_ITEM = "SAME_WORKSHOP_ITEM"
REL_DUPLICATE_ENTITY = "DUPLICATE_ENTITY"
REL_INTERNAL_POLLUTION = "INTERNAL_IDENTITY_POLLUTION"
REL_ORPHAN = "ORPHAN"
REL_AMBIGUOUS = "AMBIGUOUS"
REL_NO_SAFE_MATCH = "NO_SAFE_MATCH"

ACTION_MERGE = "MERGE_INTO_CANONICAL"
ACTION_REMOVE_META = "REMOVE_GHOST_METADATA_ONLY"
ACTION_QUARANTINE = "QUARANTINE"
ACTION_CONFLICT = "MARK_IDENTITY_CONFLICT"
ACTION_NO_ACTION = "NO_ACTION"
ACTION_SCRUB_URL = "SCRUB_POLLUTING_SOURCE_URL"
ACTION_REMOVE_INVALID = "REMOVE_INVALID_DUPLICATE"

REASON_INVALID_DUPLICATE = "INVALID_DUPLICATE_MOD"

CONF_HIGH = "HIGH"
CONF_MEDIUM = "MEDIUM"
CONF_LOW = "LOW"

_STEAM_FILEDETAILS_RE = re.compile(
    r"(?:sharedfiles/filedetails/?\?id=|[?&]id=)(\d+)", re.IGNORECASE
)
_UNKNOWN_MOD_RE = re.compile(
    r"^Unknown[_\s]Mod[_\s](\d+)$", re.IGNORECASE
)
_INTERNAL_DIGIT_RE = re.compile(r"\d{16,}")

_MOD_ID_COLUMNS = frozenset(
    {
        "mod_id",
        "source_mod_id",
        "target_mod_id",
        "published_file_id",
    }
)

REPAIR_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS identity_repair_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT '',
    ghost_mod_id TEXT NOT NULL DEFAULT '',
    canonical_mod_id TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    external_id TEXT NOT NULL DEFAULT '',
    app_id INTEGER NOT NULL DEFAULT 0,
    relationship TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    before_state TEXT NOT NULL DEFAULT '',
    after_state TEXT NOT NULL DEFAULT ''
)
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def is_valid_steam_workshop_id(value: Any) -> bool:
    text = _text(value)
    return bool(text.isdigit() and not is_internal_mod_id(text) and int(text) > 0)


def workshop_id_from_url(url: str) -> str:
    """Return a workshop-range id from a Steam filedetails URL, else empty."""
    text = _text(url)
    if not text or "steamcommunity.com" not in text.lower():
        return ""
    match = _STEAM_FILEDETAILS_RE.search(text)
    if not match:
        return ""
    wid = match.group(1)
    return wid if is_valid_steam_workshop_id(wid) else ""


def internal_ids_embedded(text: str) -> list[str]:
    found: list[str] = []
    for token in _INTERNAL_DIGIT_RE.findall(_text(text)):
        if is_internal_mod_id(token) and token not in found:
            found.append(token)
    return found


def supporting_workshop_id_from_folder(folder: str | Path) -> str:
    """Folder name is never identity authority. Supporting digits only."""
    name = Path(folder).name if folder else ""
    match = _UNKNOWN_MOD_RE.match(name)
    if not match:
        return ""
    wid = match.group(1)
    return wid if is_valid_steam_workshop_id(wid) else ""


@dataclass
class ReferenceGraph:
    tables: dict[str, int] = field(default_factory=dict)
    filesystem: list[str] = field(default_factory=list)
    sidecar: list[str] = field(default_factory=list)
    backup: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tables": dict(self.tables),
            "filesystem": list(self.filesystem),
            "sidecar": list(self.sidecar),
            "backup": list(self.backup),
        }


@dataclass
class RepairCandidate:
    ghost_mod_id: str = ""
    ghost_folder: str = ""
    ghost_platform: str = ""
    ghost_external_id: str = ""
    ghost_workspace_id: str = ""
    ghost_source_url: str = ""
    ghost_published_file_id: str = ""
    ghost_app_id: int = 0
    ghost_title: str = ""

    candidate_mod_id: str = ""
    candidate_platform: str = ""
    candidate_external_id: str = ""
    candidate_source_url: str = ""
    candidate_folder: str = ""

    relationship: str = REL_NO_SAFE_MATCH
    relationships: list[str] = field(default_factory=list)
    confidence: str = CONF_LOW
    proposed_action: str = ACTION_NO_ACTION
    blocking_reasons: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    references: dict[str, Any] = field(default_factory=dict)
    finding_class: str = "ghost"
    filesystem_action: str = ""
    reference_action: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RepairPlan:
    candidates: list[RepairCandidate] = field(default_factory=list)
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)
    applied: bool = False
    success: bool = True
    error: str = ""
    notes: list[str] = field(default_factory=list)
    allocations: int = 0
    applied_counts: dict[str, int] = field(default_factory=dict)
    forensics: dict[str, Any] = field(default_factory=dict)
    verification: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "candidates": [c.to_dict() for c in self.candidates],
            "before": self.before,
            "after": self.after,
            "applied": self.applied,
            "success": self.success,
            "error": self.error,
            "notes": self.notes,
            "allocations": self.allocations,
            "applied_counts": self.applied_counts,
            "forensics": self.forensics,
        }
        if self.verification is not None:
            payload["verification"] = dict(self.verification)
        return payload


class _ConnFacade:
    """Minimal db facade for read-only sqlite (no DatabaseManager backfill)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()


def open_readonly_sqlite(db_path: str | Path) -> _ConnFacade:
    path = Path(db_path).expanduser().resolve()
    uri = path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return _ConnFacade(conn)


def _rows_as_dicts(db: Any, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with db._lock:  # noqa: SLF001
        rows = db._conn.execute(sql, params).fetchall()  # noqa: SLF001
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(dict(row))
        else:
            out.append({k: row[k] for k in row.keys()})
    return out


def _table_names(db: Any) -> list[str]:
    rows = _rows_as_dicts(
        db, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    return [_text(r.get("name")) for r in rows if _text(r.get("name"))]


def _table_columns(db: Any, table: str) -> list[str]:
    rows = _rows_as_dicts(db, f"PRAGMA table_info({table})")
    return [_text(r.get("name")) for r in rows if _text(r.get("name"))]


def collect_reference_graph(
    db: Any,
    ghost_mod_id: str,
    *,
    library_root: str | Path,
    last_known_path: str = "",
    cover_path: str = "",
    sidecar_folders: list[Path] | None = None,
) -> ReferenceGraph:
    graph = ReferenceGraph()
    mid = _text(ghost_mod_id)
    if not mid:
        return graph
    for table in _table_names(db):
        cols = _table_columns(db, table)
        hits = 0
        for col in cols:
            if col.lower() not in _MOD_ID_COLUMNS and col.lower() != "mod_id":
                continue
            try:
                rows = _rows_as_dicts(
                    db,
                    f'SELECT COUNT(*) AS n FROM "{table}" WHERE CAST("{col}" AS TEXT) = ?',
                    (mid,),
                )
                hits += int(rows[0]["n"] or 0) if rows else 0
            except Exception:  # noqa: BLE001
                continue
        if hits:
            graph.tables[table] = hits

    for raw in (last_known_path, cover_path):
        path = Path(raw) if raw else None
        if path and path.exists():
            graph.filesystem.append(str(path))

    for folder in sidecar_folders or []:
        graph.sidecar.append(str(folder))
        meta = folder / INFO_DIR_NAME / METADATA_FILENAME
        if meta.is_file():
            graph.sidecar.append(str(meta))

    backup = data_dir() / "mod_backup" / mid
    if backup.exists():
        graph.backup.append(str(backup))

    root = Path(library_root)
    if last_known_path:
        candidate = Path(last_known_path)
        if candidate.exists():
            if str(candidate) not in graph.filesystem:
                graph.filesystem.append(str(candidate))
        elif (root / last_known_path).exists():
            graph.filesystem.append(str(root / last_known_path))
    return graph


def _load_mod_rows(db: Any) -> list[dict[str, Any]]:
    return _rows_as_dicts(
        db,
        """
        SELECT mod_id, app_id, title, display_name, platform, source_url,
               external_id, workspace_id, last_known_path, folder_present,
               cover_path, conflict_status, library_status, user_notes,
               custom_description, favorite, backup_cover_path,
               backup_offline_path, backup_metadata_json
        FROM mods
        """,
    )


def _index_sidecars(library_root: str | Path) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    root = Path(library_root)
    by_key: dict[str, list[tuple[Path, dict[str, Any]]]] = defaultdict(list)
    if not root.is_dir():
        return by_key
    for folder in ModFileManager(root).list_managed_mods():
        meta = read_info_metadata_dict(folder) or {}
        pub = _text(meta.get("published_file_id"))
        ext = _text(meta.get("external_id"))
        url = _text(meta.get("url") or meta.get("source_url"))
        keys = {pub, ext, workshop_id_from_url(url), str(folder.resolve())}
        for key in keys:
            if key:
                by_key[key].append((folder, meta))
        by_key[str(folder)].append((folder, meta))
    return by_key


def _sidecar_for_row(
    row: dict[str, Any],
    sidecar_index: dict[str, list[tuple[Path, dict[str, Any]]]],
) -> tuple[Path | None, dict[str, Any]]:
    path = _text(row.get("last_known_path"))
    if path:
        folder = Path(path)
        if folder.is_dir():
            meta = read_info_metadata_dict(folder) or {}
            return folder, meta
        hits = sidecar_index.get(str(folder.resolve()), [])
        if hits:
            return hits[0]
        hits = sidecar_index.get(path, [])
        if hits:
            return hits[0]
    mid = _text(row.get("mod_id"))
    hits = sidecar_index.get(mid, [])
    if len(hits) == 1:
        return hits[0]
    return None, {}


def _canonical_steam_matches(
    rows: list[dict[str, Any]],
    *,
    workshop_id: str,
    exclude_mod_id: str,
) -> list[dict[str, Any]]:
    if not is_valid_steam_workshop_id(workshop_id):
        return []
    matches: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        mid = _text(row.get("mod_id"))
        if mid == exclude_mod_id or mid in seen:
            continue
        plat = normalize_platform_if_known(_text(row.get("platform")))
        ext = sanitize_platform_external_id(
            plat or _text(row.get("platform")),
            _text(row.get("external_id")),
            mod_id=mid,
        )
        steam_pk = plat == PLATFORM_STEAM and mid == workshop_id
        same_ext = ext == workshop_id and (
            plat == PLATFORM_STEAM or is_valid_steam_workshop_id(ext)
        )
        # URL mentions are field pollution, not proof this row *is* the workshop item.
        if steam_pk or same_ext:
            seen.add(mid)
            matches.append(row)
    return matches


def _confidence_and_relationships(
    *,
    official: list[str],
    supporting: list[str],
    pollution: bool,
    match_count: int,
) -> tuple[str, list[str], str]:
    rels: list[str] = []
    if pollution:
        rels.append(REL_INTERNAL_POLLUTION)
    if match_count > 1:
        rels.append(REL_AMBIGUOUS)
        return CONF_LOW, rels, REL_AMBIGUOUS
    if match_count == 1:
        rels.append(REL_DUPLICATE_ENTITY)
        if "external" in official:
            rels.append(REL_SAME_EXTERNAL_ID)
        if "url" in official:
            rels.append(REL_SAME_CANONICAL_URL)
        if "workshop" in official or "published" in official:
            rels.append(REL_SAME_WORKSHOP_ITEM)
        primary = REL_DUPLICATE_ENTITY
        if official:
            return CONF_HIGH, rels, primary
        if supporting:
            return CONF_MEDIUM, rels, primary
        return CONF_LOW, rels, REL_NO_SAFE_MATCH
    if pollution and not official and not supporting:
        rels.append(REL_ORPHAN)
        return CONF_LOW, rels, REL_ORPHAN
    rels.append(REL_NO_SAFE_MATCH)
    return CONF_LOW, rels, REL_NO_SAFE_MATCH


def _classify_steam_ghost(
    row: dict[str, Any],
    all_rows: list[dict[str, Any]],
    *,
    folder: Path | None,
    meta: dict[str, Any],
    graph: ReferenceGraph,
) -> RepairCandidate:
    mid = _text(row.get("mod_id"))
    plat = normalize_platform(_text(row.get("platform")))
    ext = _text(row.get("external_id"))
    ws = _text(row.get("workspace_id"))
    url = normalize_source_url(_text(row.get("source_url")))
    pub = _text(meta.get("published_file_id"))
    sidecar_ext = _text(meta.get("external_id"))
    sidecar_url = _text(meta.get("url") or meta.get("source_url"))
    folder_path = str(folder) if folder else _text(row.get("last_known_path"))

    cand = RepairCandidate(
        ghost_mod_id=mid,
        ghost_folder=folder_path,
        ghost_platform=plat,
        ghost_external_id=ext,
        ghost_workspace_id=ws,
        ghost_source_url=url,
        ghost_published_file_id=pub,
        ghost_app_id=int(row.get("app_id") or 0),
        ghost_title=_text(row.get("title") or row.get("display_name")),
        finding_class="steam_internal_ghost",
        references=graph.to_dict(),
    )

    pollution = bool(
        is_internal_mod_id(mid)
        and (
            plat == PLATFORM_STEAM
            or ws == mid
            or is_internal_mod_id(ext)
            or is_provisional_external_id(ext)
            or is_internal_mod_id(pub)
            or is_internal_mod_id(ws)
        )
    )
    if pollution:
        cand.evidence.append("internal identity leaked into platform/workspace/sidecar")

    official: list[str] = []
    supporting: list[str] = []
    workshop_ids: list[str] = []

    for value, label in (
        (sanitize_platform_external_id(plat, ext, mod_id=mid), "external"),
        (sanitize_platform_external_id(plat, sidecar_ext, mod_id=mid), "sidecar_external"),
        (workshop_id_from_url(url), "url"),
        (workshop_id_from_url(sidecar_url), "sidecar_url"),
        (pub if is_valid_steam_workshop_id(pub) else "", "published"),
    ):
        if is_valid_steam_workshop_id(value):
            official.append(label)
            workshop_ids.append(value)
            cand.evidence.append(f"official {label}={value}")

    folder_wid = supporting_workshop_id_from_folder(folder_path)
    if folder_wid:
        supporting.append("folder_name")
        workshop_ids.append(folder_wid)
        cand.evidence.append(
            f"supporting folder leftover pattern Unknown Mod {folder_wid} (not authority)"
        )

    unique_wids = list(dict.fromkeys(workshop_ids))
    matches: list[dict[str, Any]] = []
    for wid in unique_wids:
        matches.extend(
            _canonical_steam_matches(all_rows, workshop_id=wid, exclude_mod_id=mid)
        )
    # Dedup canonical rows
    by_id = {_text(r["mod_id"]): r for r in matches}
    matches = list(by_id.values())

    # Cover/path overlap is DB relationship evidence, not display-name identity.
    for other in all_rows:
        other_id = _text(other.get("mod_id"))
        if other_id == mid or is_internal_mod_id(other_id):
            continue
        cover = _text(other.get("cover_path"))
        other_path = _text(other.get("last_known_path"))
        if folder_path and cover and Path(cover).parent == Path(folder_path):
            if other not in matches:
                matches.append(other)
            official.append("cover_path")
            cand.evidence.append(f"canonical cover_path points at ghost folder ({other_id})")
        if folder_path and other_path and Path(other_path) == Path(folder_path):
            if other not in matches:
                matches.append(other)
            official.append("same_path")
            cand.evidence.append(f"canonical last_known_path equals ghost folder ({other_id})")

    by_id = {_text(r["mod_id"]): r for r in matches}
    matches = list(by_id.values())

    confidence, rels, primary = _confidence_and_relationships(
        official=official,
        supporting=supporting,
        pollution=pollution,
        match_count=len(matches),
    )
    cand.relationships = rels
    cand.relationship = "+".join(rels) if rels else primary
    cand.confidence = confidence

    if len(matches) > 1:
        cand.proposed_action = ACTION_CONFLICT
        cand.blocking_reasons.append(
            "multiple canonical Steam entities match; refusing merge"
        )
        cand.candidate_mod_id = ",".join(_text(r["mod_id"]) for r in matches)
        return cand

    if len(matches) == 1:
        target = matches[0]
        cand.candidate_mod_id = _text(target["mod_id"])
        cand.candidate_platform = normalize_platform(_text(target.get("platform")))
        cand.candidate_external_id = _text(target.get("external_id"))
        cand.candidate_source_url = normalize_source_url(_text(target.get("source_url")))
        cand.candidate_folder = _text(target.get("last_known_path"))
        ghost_dir = Path(folder_path) if folder_path else None
        canon_dir = Path(cand.candidate_folder) if cand.candidate_folder else None
        both_live = bool(
            ghost_dir
            and canon_dir
            and ghost_dir.is_dir()
            and canon_dir.is_dir()
            and ghost_dir.resolve() != canon_dir.resolve()
        )
        if both_live:
            cand.evidence.append("filesystem collision: both folders exist")
        if confidence in (CONF_HIGH, CONF_MEDIUM):
            cand.proposed_action = ACTION_REMOVE_INVALID
            cand.reason = REASON_INVALID_DUPLICATE
            cand.reference_action = "migrate_to_canonical"
            cand.filesystem_action = (
                "quarantine_unique_folder" if both_live else "db_delete_shared_or_missing_folder"
            )
            cand.evidence.append("INVALID_DUPLICATE_MOD")
            cand.evidence.append(
                "confirmed invalid ghost; canonical Steam entity already exists; delete ghost"
            )
            if both_live:
                cand.evidence.append(
                    "quarantine ghost folder; canonical files are not overwritten"
                )
        else:
            cand.proposed_action = ACTION_CONFLICT
            cand.blocking_reasons.append(
                "canonical exists but entity-invalid evidence is too weak"
            )
        return cand

    # No canonical Steam row.
    if folder_path and Path(folder_path).is_dir():
        cand.proposed_action = ACTION_QUARANTINE
        cand.blocking_reasons.append(
            "no safe canonical Steam entity; quarantine leftover, do not mint"
        )
        cand.relationship = "+".join(
            [REL_INTERNAL_POLLUTION, REL_ORPHAN] if pollution else [REL_ORPHAN]
        )
        cand.relationships = [REL_ORPHAN] + (
            [REL_INTERNAL_POLLUTION] if pollution else []
        )
        return cand

    cand.proposed_action = ACTION_CONFLICT if pollution else ACTION_NO_ACTION
    cand.blocking_reasons.append("no canonical match and no leftover folder")
    return cand


def _classify_source_url_row(
    row: dict[str, Any],
    all_rows: list[dict[str, Any]],
) -> RepairCandidate:
    mid = _text(row.get("mod_id"))
    url = _text(row.get("source_url"))
    plat = normalize_platform_if_known(_text(row.get("platform")))
    ext = _text(row.get("external_id"))
    embedded = internal_ids_embedded(url)
    cand = RepairCandidate(
        ghost_mod_id=mid,
        ghost_folder=_text(row.get("last_known_path")),
        ghost_platform=plat or _text(row.get("platform")),
        ghost_external_id=ext,
        ghost_workspace_id=_text(row.get("workspace_id")),
        ghost_source_url=url,
        ghost_app_id=int(row.get("app_id") or 0),
        finding_class="source_url_internal_id",
        relationships=[REL_INTERNAL_POLLUTION],
        relationship=REL_INTERNAL_POLLUTION,
        evidence=[f"source_url embeds internal id(s): {', '.join(embedded)}"],
    )
    workshop = workshop_id_from_url(url)
    clean_ext = sanitize_platform_external_id(
        plat or PLATFORM_STEAM, ext, mod_id=mid
    )
    if workshop:
        cand.evidence.append(f"URL also contains valid workshop id {workshop}")
        own = mid if is_valid_steam_workshop_id(mid) else (
            clean_ext if is_valid_steam_workshop_id(clean_ext) else ""
        )
        # Never MERGE a living entity into another row because its URL mentions
        # a workshop id. Entity existence is decided elsewhere; this is a field.
        if own and workshop != own:
            cand.proposed_action = ACTION_SCRUB_URL
            cand.confidence = CONF_HIGH
            cand.candidate_mod_id = mid
            cand.candidate_platform = plat or PLATFORM_STEAM
            cand.candidate_external_id = own
            cand.candidate_source_url = steam_workshop_url(own)
            cand.evidence.append(
                f"URL points at a different workshop item; rewrite to own {own}"
            )
            return cand
        if own and workshop == own:
            # Valid workshop URL that also embeds an internal token — clear pollution
            # by rewriting to the canonical workshop URL (no internal id).
            cand.proposed_action = ACTION_SCRUB_URL
            cand.confidence = CONF_HIGH
            cand.candidate_mod_id = mid
            cand.candidate_source_url = steam_workshop_url(own)
            cand.evidence.append("rewrite source_url to own workshop URL without internal ids")
            return cand

    if plat == PLATFORM_STEAM or "steamcommunity.com" in url.lower():
        if is_valid_steam_workshop_id(clean_ext) or is_valid_steam_workshop_id(mid):
            replacement = steam_workshop_url(clean_ext or mid)
            cand.proposed_action = ACTION_SCRUB_URL
            cand.confidence = CONF_HIGH
            cand.candidate_mod_id = mid
            cand.candidate_platform = PLATFORM_STEAM
            cand.candidate_external_id = clean_ext or (
                mid if is_valid_steam_workshop_id(mid) else ""
            )
            cand.candidate_source_url = replacement
            cand.evidence.append(
                f"safe URL rewrite to workshop URL of {cand.candidate_external_id}"
            )
            return cand
        cand.proposed_action = ACTION_SCRUB_URL
        cand.confidence = CONF_MEDIUM
        cand.candidate_mod_id = mid
        cand.candidate_source_url = ""
        cand.evidence.append("Steam URL is only the internal id — clear source_url")
        return cand

    # Non-Steam URL that happens to contain a 900… token.
    cand.proposed_action = ACTION_CONFLICT
    cand.confidence = CONF_LOW
    cand.blocking_reasons.append(
        "non-Steam source_url embeds an internal id; refusing blind string replace"
    )
    cand.relationships.append(REL_NO_SAFE_MATCH)
    cand.relationship = REL_NO_SAFE_MATCH
    return cand


def _is_stub_external(row: dict[str, Any]) -> bool:
    ext = _text(row.get("external_id"))
    if not ext or ext.startswith("stub:"):
        return True
    return is_internal_mod_id(ext) or is_provisional_external_id(ext)


def _path_uniquified_with_own_id(row: dict[str, Any]) -> bool:
    path = _text(row.get("last_known_path")).replace("\\", "/").rstrip("/")
    mid = _text(row.get("mod_id"))
    if not path or not mid:
        return False
    return Path(path).name.endswith(f"_{mid}")


def _resolved_folder(row: dict[str, Any]) -> Path | None:
    raw = _text(row.get("last_known_path"))
    if not raw:
        return None
    path = Path(raw)
    try:
        return path.resolve() if path.exists() else path
    except OSError:
        return path


def elect_original_and_invalids(
    items: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Pick the original survivor and the invalid extra rows in a URL group.

    Confirmed business rule: extras are erroneous duplicates. Original identity
    fields are never rewritten. Uniquified ``_<mod_id>`` folders are extras.
    Shared-folder stub rows are extras. Otherwise the later (higher) id is extra.
    """
    uniq: dict[str, dict[str, Any]] = {}
    for row in items:
        uniq[_text(row["mod_id"])] = row
    rows = list(uniq.values())
    if len(rows) < 2:
        return rows[0], []
    remaining = list(rows)
    original: dict[str, Any] | None = None
    uniquified = [r for r in remaining if _path_uniquified_with_own_id(r)]
    not_uni = [r for r in remaining if not _path_uniquified_with_own_id(r)]
    if uniquified and not_uni:
        original = min(not_uni, key=lambda r: int(r["mod_id"]))
        invalids = [r for r in remaining if _text(r["mod_id"]) != _text(original["mod_id"])]
        return original, invalids

    paths = [_resolved_folder(r) for r in remaining]
    same = (
        all(p is not None for p in paths)
        and len({str(p) for p in paths if p is not None}) == 1
    )
    if same:
        official = [r for r in remaining if not _is_stub_external(r)]
        stubs = [r for r in remaining if _is_stub_external(r)]
        if official and stubs:
            original = min(official, key=lambda r: int(r["mod_id"]))
            invalids = [r for r in remaining if _text(r["mod_id"]) != _text(original["mod_id"])]
            return original, invalids

    original = min(remaining, key=lambda r: int(r["mod_id"]))
    invalids = [r for r in remaining if _text(r["mod_id"]) != _text(original["mod_id"])]
    return original, invalids


def _classify_duplicate_url_group(
    url: str,
    items: list[dict[str, Any]],
) -> list[RepairCandidate]:
    """Each confirmed extra entity becomes REMOVE_INVALID_DUPLICATE (not CONFLICT)."""
    ids = [_text(r["mod_id"]) for r in items]
    platforms = {normalize_platform(_text(r.get("platform"))) for r in items}
    if len({i for i in ids if i}) < 2:
        return []
    steam_pks = {
        _text(r["mod_id"])
        for r in items
        if normalize_platform(_text(r.get("platform"))) == PLATFORM_STEAM
        and is_valid_steam_workshop_id(_text(r["mod_id"]))
    }
    if len(steam_pks) >= 2:
        # Distinct living Steam workshop entities sharing a URL: field pollution,
        # not duplicate entities.
        return []
    official_exts = {
        _text(r.get("external_id"))
        for r in items
        if not _is_stub_external(r) and _text(r.get("external_id"))
    }
    if len(official_exts) >= 2:
        return []
    if len(platforms) > 1:
        cand = RepairCandidate(
            ghost_mod_id=",".join(ids),
            ghost_source_url=url,
            finding_class="duplicate_source_url",
            evidence=[f"source_url shared by {len(ids)} mod_ids: {', '.join(ids)}"],
            confidence=CONF_LOW,
            relationship=REL_AMBIGUOUS,
            relationships=[REL_AMBIGUOUS],
            proposed_action=ACTION_CONFLICT,
            blocking_reasons=["duplicate URL spans disagreeing platforms"],
        )
        return [cand]

    original, invalids = elect_original_and_invalids(items)
    out: list[RepairCandidate] = []
    for inv in invalids:
        orig_path = _text(original.get("last_known_path"))
        inv_path = _text(inv.get("last_known_path"))
        shared = False
        if orig_path and inv_path:
            try:
                shared = Path(orig_path).resolve() == Path(inv_path).resolve()
            except OSError:
                shared = orig_path.replace("\\", "/") == inv_path.replace("\\", "/")
        cand = RepairCandidate(
            ghost_mod_id=_text(inv["mod_id"]),
            ghost_folder=inv_path,
            ghost_platform=_text(inv.get("platform")),
            ghost_external_id=_text(inv.get("external_id")),
            ghost_workspace_id=_text(inv.get("workspace_id")),
            ghost_source_url=url,
            ghost_app_id=int(inv.get("app_id") or 0),
            ghost_title=_text(inv.get("title")),
            candidate_mod_id=_text(original["mod_id"]),
            candidate_platform=_text(original.get("platform")),
            candidate_external_id=_text(original.get("external_id")),
            candidate_source_url=normalize_source_url(
                _text(original.get("source_url")) or url
            ),
            candidate_folder=orig_path,
            finding_class="duplicate_source_url",
            relationship=REL_DUPLICATE_ENTITY,
            relationships=[REL_DUPLICATE_ENTITY],
            confidence=CONF_HIGH,
            proposed_action=ACTION_REMOVE_INVALID,
            reason=REASON_INVALID_DUPLICATE,
            filesystem_action=(
                "db_delete_shared_folder" if shared else "quarantine_unique_folder"
            ),
            reference_action="migrate_to_canonical",
            evidence=[
                f"confirmed invalid duplicate of original {_text(original['mod_id'])}",
                f"source_url={url}",
                "INVALID_DUPLICATE_MOD",
                "shared_folder" if shared else "distinct_folder",
            ],
        )
        out.append(cand)
    return out


_METADATA_ONLY_DIRS = {INFO_DIR_NAME, "info", "历史版本"}


def _folder_is_metadata_only(folder: Path | None) -> bool:
    if folder is None or not folder.is_dir():
        return False
    has_any = False
    for child in folder.rglob("*"):
        if not child.is_file():
            continue
        has_any = True
        try:
            rel = child.relative_to(folder)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in _METADATA_ONLY_DIRS:
            continue
        return False
    return has_any


def _remove_candidate_from_pair(
    invalid: dict[str, Any],
    original: dict[str, Any],
    *,
    finding_class: str,
    extra_evidence: list[str] | None = None,
) -> RepairCandidate:
    inv_path = _text(invalid.get("last_known_path"))
    orig_path = _text(original.get("last_known_path"))
    shared = False
    if inv_path and orig_path:
        try:
            shared = Path(inv_path).resolve() == Path(orig_path).resolve()
        except OSError:
            shared = inv_path.replace("\\", "/") == orig_path.replace("\\", "/")
    meta_only = _folder_is_metadata_only(Path(inv_path) if inv_path else None)
    evidence = [
        f"confirmed invalid duplicate of original {_text(original['mod_id'])}",
        "INVALID_DUPLICATE_MOD",
        *(extra_evidence or []),
    ]
    if meta_only:
        evidence.append("invalid folder is metadata-only (.info); no independent payload")
    return RepairCandidate(
        ghost_mod_id=_text(invalid["mod_id"]),
        ghost_folder=inv_path,
        ghost_platform=_text(invalid.get("platform")),
        ghost_external_id=_text(invalid.get("external_id")),
        ghost_workspace_id=_text(invalid.get("workspace_id")),
        ghost_source_url=_text(invalid.get("source_url")),
        ghost_app_id=int(invalid.get("app_id") or 0),
        ghost_title=_text(invalid.get("title") or invalid.get("display_name")),
        candidate_mod_id=_text(original["mod_id"]),
        candidate_platform=_text(original.get("platform")),
        candidate_external_id=_text(original.get("external_id")),
        candidate_source_url=normalize_source_url(_text(original.get("source_url"))),
        candidate_folder=orig_path,
        finding_class=finding_class,
        relationship=REL_DUPLICATE_ENTITY,
        relationships=[REL_DUPLICATE_ENTITY],
        confidence=CONF_HIGH,
        proposed_action=ACTION_REMOVE_INVALID,
        reason=REASON_INVALID_DUPLICATE,
        filesystem_action=(
            "db_delete_shared_folder" if shared else "quarantine_unique_folder"
        ),
        reference_action="migrate_to_canonical",
        evidence=evidence,
    )


def _official_identity_key(row: dict[str, Any]) -> tuple[str, int, str] | None:
    plat = normalize_platform(_text(row.get("platform")))
    ext = _text(row.get("external_id"))
    if _is_stub_external(row):
        mid = _text(row.get("mod_id"))
        if plat == PLATFORM_STEAM and is_valid_steam_workshop_id(mid):
            ext = mid
        else:
            return None
    if not plat or not ext:
        return None
    app_id = int(row.get("app_id") or 0)
    return (plat, app_id, ext.lower())


def _classify_official_identity_duplicates(
    rows: list[dict[str, Any]],
) -> list[RepairCandidate]:
    groups: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = _official_identity_key(row)
        if key:
            groups[key].append(row)
    out: list[RepairCandidate] = []
    for key, items in groups.items():
        unique = {_text(r["mod_id"]) for r in items}
        if len(unique) < 2:
            continue
        original, invalids = elect_original_and_invalids(items)
        for inv in invalids:
            out.append(
                _remove_candidate_from_pair(
                    inv,
                    original,
                    finding_class="duplicate_external_identity",
                    extra_evidence=[
                        f"shared official identity platform={key[0]} app_id={key[1]} external_id={key[2]}"
                    ],
                )
            )
    return out


def _classify_shared_path_stubs(rows: list[dict[str, Any]]) -> list[RepairCandidate]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        path = _resolved_folder(row)
        if path is None:
            continue
        groups[str(path)].append(row)
    out: list[RepairCandidate] = []
    for items in groups.values():
        unique = {_text(r["mod_id"]) for r in items}
        if len(unique) < 2:
            continue
        official = [r for r in items if not _is_stub_external(r)]
        stubs = [r for r in items if _is_stub_external(r)]
        if not official or not stubs:
            continue
        original = min(official, key=lambda r: int(r["mod_id"]))
        for inv in stubs:
            if _text(inv["mod_id"]) == _text(original["mod_id"]):
                continue
            out.append(
                _remove_candidate_from_pair(
                    inv,
                    original,
                    finding_class="shared_folder_stub",
                    extra_evidence=["stub row shares canonical folder; no independent entity"],
                )
            )
    return out


def _classify_unknown_mod_placeholders(
    rows: list[dict[str, Any]],
) -> list[RepairCandidate]:
    by_id = {_text(r["mod_id"]): r for r in rows}
    out: list[RepairCandidate] = []
    for row in rows:
        mid = _text(row.get("mod_id"))
        if not is_internal_mod_id(mid):
            continue
        folder = _text(row.get("last_known_path"))
        wid = supporting_workshop_id_from_folder(folder)
        if not wid:
            continue
        matches = _canonical_steam_matches(rows, workshop_id=wid, exclude_mod_id=mid)
        if len(matches) != 1:
            continue
        original = matches[0]
        if _text(original["mod_id"]) not in by_id:
            continue
        out.append(
            _remove_candidate_from_pair(
                row,
                original,
                finding_class="unknown_mod_placeholder",
                extra_evidence=[
                    f"Unknown Mod leftover folder for workshop {wid}",
                    "canonical Steam entity already exists",
                ],
            )
        )
    return out


def _planned_ids(plan: RepairPlan) -> set[str]:
    return {
        c.ghost_mod_id
        for c in plan.candidates
        if c.ghost_mod_id.isdigit()
    }


def summarize_sqlite_findings(db: Any) -> dict[str, int]:
    rows = _load_mod_rows(db)
    steam_internal = 0
    workspace_internal = 0
    url_internal = 0
    by_url: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        mid = _text(row["mod_id"])
        plat = normalize_platform(_text(row.get("platform")))
        if plat == PLATFORM_STEAM and is_internal_mod_id(mid):
            steam_internal += 1
        if is_internal_mod_id(mid) and _text(row.get("workspace_id")) == mid:
            workspace_internal += 1
        url = _text(row.get("source_url"))
        if url and internal_ids_embedded(url):
            url_internal += 1
        nu = normalize_source_url(url)
        if nu:
            by_url[nu].add(mid)
    dup_groups = sum(1 for mids in by_url.values() if len(mids) > 1)
    critical = steam_internal + workspace_internal + url_internal
    return {
        "CRITICAL": critical,
        "steam_internal": steam_internal,
        "workspace_internal": workspace_internal,
        "source_url_internal": url_internal,
        "duplicate_source_url_groups": dup_groups,
        "HIGH": dup_groups,
        "INFO": 0,
        "rows": len(rows),
    }


def plan_identity_repair(
    db: Any,
    library_root: str | Path,
) -> RepairPlan:
    """Classify every suspicious identity. Never mutates."""
    root = Path(library_root)
    plan = RepairPlan(before=summarize_sqlite_findings(db))
    rows = _load_mod_rows(db)
    sidecar_index = _index_sidecars(root)
    seen_ghosts: set[str] = set()

    for row in rows:
        mid = _text(row["mod_id"])
        plat = normalize_platform(_text(row.get("platform")))
        if not (is_internal_mod_id(mid) and plat == PLATFORM_STEAM):
            continue
        folder, meta = _sidecar_for_row(row, sidecar_index)
        graph = collect_reference_graph(
            db,
            mid,
            library_root=root,
            last_known_path=_text(row.get("last_known_path")),
            cover_path=_text(row.get("cover_path")),
            sidecar_folders=[folder] if folder else None,
        )
        cand = _classify_steam_ghost(
            row, rows, folder=folder, meta=meta, graph=graph
        )
        plan.candidates.append(cand)
        seen_ghosts.add(mid)

    for row in rows:
        url = _text(row.get("source_url"))
        if not url or not internal_ids_embedded(url):
            continue
        cand = _classify_source_url_row(row, rows)
        # If this row is already a steam ghost merge, keep URL as evidence only
        # unless the ghost classifier did not already propose a merge.
        existing = next(
            (c for c in plan.candidates if c.ghost_mod_id == cand.ghost_mod_id),
            None,
        )
        if existing and existing.proposed_action in (
            ACTION_MERGE,
            ACTION_REMOVE_INVALID,
        ):
            existing.evidence.extend(cand.evidence)
            continue
        plan.candidates.append(cand)

    by_url: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        url = normalize_source_url(_text(row.get("source_url")))
        if url:
            by_url[url].append(row)
    for url, items in by_url.items():
        unique = {_text(i["mod_id"]) for i in items}
        if len(unique) < 2:
            continue
        for grouped in _classify_duplicate_url_group(url, items):
            duplicate_planned = any(
                c.ghost_mod_id == grouped.ghost_mod_id
                and c.proposed_action == grouped.proposed_action
                for c in plan.candidates
            )
            if duplicate_planned:
                continue
            if grouped.ghost_mod_id.isdigit():
                sidecar = (
                    [Path(grouped.ghost_folder)] if grouped.ghost_folder else None
                )
                grouped.references = collect_reference_graph(
                    db,
                    grouped.ghost_mod_id,
                    library_root=root,
                    last_known_path=grouped.ghost_folder,
                    sidecar_folders=sidecar,
                ).to_dict()
            plan.candidates.append(grouped)

    already = _planned_ids(plan)
    extra_classes = (
        _classify_official_identity_duplicates(rows)
        + _classify_shared_path_stubs(rows)
        + _classify_unknown_mod_placeholders(rows)
    )
    extra_added = 0
    for grouped in extra_classes:
        if grouped.ghost_mod_id in already:
            continue
        if grouped.ghost_mod_id.isdigit():
            sidecar = [Path(grouped.ghost_folder)] if grouped.ghost_folder else None
            grouped.references = collect_reference_graph(
                db,
                grouped.ghost_mod_id,
                library_root=root,
                last_known_path=grouped.ghost_folder,
                sidecar_folders=sidecar,
            ).to_dict()
        plan.candidates.append(grouped)
        already.add(grouped.ghost_mod_id)
        extra_added += 1

    removes = [c for c in plan.candidates if c.proposed_action == ACTION_REMOVE_INVALID]
    scrubs = [c for c in plan.candidates if c.proposed_action == ACTION_SCRUB_URL]
    conflicts = [c for c in plan.candidates if c.proposed_action == ACTION_CONFLICT]
    plan.forensics = {
        "INVALID_ENTITY_FORENSICS": True,
        "remove_invalid_duplicate": len(removes),
        "scrub_polluting_source_url": len(scrubs),
        "identity_conflict": len(conflicts),
        "extra_invalid_entities_beyond_url_and_ghost": extra_added,
        "finding_classes": sorted({c.finding_class for c in removes}),
    }
    plan.notes.append(f"planned_candidates={len(plan.candidates)}")
    plan.notes.append(f"invalid_entities_to_remove={len(removes)}")
    return plan


def format_repair_plan(plan: RepairPlan) -> str:
    lines: list[str] = []
    before = plan.before
    lines.append("Identity repair plan (read-only)")
    lines.append(f"Total findings: {len(plan.candidates)}")
    lines.append(
        f"CRITICAL: {before.get('CRITICAL', 0)}  "
        f"HIGH: {before.get('HIGH', 0)}  "
        f"INFO: {before.get('INFO', 0)}"
    )
    lines.append(
        f"  steam+internal={before.get('steam_internal', 0)} "
        f"workspace==internal={before.get('workspace_internal', 0)} "
        f"source_url internal={before.get('source_url_internal', 0)} "
        f"duplicate URL groups={before.get('duplicate_source_url_groups', 0)}"
    )
    amb = [
        c
        for c in plan.candidates
        if c.relationship == REL_AMBIGUOUS or REL_AMBIGUOUS in c.relationships
    ]
    unresolved = [
        c
        for c in plan.candidates
        if c.proposed_action in (ACTION_NO_ACTION, ACTION_CONFLICT, ACTION_QUARANTINE)
        and c not in amb
    ]
    removes = [c for c in plan.candidates if c.proposed_action == ACTION_REMOVE_INVALID]
    lines.append(f"Repair candidates: {len(plan.candidates)}")
    lines.append(f"Invalid duplicates to remove: {len(removes)}")
    lines.append(f"Polluting URL scrubs: {len([c for c in plan.candidates if c.proposed_action == ACTION_SCRUB_URL])}")
    lines.append(f"Identity conflicts: {len([c for c in plan.candidates if c.proposed_action == ACTION_CONFLICT])}")
    lines.append(f"Ambiguous cases: {len(amb)}")
    lines.append(f"Unresolved cases: {len(unresolved)}")
    lines.append("")
    for cand in plan.candidates:
        lines.append("-" * 60)
        lines.append(f"class: {cand.finding_class}")
        lines.append(f"ghost: {cand.ghost_mod_id}  folder={cand.ghost_folder}")
        lines.append(
            f"  platform={cand.ghost_platform} external_id={cand.ghost_external_id} "
            f"workspace_id={cand.ghost_workspace_id}"
        )
        lines.append(f"  source_url={cand.ghost_source_url}")
        lines.append(f"  published_file_id={cand.ghost_published_file_id}")
        lines.append(
            f"canonical candidate: {cand.candidate_mod_id} "
            f"external_id={cand.candidate_external_id}"
        )
        lines.append(f"evidence: {'; '.join(cand.evidence) or '(none)'}")
        lines.append(f"relationship: {cand.relationship}  confidence: {cand.confidence}")
        lines.append(f"proposed action: {cand.proposed_action}")
        if cand.filesystem_action:
            lines.append(f"filesystem action: {cand.filesystem_action}")
        if cand.reference_action:
            lines.append(f"reference action: {cand.reference_action}")
        if cand.blocking_reasons:
            lines.append(f"blocking: {'; '.join(cand.blocking_reasons)}")
        refs = cand.references or {}
        if refs.get("tables"):
            lines.append(f"  references tables: {refs['tables']}")
        if refs.get("filesystem"):
            lines.append(f"  filesystem: {refs['filesystem']}")
        if refs.get("sidecar"):
            lines.append(f"  sidecar: {refs['sidecar']}")
        if refs.get("backup"):
            lines.append(f"  backup: {refs['backup']}")
    return "\n".join(lines) + "\n"


def _ensure_repair_audit_table(db: Any) -> None:
    with db._lock:  # noqa: SLF001
        db._conn.execute(REPAIR_AUDIT_DDL)  # noqa: SLF001


def _write_repair_audit(db: Any, cand: RepairCandidate, *, operation: str, after: dict[str, Any]) -> None:
    _ensure_repair_audit_table(db)
    payload_before = {
        "mod_id": cand.ghost_mod_id,
        "platform": cand.ghost_platform,
        "external_id": cand.ghost_external_id,
        "workspace_id": cand.ghost_workspace_id,
        "source_url": cand.ghost_source_url,
        "published_file_id": cand.ghost_published_file_id,
        "folder": cand.ghost_folder,
    }
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            """
            INSERT INTO identity_repair_audit (
                created_at, operation, ghost_mod_id, canonical_mod_id,
                platform, external_id, app_id, relationship, confidence,
                action, reason, before_state, after_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now(),
                operation,
                cand.ghost_mod_id,
                cand.candidate_mod_id,
                cand.ghost_platform,
                cand.candidate_external_id or cand.ghost_external_id,
                int(cand.ghost_app_id or 0),
                cand.relationship,
                cand.confidence,
                cand.proposed_action,
                (
                    REASON_INVALID_DUPLICATE
                    if cand.proposed_action == ACTION_REMOVE_INVALID
                    else "; ".join(cand.evidence + cand.blocking_reasons)
                ),
                json.dumps(payload_before, ensure_ascii=False),
                json.dumps(after, ensure_ascii=False),
            ),
        )
    log_identity_mutation(
        db,
        mod_id=cand.ghost_mod_id or cand.candidate_mod_id,
        field_name="identity_repair",
        old_value=cand.ghost_mod_id,
        new_value=cand.candidate_mod_id,
        source="identity_repair",
        reason=f"{operation}:{cand.proposed_action}",
        commit=False,
    )


def _quarantine_path(src: Path, dest_root: Path) -> Path:
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / src.name
    if dest.exists():
        index = 2
        while True:
            alt = dest_root / f"{src.name} ({index})"
            if not alt.exists():
                dest = alt
                break
            index += 1
    shutil.move(str(src), str(dest))
    return dest


def _migrate_int_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    *,
    src: int,
    dst: int,
) -> int:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        return 0
    unique = False
    try:
        for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
            if not int(idx[2] or 0):
                continue
            info = conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()
            names = [i[2] for i in info]
            if column in names:
                unique = True
                break
    except Exception:  # noqa: BLE001
        unique = False
    moved = 0
    if table == "deployment_record_items" and column == "mod_id":
        rows = conn.execute(
            "SELECT record_id FROM deployment_record_items WHERE mod_id = ?",
            (src,),
        ).fetchall()
        for row in rows:
            rid = int(row[0] if not isinstance(row, sqlite3.Row) else row["record_id"])
            exists = conn.execute(
                "SELECT 1 FROM deployment_record_items WHERE record_id=? AND mod_id=?",
                (rid, dst),
            ).fetchone()
            if exists:
                conn.execute(
                    "DELETE FROM deployment_record_items WHERE record_id=? AND mod_id=?",
                    (rid, src),
                )
            else:
                conn.execute(
                    "UPDATE deployment_record_items SET mod_id=? WHERE record_id=? AND mod_id=?",
                    (dst, rid, src),
                )
            moved += 1
        return moved
    if unique:
        conn.execute(
            f'DELETE FROM "{table}" WHERE CAST("{column}" AS TEXT) = ?',
            (str(src),),
        )
        return 0
    cur = conn.execute(
        f'UPDATE "{table}" SET "{column}" = ? WHERE CAST("{column}" AS TEXT) = ?',
        (dst, str(src)),
    )
    return int(cur.rowcount or 0)


def _delete_ghost_mod_row(conn: sqlite3.Connection, ghost: int) -> None:
    conn.execute("DELETE FROM mods WHERE mod_id = ?", (ghost,))


def _delete_invalid_mod_row(conn: sqlite3.Connection, invalid_id: int) -> None:
    conn.execute("DELETE FROM mods WHERE mod_id = ?", (invalid_id,))


def _count_dangling_mod_id(db: Any, mod_id: str) -> int:
    total = 0
    mid = _text(mod_id)
    for table in _table_names(db):
        if table in {"identity_audit_log", "identity_repair_audit"}:
            continue
        for col in _table_columns(db, table):
            if col.lower() not in _MOD_ID_COLUMNS:
                continue
            rows = _rows_as_dicts(
                db,
                f'SELECT COUNT(*) AS n FROM "{table}" WHERE CAST("{col}" AS TEXT) = ?',
                (mid,),
            )
            total += int(rows[0]["n"] or 0) if rows else 0
    return total


def _clear_mod_id_references(conn: sqlite3.Connection, db: Any, invalid_id: int) -> None:
    for table in _table_names(db):
        if table in {"mods", "identity_audit_log", "identity_repair_audit"}:
            continue
        for col in _table_columns(db, table):
            if col.lower() not in _MOD_ID_COLUMNS:
                continue
            conn.execute(
                f'DELETE FROM "{table}" WHERE CAST("{col}" AS TEXT) = ?',
                (str(invalid_id),),
            )


def _rewire_mod_id_references(
    conn: sqlite3.Connection,
    db: Any,
    invalid_id: int,
    canonical_id: int,
) -> str:
    """Point living FKs at canonical, otherwise delete the invalid id."""
    migrated = False
    for table in _table_names(db):
        if table in {"mods", "identity_audit_log", "identity_repair_audit"}:
            continue
        for col in _table_columns(db, table):
            if col.lower() not in _MOD_ID_COLUMNS:
                continue
            try:
                moved = _migrate_int_column(
                    conn, table, col, src=invalid_id, dst=canonical_id
                )
                if moved:
                    migrated = True
            except sqlite3.IntegrityError:
                conn.execute(
                    f'DELETE FROM "{table}" WHERE CAST("{col}" AS TEXT) = ?',
                    (str(invalid_id),),
                )
                migrated = True
    leftover = 0
    for table in _table_names(db):
        if table in {"mods", "identity_audit_log", "identity_repair_audit"}:
            continue
        for col in _table_columns(db, table):
            if col.lower() not in _MOD_ID_COLUMNS:
                continue
            rows = conn.execute(
                f'SELECT COUNT(*) AS n FROM "{table}" WHERE CAST("{col}" AS TEXT) = ?',
                (str(invalid_id),),
            ).fetchone()
            leftover += int(rows["n"] or 0) if rows else 0
    if leftover:
        _clear_mod_id_references(conn, db, invalid_id)
    return "migrate_to_canonical" if migrated else "clear_invalid_references"


def _rewrite_canonical_cover_if_points_at_invalid(
    conn: sqlite3.Connection,
    *,
    canonical_id: int,
    invalid_folder: Path | None,
    canon_folder: Path | None,
) -> str:
    if invalid_folder is None:
        return ""
    row = conn.execute(
        "SELECT cover_path FROM mods WHERE mod_id = ?",
        (canonical_id,),
    ).fetchone()
    if row is None:
        return ""
    cover = _text(row["cover_path"])
    if not cover:
        return ""
    try:
        cover_p = Path(cover).resolve()
        ghost_r = invalid_folder.resolve()
        dangling = cover_p == ghost_r or ghost_r in cover_p.parents
    except OSError:
        dangling = str(invalid_folder) in cover
    if not dangling:
        return ""
    replacement = ""
    if canon_folder and canon_folder.is_dir():
        info_dir = canon_folder / INFO_DIR_NAME
        for name in ("cover.jpg", "cover.png", "cover.webp", "preview.jpg", "preview.png"):
            candidate = info_dir / name
            if candidate.is_file():
                replacement = str(candidate)
                break
    conn.execute(
        "UPDATE mods SET cover_path = ? WHERE mod_id = ?",
        (replacement, canonical_id),
    )
    return replacement or "(cleared dangling ghost path)"
    for table in _table_names(db):
        if table in {"mods", "identity_audit_log", "identity_repair_audit"}:
            continue
        for col in _table_columns(db, table):
            if col.lower() not in _MOD_ID_COLUMNS:
                continue
            conn.execute(
                f'DELETE FROM "{table}" WHERE CAST("{col}" AS TEXT) = ?',
                (str(invalid_id),),
            )


def _snapshot_identity(conn: sqlite3.Connection, mod_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT mod_id, platform, external_id, workspace_id, source_url,
               display_name, last_known_path
        FROM mods WHERE mod_id = ?
        """,
        (mod_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"original {mod_id} missing")
    return {k: row[k] for k in row.keys()}


def _validate_remove_invalid(
    db: Any,
    *,
    invalid_id: int,
    original_id: int,
    before_original: dict[str, Any],
) -> None:
    dangling = _count_dangling_mod_id(db, str(invalid_id))
    if dangling:
        raise RuntimeError(f"dangling references remain for {invalid_id}: {dangling}")
    after = _snapshot_identity(db._conn, original_id)  # noqa: SLF001
    for key in (
        "mod_id",
        "platform",
        "external_id",
        "workspace_id",
        "source_url",
        "display_name",
        "last_known_path",
    ):
        if _text(after.get(key)) != _text(before_original.get(key)):
            raise RuntimeError(
                f"original {original_id} {key} changed during invalid delete"
            )


def _apply_merge(
    db: Any,
    cand: RepairCandidate,
    *,
    quarantine_root: Path,
) -> dict[str, Any]:
    ghost = int(cand.ghost_mod_id)
    canonical = int(cand.candidate_mod_id)
    if ghost == canonical:
        raise RuntimeError("merge refused: ghost equals canonical")
    if is_internal_mod_id(canonical) and is_valid_steam_workshop_id(cand.ghost_mod_id):
        raise RuntimeError("merge refused: would invert canonical toward internal id")
    after: dict[str, Any] = {"merged": True, "quarantined": []}
    conn: sqlite3.Connection = db._conn  # noqa: SLF001
    canon_folder = Path(cand.candidate_folder) if cand.candidate_folder else None
    conn.execute("SAVEPOINT identity_repair_merge")
    try:
        canon = conn.execute(
            """
            SELECT mod_id, platform, external_id, workspace_id, source_url,
                   app_id, last_known_path, user_notes, custom_description,
                   favorite, display_name, cover_path
            FROM mods WHERE mod_id = ?
            """,
            (canonical,),
        ).fetchone()
        if canon is None:
            raise RuntimeError(f"canonical {canonical} missing; refusing merge")
        canon_ext = _text(canon["external_id"] if not isinstance(canon, dict) else canon.get("external_id"))
        if is_internal_mod_id(canon_ext):
            raise RuntimeError("canonical external_id is already polluted; refusing merge")

        ghost_row = conn.execute(
            "SELECT user_notes, custom_description, favorite, display_name FROM mods WHERE mod_id = ?",
            (ghost,),
        ).fetchone()
        if ghost_row is not None:
            g_notes = _text(ghost_row["user_notes"])
            g_desc = _text(ghost_row["custom_description"])
            g_disp = _text(ghost_row["display_name"])
            g_fav = int(ghost_row["favorite"] or 0)
            c_notes = _text(canon["user_notes"])
            c_desc = _text(canon["custom_description"])
            c_disp = _text(canon["display_name"])
            c_fav = int(canon["favorite"] or 0)
            sets: list[str] = []
            params: list[Any] = []
            if g_notes and not c_notes:
                sets.append("user_notes = ?")
                params.append(g_notes)
            if g_desc and not c_desc:
                sets.append("custom_description = ?")
                params.append(g_desc)
            if g_disp and not c_disp and not _UNKNOWN_MOD_RE.match(g_disp.replace(" ", "_")):
                sets.append("display_name = ?")
                params.append(g_disp)
            if g_fav and not c_fav:
                sets.append("favorite = ?")
                params.append(g_fav)
            if sets:
                params.append(canonical)
                conn.execute(
                    f"UPDATE mods SET {', '.join(sets)} WHERE mod_id = ?",
                    params,
                )

        ghost_folder_pre = Path(cand.ghost_folder) if cand.ghost_folder else None
        cover = _text(canon["cover_path"])
        if ghost_folder_pre and cover:
            try:
                cover_p = Path(cover).resolve()
                ghost_r = ghost_folder_pre.resolve()
                dangling = cover_p == ghost_r or ghost_r in cover_p.parents
            except OSError:
                dangling = str(ghost_folder_pre) in cover
            if dangling:
                replacement = ""
                if canon_folder and canon_folder.is_dir():
                    info_dir = canon_folder / INFO_DIR_NAME
                    for name in ("cover.jpg", "cover.png", "cover.webp", "preview.jpg", "preview.png"):
                        candidate = info_dir / name
                        if candidate.is_file():
                            replacement = str(candidate)
                            break
                conn.execute(
                    "UPDATE mods SET cover_path = ? WHERE mod_id = ?",
                    (replacement, canonical),
                )
                after["cover_path_rewritten"] = replacement or "(cleared dangling ghost path)"

        for table in _table_names(db):
            if table in {"mods", "identity_audit_log", "identity_repair_audit"}:
                continue
            for col in _table_columns(db, table):
                if col.lower() not in _MOD_ID_COLUMNS:
                    continue
                try:
                    _migrate_int_column(conn, table, col, src=ghost, dst=canonical)
                except sqlite3.IntegrityError:
                    conn.execute(
                        f'DELETE FROM "{table}" WHERE CAST("{col}" AS TEXT) = ?',
                        (str(ghost),),
                    )

        _delete_ghost_mod_row(conn, ghost)

        check = conn.execute(
            """
            SELECT platform, external_id, workspace_id, source_url
            FROM mods WHERE mod_id = ?
            """,
            (canonical,),
        ).fetchone()
        if check is None:
            raise RuntimeError("canonical disappeared during merge")
        ext_now = _text(check["external_id"])
        ws_now = _text(check["workspace_id"])
        url_now = _text(check["source_url"])
        if ext_now == str(ghost) or is_internal_mod_id(ext_now):
            raise RuntimeError("merge refused: canonical external_id became internal")
        if ws_now == str(ghost):
            raise RuntimeError("merge refused: canonical workspace_id became ghost id")
        if str(ghost) in url_now:
            raise RuntimeError("merge refused: canonical source_url inherited ghost id")

        ghost_folder = Path(cand.ghost_folder) if cand.ghost_folder else None
        canon_folder = Path(cand.candidate_folder) if cand.candidate_folder else None
        if ghost_folder and ghost_folder.is_dir():
            same = bool(
                canon_folder
                and canon_folder.is_dir()
                and ghost_folder.resolve() == canon_folder.resolve()
            )
            if not same:
                dest = _quarantine_path(
                    ghost_folder, quarantine_root / f"ghost_{ghost}"
                )
                after["quarantined"].append(str(dest))
                (dest.parent / "MANIFEST.json").write_text(
                    json.dumps(
                        {
                            "ghost_mod_id": str(ghost),
                            "canonical_mod_id": str(canonical),
                            "original_path": str(ghost_folder),
                            "quarantined_at": _utc_now(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

        backup = data_dir() / "mod_backup" / str(ghost)
        if backup.exists():
            dest = _quarantine_path(backup, quarantine_root / f"backup_{ghost}")
            after["quarantined"].append(str(dest))

        conn.execute("RELEASE SAVEPOINT identity_repair_merge")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT identity_repair_merge")
        conn.execute("RELEASE SAVEPOINT identity_repair_merge")
        raise

    if canon_folder and canon_folder.is_dir():
        try:
            meta = read_info_metadata_dict(canon_folder) or {}
            plat = _text(canon["platform"])
            ext = _text(canon["external_id"])
            pub = sidecar_published_file_id(
                mod_id=canonical, platform=plat, external_id=ext
            )
            if pub:
                meta["published_file_id"] = pub
            if is_internal_mod_id(_text(meta.get("published_file_id"))):
                meta["published_file_id"] = pub or ""
            persist_unified_metadata_dict(
                canon_folder, meta, sync_backup=False, sync_reason="identity_repair"
            )
            after["sidecar_persist_ok"] = True
        except Exception as exc:  # noqa: BLE001
            after["sidecar_persist_ok"] = False
            after["sidecar_persist_error"] = str(exc)
            after["recoverable_state"] = (
                "canonical sidecar persist failed after merge; "
                "DB identity is canonical; retry sidecar write"
            )
    return after


def _apply_quarantine_only(
    db: Any,
    cand: RepairCandidate,
    *,
    quarantine_root: Path,
) -> dict[str, Any]:
    after: dict[str, Any] = {"quarantined": []}
    folder = Path(cand.ghost_folder) if cand.ghost_folder else None
    if folder and folder.is_dir():
        dest = _quarantine_path(
            folder, quarantine_root / f"ghost_{cand.ghost_mod_id}"
        )
        after["quarantined"].append(str(dest))
        (dest.parent / "MANIFEST.json").write_text(
            json.dumps(
                {
                    "ghost_mod_id": cand.ghost_mod_id,
                    "original_path": str(folder),
                    "quarantined_at": _utc_now(),
                    "reason": "no_safe_canonical",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    if cand.ghost_mod_id.isdigit():
        with db._lock:  # noqa: SLF001
            db._conn.execute(  # noqa: SLF001
                """
                UPDATE mods SET folder_present = 0, library_status = ?,
                       conflict_status = ?, conflict_note = ?, updated_at = ?
                WHERE mod_id = ?
                """,
                (
                    "IDENTITY_UNRESOLVED",
                    "identity_conflict",
                    "quarantined leftover; no canonical mint",
                    _utc_now(),
                    int(cand.ghost_mod_id),
                ),
            )
    return after


def _apply_remove_invalid_duplicate(
    db: Any,
    cand: RepairCandidate,
    *,
    quarantine_root: Path,
    repair_run_id: str,
) -> dict[str, Any]:
    invalid_id = int(cand.ghost_mod_id)
    original_id = int(cand.candidate_mod_id)
    if invalid_id == original_id:
        raise RuntimeError("refusing to delete original as invalid duplicate")
    conn: sqlite3.Connection = db._conn  # noqa: SLF001
    now = _utc_now()
    dest_dir = quarantine_root / f"invalid_{invalid_id}"
    after: dict[str, Any] = {
        "removed": True,
        "quarantined": [],
        "shared_folder": False,
        "repair_run_id": repair_run_id,
        "reason": REASON_INVALID_DUPLICATE,
        "action": ACTION_REMOVE_INVALID,
        "invalid_mod_id": str(invalid_id),
        "external_identity": cand.ghost_external_id,
        "platform": cand.ghost_platform,
        "app_id": int(cand.ghost_app_id or 0),
        "source_url": cand.ghost_source_url,
        "original_path": cand.ghost_folder,
        "quarantine_path": "",
        "timestamp": now,
        "note": "该实体是已确认的无效重复 Mod，因此被删除。",
    }
    moved: tuple[Path, Path] | None = None
    with db._lock:  # noqa: SLF001
        conn.execute("SAVEPOINT identity_repair_remove_invalid")
        try:
            before_original = _snapshot_identity(conn, original_id)
            inv_row = conn.execute(
                "SELECT backup_metadata_json FROM mods WHERE mod_id = ?",
                (invalid_id,),
            ).fetchone()
            backup_meta = ""
            if inv_row is not None:
                try:
                    backup_meta = _text(inv_row["backup_metadata_json"])
                except (KeyError, IndexError, TypeError):
                    backup_meta = ""
            inv_folder = Path(cand.ghost_folder) if cand.ghost_folder else None
            orig_folder = Path(cand.candidate_folder) if cand.candidate_folder else None
            shared = False
            if inv_folder and orig_folder:
                try:
                    shared = (
                        inv_folder.exists()
                        and orig_folder.exists()
                        and inv_folder.resolve() == orig_folder.resolve()
                    )
                except OSError:
                    shared = str(inv_folder) == str(orig_folder)
            after["shared_folder"] = shared

            dest_dir.mkdir(parents=True, exist_ok=True)
            quarantine_path = dest_dir
            if inv_folder and inv_folder.is_dir() and not shared:
                dest = _quarantine_path(inv_folder, dest_dir)
                moved = (inv_folder, dest)
                after["quarantined"].append(str(dest))
                quarantine_path = dest
                backup = data_dir() / "mod_backup" / str(invalid_id)
                if backup.exists():
                    bdest = _quarantine_path(
                        backup, quarantine_root / f"invalid_backup_{invalid_id}"
                    )
                    after["quarantined"].append(str(bdest))
            after["quarantine_path"] = str(quarantine_path)
            (dest_dir / "MANIFEST.json").write_text(
                json.dumps(
                    {
                        "original_path": str(inv_folder) if inv_folder else "",
                        "quarantine_path": str(quarantine_path),
                        "mod_id": str(invalid_id),
                        "invalid_mod_id": str(invalid_id),
                        "canonical_mod_id": str(original_id),
                        "external_id": cand.ghost_external_id,
                        "external_identity": cand.ghost_external_id,
                        "platform": cand.ghost_platform,
                        "app_id": int(cand.ghost_app_id or 0),
                        "source_url": cand.ghost_source_url,
                        "reason": REASON_INVALID_DUPLICATE,
                        "action": ACTION_REMOVE_INVALID,
                        "timestamp": now,
                        "repair_run_id": repair_run_id,
                        "shared_folder": shared,
                        "note": "该实体是已确认的无效重复 Mod，因此被删除。",
                        "backup_metadata_json": backup_meta,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            cover_fix = _rewrite_canonical_cover_if_points_at_invalid(
                conn,
                canonical_id=original_id,
                invalid_folder=inv_folder if inv_folder and inv_folder.is_dir() and not shared else None,
                canon_folder=orig_folder,
            )
            if cover_fix:
                after["cover_path_rewritten"] = cover_fix
            after["reference_action"] = _rewire_mod_id_references(
                conn, db, invalid_id, original_id
            )
            _delete_invalid_mod_row(conn, invalid_id)
            _validate_remove_invalid(
                db,
                invalid_id=invalid_id,
                original_id=original_id,
                before_original=before_original,
            )
            conn.execute("RELEASE SAVEPOINT identity_repair_remove_invalid")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT identity_repair_remove_invalid")
            conn.execute("RELEASE SAVEPOINT identity_repair_remove_invalid")
            if moved is not None:
                src, dest = moved
                if dest.exists() and not src.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dest), str(src))
            raise
    return after


def _apply_mark_conflict(db: Any, cand: RepairCandidate) -> dict[str, Any]:
    ids = [
        part.strip()
        for part in (cand.ghost_mod_id + "," + cand.candidate_mod_id).split(",")
        if part.strip().isdigit()
    ]
    note = cand.relationship + ": " + "; ".join(cand.blocking_reasons or cand.evidence)
    with db._lock:  # noqa: SLF001
        for mid in ids:
            db._conn.execute(  # noqa: SLF001
                """
                UPDATE mods SET conflict_status = ?, conflict_note = ?,
                       library_status = ?, updated_at = ?
                WHERE mod_id = ?
                """,
                ("identity_conflict", note[:500], "IDENTITY_CONFLICT", _utc_now(), int(mid)),
            )
    return {"marked": ids}


def _apply_scrub_url(db: Any, cand: RepairCandidate) -> dict[str, Any]:
    new_url = cand.candidate_source_url
    if internal_ids_embedded(new_url):
        raise RuntimeError("refusing to write source_url that still embeds an internal id")
    mid = int(cand.ghost_mod_id)
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "UPDATE mods SET source_url = ?, updated_at = ? WHERE mod_id = ?",
            (new_url, _utc_now(), mid),
        )
    return {"source_url": new_url}


def apply_identity_repair(
    db: Any,
    library_root: str | Path,
    plan: RepairPlan | None = None,
    *,
    apply: bool = False,
    quarantine_root: str | Path | None = None,
) -> RepairPlan:
    """
    Dry-run (default) or apply a repair plan.

    ``apply=False`` never mutates. ``apply=True`` never allocates identity.
    """
    root = Path(library_root)
    working = plan or plan_identity_repair(db, root)
    working.allocations = 0
    if not apply:
        working.applied = False
        working.success = True
        working.notes.append("dry_run_only")
        return working

    qroot = Path(quarantine_root) if quarantine_root else (
        data_dir() / "identity_repair_quarantine" / _utc_now().replace(":", "")
    )
    qroot.mkdir(parents=True, exist_ok=True)
    repair_run_id = qroot.name
    counts = {
        "merged": 0,
        "quarantined": 0,
        "conflict": 0,
        "skipped": 0,
        "scrubbed_url": 0,
        "removed_invalid": 0,
    }
    working.applied = True
    try:
        with repair_no_allocate_scope():
            _ensure_repair_audit_table(db)
            for cand in working.candidates:
                action = cand.proposed_action
                if action in (ACTION_NO_ACTION,):
                    counts["skipped"] += 1
                    continue
                if action in (ACTION_MERGE, ACTION_REMOVE_INVALID):
                    if not cand.ghost_mod_id.isdigit() or not cand.candidate_mod_id.isdigit():
                        counts["skipped"] += 1
                        continue
                    existing = _rows_as_dicts(
                        db,
                        "SELECT mod_id FROM mods WHERE mod_id = ?",
                        (int(cand.ghost_mod_id),),
                    )
                    if not existing:
                        counts["skipped"] += 1
                        continue
                    after = _apply_remove_invalid_duplicate(
                        db,
                        cand,
                        quarantine_root=qroot,
                        repair_run_id=repair_run_id,
                    )
                    counts["removed_invalid"] += 1
                    if after.get("quarantined"):
                        counts["quarantined"] += len(after["quarantined"])
                    _write_repair_audit(
                        db, cand, operation="remove_invalid_duplicate", after=after
                    )
                elif action == ACTION_QUARANTINE:
                    after = _apply_quarantine_only(db, cand, quarantine_root=qroot)
                    counts["quarantined"] += len(after.get("quarantined") or [])
                    _write_repair_audit(db, cand, operation="quarantine", after=after)
                elif action == ACTION_CONFLICT:
                    after = _apply_mark_conflict(db, cand)
                    counts["conflict"] += 1
                    _write_repair_audit(db, cand, operation="mark_conflict", after=after)
                elif action == ACTION_SCRUB_URL:
                    after = _apply_scrub_url(db, cand)
                    counts["scrubbed_url"] += 1
                    _write_repair_audit(db, cand, operation="scrub_url", after=after)
                elif action == ACTION_REMOVE_META:
                    if cand.ghost_mod_id.isdigit() and not cand.ghost_folder:
                        with db._lock:  # noqa: SLF001
                            db._conn.execute(  # noqa: SLF001
                                "DELETE FROM mods WHERE mod_id = ?",
                                (int(cand.ghost_mod_id),),
                            )
                        counts["merged"] += 1
                        _write_repair_audit(
                            db, cand, operation="remove_ghost_row", after={"deleted": True}
                        )
                    else:
                        counts["skipped"] += 1
                else:
                    counts["skipped"] += 1
            with db._lock:  # noqa: SLF001
                db._conn.commit()  # noqa: SLF001
        working.applied_counts = counts
        working.notes.append(
            f"Applied: merged={counts['merged']} quarantined={counts['quarantined']} "
            f"removed_invalid={counts['removed_invalid']} "
            f"conflict={counts['conflict']} skipped={counts['skipped']}"
        )
        working.notes.append(f"Identity allocations during repair: {working.allocations}")
        try:
            from services.mod_library_integrity_audit import audit_mod_library_integrity
            from services.mod_identity_repair import audit_severity_counts

            report = audit_mod_library_integrity(root, db=db)
            working.after = audit_severity_counts(report)
            working.after["CRITICAL"] = int(working.after.get("CRITICAL") or 0)
        except Exception as exc:  # noqa: BLE001
            working.after = summarize_sqlite_findings(db)
            working.notes.append(f"post_audit_fallback: {exc}")
        working.success = True
    except RepairMustNotAllocateError as exc:
        working.success = False
        working.error = str(exc)
        working.allocations = 1
        try:
            with db._lock:  # noqa: SLF001
                db._conn.rollback()  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        logger.exception("identity repair apply failed")
        working.success = False
        working.error = f"REPAIR FAILED: {exc}"
        try:
            with db._lock:  # noqa: SLF001
                db._conn.rollback()  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass
    from services.verification_result import (
        APPLY_EXECUTED,
        APPLY_FAILED,
        APPLY_UNVERIFIED,
        CODE_IMPLEMENTED,
        MUTATED,
        NOT_APPLIED,
        NOT_VERIFIED,
        PRODUCTION_VERIFIED,
        TESTED,
        UNCHANGED,
        UNTESTED,
        VerificationResult,
    )

    applied = bool(working.applied)
    critical = int((working.after or {}).get("CRITICAL") or 0)
    high = int((working.after or {}).get("HIGH") or 0)
    vr = VerificationResult(
        code_status=CODE_IMPLEMENTED,
        test_status=UNTESTED,
        plan_status="DRY_RUN_PLANNED",
        apply_status=APPLY_EXECUTED if applied and working.success else (
            APPLY_FAILED if applied else NOT_APPLIED
        ),
        production_status=MUTATED if applied else UNCHANGED,
        verification_status=NOT_VERIFIED,
        evidence={
            "applied": applied,
            "success": working.success,
            "CRITICAL": critical,
            "HIGH": high,
            "allocations": working.allocations,
        },
    )
    if applied:
        # Apply never implies PRODUCTION_VERIFIED. Residual CRITICAL/HIGH or
        # missing full verification protocol stays APPLY_UNVERIFIED.
        vr.verification_status = APPLY_UNVERIFIED
    working.verification = vr.to_dict()
    return working


def run_audit_cli(library: Path, db_path: Path, out: Path | None) -> int:
    from services.verification_result import (
        CODE_IMPLEMENTED,
        DRY_RUN_PLANNED,
        NOT_APPLIED,
        NOT_VERIFIED,
        UNCHANGED,
        VerificationResult,
    )

    facade = open_readonly_sqlite(db_path)
    try:
        plan = plan_identity_repair(facade, library)
    finally:
        facade._conn.close()
    plan.verification = VerificationResult(
        code_status=CODE_IMPLEMENTED,
        test_status="UNTESTED",
        plan_status=DRY_RUN_PLANNED,
        apply_status=NOT_APPLIED,
        production_status=UNCHANGED,
        verification_status=NOT_VERIFIED,
        evidence={
            "cli": "identity_repair --audit",
            "readonly": True,
            "db_path": str(db_path),
            "candidates": len(plan.candidates),
        },
    ).to_dict()
    text = format_repair_plan(plan)
    payload = plan.to_dict()
    if out:
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {out}")
    print(text)
    return 0


def run_apply_cli(
    library: Path,
    db_path: Path,
    *,
    yes: bool,
    out: Path | None,
) -> int:
    if not yes:
        print("Refusing apply without --yes. Printing dry-run plan only.")
        return run_audit_cli(library, db_path, out)
    from core.db_manager import DatabaseManager

    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(db_path)
    try:
        plan = plan_identity_repair(db, library)
        print(format_repair_plan(plan))
        print("Applying planned mutations...")
        result = apply_identity_repair(db, library, plan, apply=True)
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2) if out else "")
        print(
            f"Applied: merged={result.applied_counts.get('merged', 0)} "
            f"quarantined={result.applied_counts.get('quarantined', 0)} "
            f"removed_invalid={result.applied_counts.get('removed_invalid', 0)} "
            f"conflict={result.applied_counts.get('conflict', 0)} "
            f"skipped={result.applied_counts.get('skipped', 0)}"
        )
        print(f"Identity allocations during repair: {result.allocations}")
        if out:
            out.write_text(
                json.dumps(result.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if not result.success:
            return 1
        if int(result.after.get("CRITICAL") or 0) > 0:
            return 2
        return 0
    finally:
        DatabaseManager.reset_instance()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="identity-repair",
        description="Historical identity pollution planner (dry-run by default).",
    )
    parser.add_argument("--library", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--audit", action="store_true", help="Read-only plan (default)")
    mode.add_argument("--apply", action="store_true", help="Apply mutations (requires --yes)")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required confirmation for --apply",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    library = Path(args.library) if args.library else Path(default_mod_library())
    db_path = Path(args.db) if args.db else database_path()
    if args.apply:
        return run_apply_cli(library, db_path, yes=bool(args.yes), out=args.out)
    return run_audit_cli(library, db_path, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
