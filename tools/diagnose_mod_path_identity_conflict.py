#!/usr/bin/env python3
"""Read-only diagnosis for historical Mod path / identity conflicts.

Finds DB entities whose ``last_known_path`` is gone, then scans the same-game
managed library for folders whose ``.info`` proves a *different* ``internal_id``
while sharing ``workspace_id`` and a highly matching title.

This tool never mutates DB, ``.info``, backup, or lifecycle code.
It never treats ``workspace_id`` as an entity key for Reconcile/bind.

Usage::

    python tools/diagnose_mod_path_identity_conflict.py --internal-id <uuid|pk>
    python tools/diagnose_mod_path_identity_conflict.py --workspace-id 1401
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INFO_DIR = ".info"
LEGACY_INFO_DIR = "info"
METADATA = "metadata.json"
LEGACY_METADATA = "mod.json"

# Title similarity threshold for "高度匹配"
TITLE_MATCH_RATIO = 0.75

DECISION_MATCHED = "MATCHED_BY_INTERNAL_ID"
DECISION_CONFLICT = "IDENTITY_CONFLICT"
DECISION_NONE = "NO_CANDIDATE"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_title(value: Any) -> str:
    text = _text(value).casefold()
    out: list[str] = []
    prev_space = False
    for ch in text:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
            prev_space = False
        elif ch.isspace() or ch in ("·", "—", "–", ".", ",", "'", '"'):
            if not prev_space and out:
                out.append(" ")
                prev_space = True
    return "".join(out).strip()


def titles_highly_match(a: Any, b: Any, *, ratio: float = TITLE_MATCH_RATIO) -> bool:
    """True when titles are equal, nested, or SequenceMatcher ratio is high."""
    na = _normalize_title(a)
    nb = _normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= float(ratio)


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def _read_info_dict(folder: Path) -> dict[str, Any] | None:
    for info_name, meta_name in (
        (INFO_DIR, METADATA),
        (INFO_DIR, LEGACY_METADATA),
        (LEGACY_INFO_DIR, METADATA),
        (LEGACY_INFO_DIR, LEGACY_METADATA),
    ):
        meta = folder / info_name / meta_name
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"_read_error": True}
        return data if isinstance(data, dict) else {"_read_error": True}
    return None


def _info_exists(folder: Path) -> bool:
    for info_name, meta_name in (
        (INFO_DIR, METADATA),
        (INFO_DIR, LEGACY_METADATA),
        (LEGACY_INFO_DIR, METADATA),
        (LEGACY_INFO_DIR, LEGACY_METADATA),
    ):
        if (folder / info_name / meta_name).is_file():
            return True
    return False


@dataclass
class DbEntity:
    internal_id: str
    workspace_id: str
    app_id: int
    platform: str
    title: str
    last_known_path: str
    content_status: str
    mod_id: str = ""
    folder_present: int = 0
    display_name: str = ""
    identity_status: str = ""


@dataclass
class DiskCandidate:
    path: str
    info_exists: bool
    info_internal_id: str
    info_workspace_id: str
    info_title: str
    title_match: bool = False
    workspace_match: bool = False
    internal_id_match: bool = False
    info_internal_id_in_db: bool | None = None
    folder_name: str = ""


@dataclass
class DiagnoseReport:
    decision: str
    db_entity: dict[str, Any]
    disk_candidates: list[dict[str, Any]] = field(default_factory=list)
    conflict_candidates: list[dict[str, Any]] = field(default_factory=list)
    matched_candidates: list[dict[str, Any]] = field(default_factory=list)
    scan_game_folder: str = ""
    library_root: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "db_entity": self.db_entity,
            "disk_candidates": self.disk_candidates,
            "conflict_candidates": self.conflict_candidates,
            "matched_candidates": self.matched_candidates,
            "scan_game_folder": self.scan_game_folder,
            "library_root": self.library_root,
            "notes": self.notes,
        }


def load_db_entity_by_internal_id(
    con: sqlite3.Connection, internal_id: str
) -> DbEntity | None:
    """Resolve by portable ``mods.internal_id``, else numeric ``mods.mod_id`` PK."""
    key = _text(internal_id)
    if not key:
        return None
    row = con.execute(
        """
        SELECT mod_id, internal_id, workspace_id, app_id, platform, title,
               display_name, last_known_path, folder_present, content_status,
               identity_status
        FROM mods
        WHERE TRIM(COALESCE(internal_id, '')) = ?
        LIMIT 1
        """,
        (key,),
    ).fetchone()
    if row is None and key.isdigit():
        row = con.execute(
            """
            SELECT mod_id, internal_id, workspace_id, app_id, platform, title,
                   display_name, last_known_path, folder_present, content_status,
                   identity_status
            FROM mods
            WHERE mod_id = ?
            LIMIT 1
            """,
            (int(key),),
        ).fetchone()
    if row is None:
        return None
    return _row_to_entity(row)


def load_db_entity_by_workspace_id(
    con: sqlite3.Connection,
    workspace_id: str,
    *,
    app_id: int | None = None,
    platform: str | None = None,
) -> tuple[DbEntity | None, list[DbEntity]]:
    """
    Diagnostic-only workspace lookup (never used by Reconcile entity match).

    Returns ``(single_entity_or_None, all_matches)``.
    """
    wid = _text(workspace_id)
    if not wid:
        return None, []
    sql = """
        SELECT mod_id, internal_id, workspace_id, app_id, platform, title,
               display_name, last_known_path, folder_present, content_status,
               identity_status
        FROM mods
        WHERE TRIM(COALESCE(workspace_id, '')) = ?
    """
    params: list[Any] = [wid]
    if app_id is not None and int(app_id) > 0:
        sql += " AND app_id = ?"
        params.append(int(app_id))
    if platform:
        sql += " AND LOWER(TRIM(platform)) = ?"
        params.append(_text(platform).lower())
    sql += " ORDER BY mod_id"
    rows = con.execute(sql, tuple(params)).fetchall()
    entities = [_row_to_entity(r) for r in rows]
    if len(entities) == 1:
        return entities[0], entities
    return None, entities


def _row_to_entity(row: sqlite3.Row) -> DbEntity:
    iid = _text(row["internal_id"]) or _text(row["mod_id"])
    title = _text(row["title"]) or _text(row["display_name"])
    return DbEntity(
        internal_id=iid,
        workspace_id=_text(row["workspace_id"]),
        app_id=int(row["app_id"] or 0),
        platform=_text(row["platform"]),
        title=title,
        last_known_path=_text(row["last_known_path"]),
        content_status=_text(row["content_status"]),
        mod_id=_text(row["mod_id"]),
        folder_present=int(row["folder_present"] or 0),
        display_name=_text(row["display_name"]),
        identity_status=_text(row["identity_status"]),
    )


def resolve_game_folder(
    con: sqlite3.Connection,
    entity: DbEntity,
    *,
    library_root: Path,
) -> str:
    """Prefer last_known_path parent; else games.name sanitized via app_id."""
    lkp = _text(entity.last_known_path)
    if lkp:
        try:
            path = Path(lkp)
            parent = path.parent
            if parent.name and parent != library_root:
                return parent.name
        except OSError:
            pass

    aid = int(entity.app_id or 0)
    if aid > 0:
        row = con.execute(
            "SELECT name FROM games WHERE app_id = ? LIMIT 1",
            (aid,),
        ).fetchone()
        if row is not None and _text(row["name"]):
            from core.sanitize import sanitize_folder_name

            return sanitize_folder_name(
                _text(row["name"]), fallback=f"App_{aid}"
            )
    return ""


def list_mod_dirs(game_dir: Path) -> list[Path]:
    if not game_dir.is_dir():
        return []
    try:
        return sorted(
            (p for p in game_dir.iterdir() if p.is_dir()),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return []


def internal_id_exists_in_db(con: sqlite3.Connection, internal_id: str) -> bool:
    key = _text(internal_id)
    if not key:
        return False
    row = con.execute(
        """
        SELECT 1 FROM mods
        WHERE TRIM(COALESCE(internal_id, '')) = ?
        LIMIT 1
        """,
        (key,),
    ).fetchone()
    if row is not None:
        return True
    if key.isdigit():
        row = con.execute(
            "SELECT 1 FROM mods WHERE mod_id = ? LIMIT 1",
            (int(key),),
        ).fetchone()
        return row is not None
    return False


def build_disk_candidates(
    con: sqlite3.Connection,
    entity: DbEntity,
    *,
    game_dir: Path,
) -> list[DiskCandidate]:
    db_titles = [
        t
        for t in (entity.title, entity.display_name, Path(entity.last_known_path).name)
        if _text(t)
    ]
    out: list[DiskCandidate] = []
    for folder in list_mod_dirs(game_dir):
        exists = _info_exists(folder)
        data = _read_info_dict(folder) if exists else None
        info_iid = _text((data or {}).get("internal_id")) if data else ""
        info_wid = _text((data or {}).get("workspace_id")) if data else ""
        info_title = ""
        if data:
            info_title = (
                _text(data.get("title"))
                or _text(data.get("display_name"))
                or folder.name
            )
        title_match = False
        for db_t in db_titles:
            if titles_highly_match(db_t, info_title) or titles_highly_match(
                db_t, folder.name
            ):
                title_match = True
                break
        iid_match = bool(
            info_iid
            and (
                info_iid == entity.internal_id
                or (entity.mod_id and info_iid == entity.mod_id)
            )
        )
        in_db: bool | None = None
        if info_iid:
            in_db = internal_id_exists_in_db(con, info_iid)
        out.append(
            DiskCandidate(
                path=str(folder.resolve()),
                info_exists=exists,
                info_internal_id=info_iid,
                info_workspace_id=info_wid,
                info_title=info_title or folder.name,
                title_match=title_match,
                workspace_match=bool(
                    entity.workspace_id
                    and info_wid
                    and info_wid == entity.workspace_id
                ),
                internal_id_match=iid_match,
                info_internal_id_in_db=in_db,
                folder_name=folder.name,
            )
        )
    return out


def diagnose_entity(
    con: sqlite3.Connection,
    entity: DbEntity,
    *,
    library_root: Path,
) -> DiagnoseReport:
    notes: list[str] = []
    game_folder = resolve_game_folder(con, entity, library_root=library_root)
    if not game_folder:
        notes.append("could not resolve game folder from last_known_path or app_id")
        return DiagnoseReport(
            decision=DECISION_NONE,
            db_entity=_entity_public(entity),
            scan_game_folder="",
            library_root=str(library_root),
            notes=notes,
        )

    game_dir = library_root / game_folder
    if not game_dir.is_dir():
        notes.append(f"game folder missing on disk: {game_dir}")
        return DiagnoseReport(
            decision=DECISION_NONE,
            db_entity=_entity_public(entity),
            scan_game_folder=game_folder,
            library_root=str(library_root),
            notes=notes,
        )

    candidates = build_disk_candidates(con, entity, game_dir=game_dir)
    matched = [c for c in candidates if c.internal_id_match]
    conflicts = [
        c
        for c in candidates
        if c.info_exists
        and c.workspace_match
        and c.title_match
        and not c.internal_id_match
        and _text(c.info_internal_id)
        and _text(c.info_internal_id) != entity.internal_id
    ]

    if matched:
        decision = DECISION_MATCHED
        notes.append(
            "folder already proves DB internal_id — no identity rewrite needed"
        )
    elif conflicts:
        decision = DECISION_CONFLICT
        notes.append(
            "historical conflict: same workspace_id + high title match, "
            "but .info.internal_id differs from DB (and is not used for auto-bind)"
        )
        for c in conflicts:
            if c.info_internal_id_in_db is False:
                notes.append(
                    f"disk .info.internal_id {c.info_internal_id!r} is absent from DB"
                )
    else:
        decision = DECISION_NONE
        notes.append("no matching or conflicting candidates under game folder")

    return DiagnoseReport(
        decision=decision,
        db_entity=_entity_public(entity),
        disk_candidates=[asdict(c) for c in candidates],
        conflict_candidates=[asdict(c) for c in conflicts],
        matched_candidates=[asdict(c) for c in matched],
        scan_game_folder=game_folder,
        library_root=str(library_root.resolve()),
        notes=notes,
    )


def _entity_public(entity: DbEntity) -> dict[str, Any]:
    """Public DB entity block required by the tool contract."""
    return {
        "internal_id": entity.internal_id,
        "workspace_id": entity.workspace_id,
        "app_id": entity.app_id,
        "platform": entity.platform,
        "title": entity.title,
        "last_known_path": entity.last_known_path,
        "content_status": entity.content_status,
        # helpful extras (do not replace contract fields)
        "mod_id": entity.mod_id,
        "folder_present": entity.folder_present,
        "display_name": entity.display_name,
        "identity_status": entity.identity_status,
    }


def run_diagnose(
    *,
    internal_id: str | None = None,
    workspace_id: str | None = None,
    app_id: int | None = None,
    platform: str | None = None,
    db_path: Path | None = None,
    library_root: Path | None = None,
) -> DiagnoseReport:
    from core.paths import database_path, default_mod_library

    db_file = Path(db_path) if db_path is not None else database_path()
    lib = Path(library_root) if library_root is not None else default_mod_library()
    con = _connect(db_file)
    try:
        entity: DbEntity | None = None
        if _text(internal_id):
            entity = load_db_entity_by_internal_id(con, _text(internal_id))
            if entity is None:
                return DiagnoseReport(
                    decision=DECISION_NONE,
                    db_entity={},
                    library_root=str(lib),
                    notes=[f"no DB entity for --internal-id {_text(internal_id)!r}"],
                )
        elif _text(workspace_id):
            entity, matches = load_db_entity_by_workspace_id(
                con,
                _text(workspace_id),
                app_id=app_id,
                platform=platform,
            )
            if entity is None:
                if not matches:
                    return DiagnoseReport(
                        decision=DECISION_NONE,
                        db_entity={},
                        library_root=str(lib),
                        notes=[
                            f"no DB entity for --workspace-id {_text(workspace_id)!r}"
                        ],
                    )
                return DiagnoseReport(
                    decision=DECISION_NONE,
                    db_entity={},
                    library_root=str(lib),
                    notes=[
                        "ambiguous workspace_id matches "
                        f"({len(matches)}); pass --app-id / --platform / "
                        "--internal-id to disambiguate",
                        *[
                            f"candidate mod_id={m.mod_id} app_id={m.app_id} "
                            f"title={m.title!r}"
                            for m in matches
                        ],
                    ],
                )
        else:
            raise ValueError("either internal_id or workspace_id is required")

        assert entity is not None
        return diagnose_entity(con, entity, library_root=lib)
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose Mod path / identity conflicts (read-only)."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--internal-id",
        help="DB portable internal_id UUID, or numeric mods.mod_id PK",
    )
    group.add_argument(
        "--workspace-id",
        help="Diagnostic-only workspace lookup (not an entity bind key)",
    )
    parser.add_argument(
        "--app-id",
        type=int,
        default=None,
        help="Optional disambiguator with --workspace-id",
    )
    parser.add_argument(
        "--platform",
        default=None,
        help="Optional disambiguator with --workspace-id",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite path (default: production database_path())",
    )
    parser.add_argument(
        "--library",
        type=Path,
        default=None,
        help="Managed mod library root (default: default_mod_library())",
    )
    args = parser.parse_args(argv)

    report = run_diagnose(
        internal_id=args.internal_id,
        workspace_id=args.workspace_id,
        app_id=args.app_id,
        platform=args.platform,
        db_path=args.db,
        library_root=args.library,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
