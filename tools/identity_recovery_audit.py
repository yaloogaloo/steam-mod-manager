"""Identity Recovery — Phase 1 read-only pollution audit.

ARCHITECTURE RULES (must not violate)
-------------------------------------
- DB ``internal_id`` / ``mod_id`` is the only entity authority.
- ``.info`` is entity proof; directory name is never identity.
- ``workspace_id`` is display-only — this tool never reverse-looks it up.
- Never create / merge / delete / migrate Mod entities.
- Never call ``create_mod_identity``, Sync, Import, Deploy, or Status writers.
- Phase 1: report only. Phase 2 repair waits for human confirmation.

Usage::

    python tools/identity_recovery_audit.py
    python tools/identity_recovery_audit.py --db path/to.db --library path/to/mod --out report.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

INFO_DIR_NAMES = (".info", "info")
METADATA_NAMES = ("metadata.json", "mod.json")
BACKUP_DIR_NAME = "mod_backup"
BACKUP_METADATA_NAME = "metadata.json"

# Finding codes (stable for report consumers)
DUP_INTERNAL_ID = "duplicate_internal_id"
CROSS_GAME_EXTERNAL = "cross_game_same_external_id"
CROSS_GAME_WORKSPACE = "cross_game_same_workspace_id"
INFO_NOT_IN_DB = "info_internal_id_not_in_db"
DB_WITHOUT_VALID_INFO = "db_entity_without_valid_info"
DB_INFO_MISMATCH = "db_info_identity_mismatch"
BACKUP_ANOMALY = "backup_identity_source_anomaly"

# Recommended actions — suggestions only; never auto-executed in phase 1
ACTION_REVIEW = "REVIEW_MANUAL"
ACTION_IGNORE_ORPHAN_INFO = "IGNORE_ORPHAN_INFO"
ACTION_RESTORE_INFO_FROM_BACKUP = "RESTORE_INFO_FROM_BACKUP_AFTER_CONFIRM"
ACTION_REBIND_PATH = "REBIND_STORAGE_PATH_AFTER_CONFIRM"
ACTION_SPLIT_CROSS_GAME = "SPLIT_CROSS_GAME_ENTITIES_AFTER_CONFIRM"
ACTION_DEDUP_INTERNAL = "DEDUP_INTERNAL_ID_AFTER_CONFIRM"
ACTION_QUARANTINE_BACKUP = "QUARANTINE_BACKUP_AFTER_CONFIRM"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_info(folder: Path) -> tuple[dict[str, Any] | None, str]:
    """Return (payload, info_path). Empty payload file → ({}, path). Missing → (None, "")."""
    for info_name in INFO_DIR_NAMES:
        for meta_name in METADATA_NAMES:
            meta = folder / info_name / meta_name
            if not meta.is_file():
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except Exception:
                return {"_read_error": True}, str(meta)
            if isinstance(data, dict):
                return data, str(meta)
            return {"_read_error": True}, str(meta)
    return None, ""


def _db_row_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "mod_id": _text(row.get("mod_id")),
        "internal_id": _text(row.get("internal_id")),
        "app_id": int(row.get("app_id") or 0),
        "platform": _text(row.get("platform")),
        "external_id": _text(row.get("external_id")),
        "workspace_id": _text(row.get("workspace_id")),
        "title": _text(row.get("title") or row.get("display_name")),
        "source_url": _text(row.get("source_url")),
        "last_known_path": _text(row.get("last_known_path")),
        "folder_present": int(row.get("folder_present") or 0),
        "identity_status": _text(row.get("identity_status")),
    }


def _finding(
    *,
    code: str,
    reason: str,
    recommended_action: str,
    internal_id: str = "",
    app_id: int = 0,
    platform: str = "",
    external_id: str = "",
    info_path: str = "",
    db_record: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "code": code,
        "internal_id": internal_id,
        "app_id": int(app_id or 0),
        "platform": platform,
        "external_id": external_id,
        "info_path": info_path,
        "db_record": db_record or {},
        "conflict_reason": reason,
        "recommended_action": recommended_action,
    }
    if extra:
        out["extra"] = extra
    return out


def load_db_rows(db_path: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(mods)")}
        wanted = [
            "mod_id",
            "app_id",
            "platform",
            "external_id",
            "workspace_id",
            "internal_id",
            "title",
            "display_name",
            "source_url",
            "last_known_path",
            "folder_present",
            "identity_status",
            "source_type",
        ]
        select = ", ".join(c for c in wanted if c in cols)
        rows = [dict(r) for r in con.execute(f"SELECT {select} FROM mods")]
    finally:
        con.close()
    return rows


def iter_managed_folders(library: Path) -> list[Path]:
    if not library.is_dir():
        return []
    out: list[Path] = []
    for game in sorted(p for p in library.iterdir() if p.is_dir()):
        if game.name.startswith("."):
            continue
        for folder in sorted(p for p in game.iterdir() if p.is_dir()):
            if folder.name.startswith("."):
                continue
            out.append(folder)
    return out


def scan_identity_recovery(
    *,
    db_path: Path,
    library: Path,
    backup_root: Path,
) -> dict[str, Any]:
    """Read-only scan. Never mutates DB, filesystem, or identity."""
    rows = load_db_rows(db_path)
    findings: list[dict[str, Any]] = []

    by_mod_id: dict[str, dict[str, Any]] = {
        _text(r.get("mod_id")): r for r in rows if _text(r.get("mod_id"))
    }
    by_uuid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        uid = _text(r.get("internal_id"))
        if uid:
            by_uuid[uid].append(r)

    # --- 1. duplicate internal_id (UUID column) ---
    for uid, group in by_uuid.items():
        if len(group) < 2:
            continue
        findings.append(
            _finding(
                code=DUP_INTERNAL_ID,
                reason=f"mods.internal_id={uid!r} shared by {len(group)} rows",
                recommended_action=ACTION_DEDUP_INTERNAL,
                internal_id=uid,
                app_id=int(group[0].get("app_id") or 0),
                platform=_text(group[0].get("platform")),
                external_id=_text(group[0].get("external_id")),
                db_record=_db_row_summary(group[0]),
                extra={"rows": [_db_row_summary(g) for g in group]},
            )
        )

    # --- 2. cross-game same external_id ---
    ext_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        plat = _text(r.get("platform")).lower()
        ext = _text(r.get("external_id"))
        if not plat or not ext or ext.startswith("local/"):
            continue
        ext_groups[(plat, ext)].append(r)
    for (plat, ext), group in ext_groups.items():
        apps = {int(g.get("app_id") or 0) for g in group}
        if len(group) > 1 and len(apps) > 1:
            findings.append(
                _finding(
                    code=CROSS_GAME_EXTERNAL,
                    reason=(
                        f"(platform={plat}, external_id={ext}) spans app_ids={sorted(apps)}"
                    ),
                    recommended_action=ACTION_SPLIT_CROSS_GAME,
                    internal_id=_text(group[0].get("internal_id"))
                    or _text(group[0].get("mod_id")),
                    app_id=int(group[0].get("app_id") or 0),
                    platform=plat,
                    external_id=ext,
                    db_record=_db_row_summary(group[0]),
                    extra={"rows": [_db_row_summary(g) for g in group]},
                )
            )

    # --- 3. cross-game same workspace_id (report only; never used as lookup) ---
    ws_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        ws = _text(r.get("workspace_id"))
        if ws:
            ws_groups[ws].append(r)
    for ws, group in ws_groups.items():
        apps = {int(g.get("app_id") or 0) for g in group}
        if len(group) > 1 and len(apps) > 1:
            findings.append(
                _finding(
                    code=CROSS_GAME_WORKSPACE,
                    reason=(
                        f"workspace_id={ws!r} spans app_ids={sorted(apps)} "
                        "(display collision; not an entity key)"
                    ),
                    recommended_action=ACTION_REVIEW,
                    internal_id=_text(group[0].get("internal_id"))
                    or _text(group[0].get("mod_id")),
                    app_id=int(group[0].get("app_id") or 0),
                    platform=_text(group[0].get("platform")),
                    external_id=_text(group[0].get("external_id")),
                    db_record=_db_row_summary(group[0]),
                    extra={
                        "workspace_id": ws,
                        "rows": [_db_row_summary(g) for g in group],
                    },
                )
            )

    # Index DB by path for mismatch / missing-info checks
    by_path: dict[str, dict[str, Any]] = {}
    for r in rows:
        path = _text(r.get("last_known_path"))
        if not path:
            continue
        try:
            key = str(Path(path).resolve()).lower().replace("/", "\\")
        except OSError:
            key = path.lower().replace("/", "\\")
        by_path[key] = r

    # --- scan library folders ---
    folders = iter_managed_folders(library)
    info_seen_mod_ids: set[str] = set()

    for folder in folders:
        payload, info_path = _read_info(folder)
        try:
            folder_key = str(folder.resolve()).lower().replace("/", "\\")
        except OSError:
            folder_key = str(folder).lower().replace("/", "\\")

        path_row = by_path.get(folder_key)

        if payload is None:
            # No .info — if DB owns this path, report missing proof
            if path_row is not None:
                mid = _text(path_row.get("mod_id"))
                findings.append(
                    _finding(
                        code=DB_WITHOUT_VALID_INFO,
                        reason="DB last_known_path points at folder with no .info",
                        recommended_action=ACTION_RESTORE_INFO_FROM_BACKUP,
                        internal_id=_text(path_row.get("internal_id")) or mid,
                        app_id=int(path_row.get("app_id") or 0),
                        platform=_text(path_row.get("platform")),
                        external_id=_text(path_row.get("external_id")),
                        info_path="",
                        db_record=_db_row_summary(path_row),
                        extra={"folder": str(folder)},
                    )
                )
            continue

        if payload.get("_read_error"):
            findings.append(
                _finding(
                    code=DB_INFO_MISMATCH,
                    reason="unreadable .info/metadata.json",
                    recommended_action=ACTION_REVIEW,
                    info_path=info_path,
                    db_record=_db_row_summary(path_row) if path_row else {},
                    extra={"folder": str(folder)},
                )
            )
            continue

        from services.mod_identity import read_entity_key

        info_uuid = read_entity_key(payload)
        info_ext = _text(payload.get("external_id") or payload.get("published_file_id"))
        info_plat = _text(
            payload.get("platform") or payload.get("source_type")
        ).lower()
        try:
            info_app = int(payload.get("app_id") or 0)
        except (TypeError, ValueError):
            info_app = 0

        # Resolve DB entity by entity_key / legacy sidecar key → mods.internal_id
        matched: dict[str, Any] | None = None
        if info_uuid:
            uuid_hits = by_uuid.get(info_uuid) or []
            if len(uuid_hits) == 1:
                matched = uuid_hits[0]
            elif info_uuid in by_mod_id:
                matched = by_mod_id[info_uuid]
            elif not uuid_hits and info_uuid not in by_mod_id:
                findings.append(
                    _finding(
                        code=INFO_NOT_IN_DB,
                        reason=(
                            f".info internal_id={info_uuid!r} does not exist in DB"
                        ),
                        recommended_action=ACTION_IGNORE_ORPHAN_INFO,
                        internal_id=info_uuid,
                        app_id=info_app,
                        platform=info_plat,
                        external_id=info_ext,
                        info_path=info_path,
                        db_record={},
                        extra={
                            "folder": str(folder),
                            "info_workspace_id": _text(payload.get("workspace_id")),
                            "info_title": _text(
                                payload.get("title") or payload.get("display_name")
                            ),
                        },
                    )
                )
                continue

        if matched is None and path_row is not None:
            # Path owned by DB but .info lacks resolvable internal_id
            if not info_uuid:
                findings.append(
                    _finding(
                        code=DB_WITHOUT_VALID_INFO,
                        reason="DB owns path but .info has no internal_id",
                        recommended_action=ACTION_RESTORE_INFO_FROM_BACKUP,
                        internal_id=_text(path_row.get("internal_id"))
                        or _text(path_row.get("mod_id")),
                        app_id=int(path_row.get("app_id") or 0),
                        platform=_text(path_row.get("platform")),
                        external_id=_text(path_row.get("external_id")),
                        info_path=info_path,
                        db_record=_db_row_summary(path_row),
                        extra={"folder": str(folder)},
                    )
                )
            continue

        if matched is None:
            # .info without internal_id and no path ownership — not a registered Mod
            continue

        mid = _text(matched.get("mod_id"))
        info_seen_mod_ids.add(mid)

        # --- 6. DB vs .info identity mismatch ---
        mismatches: list[str] = []
        db_uuid = _text(matched.get("internal_id"))
        if info_uuid and db_uuid and info_uuid != db_uuid and info_uuid != mid:
            mismatches.append(
                f"internal_id info={info_uuid!r} db={db_uuid!r}"
            )
        db_ext = _text(matched.get("external_id"))
        if info_ext and db_ext and info_ext != db_ext:
            mismatches.append(f"external_id info={info_ext!r} db={db_ext!r}")
        db_plat = _text(matched.get("platform")).lower()
        if info_plat and db_plat and info_plat != db_plat:
            mismatches.append(f"platform info={info_plat!r} db={db_plat!r}")
        db_app = int(matched.get("app_id") or 0)
        if info_app > 0 and db_app > 0 and info_app != db_app:
            mismatches.append(f"app_id info={info_app} db={db_app}")
        if path_row is not None and _text(path_row.get("mod_id")) != mid:
            mismatches.append(
                "path owned by different mod_id="
                f"{_text(path_row.get('mod_id'))!r} vs info→{mid!r}"
            )

        if mismatches:
            findings.append(
                _finding(
                    code=DB_INFO_MISMATCH,
                    reason="; ".join(mismatches),
                    recommended_action=ACTION_REBIND_PATH,
                    internal_id=db_uuid or mid,
                    app_id=db_app,
                    platform=db_plat,
                    external_id=db_ext,
                    info_path=info_path,
                    db_record=_db_row_summary(matched),
                    extra={
                        "folder": str(folder),
                        "info_internal_id": info_uuid,
                        "info_external_id": info_ext,
                        "info_platform": info_plat,
                        "info_app_id": info_app,
                    },
                )
            )

    # DB rows whose path is missing / has no matching .info bind
    for r in rows:
        mid = _text(r.get("mod_id"))
        if mid in info_seen_mod_ids:
            continue
        path = _text(r.get("last_known_path"))
        if not path:
            findings.append(
                _finding(
                    code=DB_WITHOUT_VALID_INFO,
                    reason="DB entity has empty last_known_path (no storage bind)",
                    recommended_action=ACTION_REVIEW,
                    internal_id=_text(r.get("internal_id")) or mid,
                    app_id=int(r.get("app_id") or 0),
                    platform=_text(r.get("platform")),
                    external_id=_text(r.get("external_id")),
                    info_path="",
                    db_record=_db_row_summary(r),
                )
            )
            continue
        p = Path(path)
        if not p.is_dir():
            findings.append(
                _finding(
                    code=DB_WITHOUT_VALID_INFO,
                    reason="DB last_known_path directory missing on disk",
                    recommended_action=ACTION_REVIEW,
                    internal_id=_text(r.get("internal_id")) or mid,
                    app_id=int(r.get("app_id") or 0),
                    platform=_text(r.get("platform")),
                    external_id=_text(r.get("external_id")),
                    info_path="",
                    db_record=_db_row_summary(r),
                    extra={"missing_path": path},
                )
            )
            continue
        payload, info_path = _read_info(p)
        from services.mod_identity import read_entity_key

        if payload is None or not read_entity_key(payload):
            # Already reported when scanning folders if path under library;
            # still report for paths outside scanned tree.
            if mid not in info_seen_mod_ids:
                findings.append(
                    _finding(
                        code=DB_WITHOUT_VALID_INFO,
                        reason="DB path exists but .info missing or lacks entity_key",
                        recommended_action=ACTION_RESTORE_INFO_FROM_BACKUP,
                        internal_id=_text(r.get("internal_id")) or mid,
                        app_id=int(r.get("app_id") or 0),
                        platform=_text(r.get("platform")),
                        external_id=_text(r.get("external_id")),
                        info_path=info_path,
                        db_record=_db_row_summary(r),
                    )
                )

    # --- 7. backup identity anomalies ---
    if backup_root.is_dir():
        for child in sorted(p for p in backup_root.iterdir() if p.is_dir()):
            folder_name = child.name
            bak_meta = child / BACKUP_METADATA_NAME
            payload: dict[str, Any] = {}
            if bak_meta.is_file():
                try:
                    raw = json.loads(bak_meta.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        payload = raw
                except Exception:
                    payload = {"_read_error": True}

            db_row = by_mod_id.get(folder_name)
            from services.mod_identity import read_entity_key

            bak_uuid = read_entity_key(payload)
            bak_ws = _text(payload.get("workspace_id"))
            bak_plat = _text(
                payload.get("platform") or payload.get("source_type")
            ).lower()
            try:
                bak_app = int(payload.get("app_id") or 0)
            except (TypeError, ValueError):
                bak_app = 0

            if db_row is None:
                findings.append(
                    _finding(
                        code=BACKUP_ANOMALY,
                        reason=(
                            f"backup dir {folder_name!r} has no DB entity "
                            "(must not create Mod from backup)"
                        ),
                        recommended_action=ACTION_QUARANTINE_BACKUP,
                        internal_id=bak_uuid or folder_name,
                        app_id=bak_app,
                        platform=bak_plat,
                        external_id=_text(payload.get("external_id")),
                        info_path=str(bak_meta) if bak_meta.is_file() else "",
                        db_record={},
                        extra={
                            "backup_dir": str(child),
                            "backup_workspace_id": bak_ws,
                            "backup_title": _text(
                                payload.get("title") or payload.get("display_name")
                            ),
                        },
                    )
                )
                continue

            anomalies: list[str] = []
            db_uuid = _text(db_row.get("internal_id"))
            if bak_uuid and db_uuid and bak_uuid != db_uuid and bak_uuid != folder_name:
                anomalies.append(
                    f"backup internal_id={bak_uuid!r} != db={db_uuid!r}"
                )
            db_ws = _text(db_row.get("workspace_id"))
            if bak_ws and db_ws and bak_ws != db_ws:
                anomalies.append(
                    f"backup workspace_id={bak_ws!r} != db={db_ws!r}"
                )
            db_plat = _text(db_row.get("platform")).lower()
            if bak_plat and db_plat and bak_plat != db_plat:
                anomalies.append(
                    f"backup platform={bak_plat!r} != db={db_plat!r}"
                )
            db_app = int(db_row.get("app_id") or 0)
            if bak_app > 0 and db_app > 0 and bak_app != db_app:
                anomalies.append(f"backup app_id={bak_app} != db={db_app}")
            # Digit-named backup that does not match entity PK semantics noise
            if folder_name.isdigit() and folder_name != _text(db_row.get("mod_id")):
                anomalies.append(
                    f"backup folder name {folder_name!r} != mod_id "
                    f"{_text(db_row.get('mod_id'))!r}"
                )

            if anomalies:
                findings.append(
                    _finding(
                        code=BACKUP_ANOMALY,
                        reason="; ".join(anomalies),
                        recommended_action=ACTION_QUARANTINE_BACKUP,
                        internal_id=db_uuid or folder_name,
                        app_id=db_app,
                        platform=db_plat,
                        external_id=_text(db_row.get("external_id")),
                        info_path=str(bak_meta) if bak_meta.is_file() else "",
                        db_record=_db_row_summary(db_row),
                        extra={"backup_dir": str(child)},
                    )
                )

    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[str(f["code"])] += 1

    return {
        "generated_at": _now(),
        "phase": 1,
        "mode": "read_only",
        "production_mutation": "NONE",
        "db_path": str(db_path),
        "library_path": str(library),
        "backup_path": str(backup_root),
        "mods_count": len(rows),
        "managed_folders_scanned": len(folders),
        "findings_count": len(findings),
        "counts": dict(sorted(counts.items())),
        "recommended_actions_legend": {
            ACTION_REVIEW: "Human review only; no automatic change",
            ACTION_IGNORE_ORPHAN_INFO: "Treat .info as non-Mod; do not create DB entity",
            ACTION_RESTORE_INFO_FROM_BACKUP: "After confirm: restore .info for existing DB entity",
            ACTION_REBIND_PATH: "After confirm: rebind storage path for existing internal_id",
            ACTION_SPLIT_CROSS_GAME: "After confirm: split polluted cross-game bindings",
            ACTION_DEDUP_INTERNAL: "After confirm: resolve duplicate internal_id rows",
            ACTION_QUARANTINE_BACKUP: "After confirm: quarantine orphan/mismatched backup",
        },
        "findings": findings,
        "guards": {
            "creates_mods": False,
            "merges_entities": False,
            "modifies_deploy": False,
            "modifies_status": False,
            "calls_create_mod_identity": False,
            "calls_workspace_reverse_lookup": False,
            "auto_repair": False,
        },
    }


def assert_tool_is_read_only(source: str) -> None:
    """Static self-check used by tests (ignores comments / docstrings)."""
    import re

    stripped = re.sub(r'"""[\s\S]*?"""', "", source)
    stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
    stripped = re.sub(r"#.*?$", "", stripped, flags=re.M)
    # Drop this guard function body so its own string tokens are not false positives.
    stripped = re.sub(
        r"def assert_tool_is_read_only\([\s\S]*?(?=\ndef |\Z)",
        "",
        stripped,
    )
    forbidden = (
        "create_mod_" + "identity(",
        "find_mod_by_workspace_" + "id(",
        "find_mod_id_by_workspace_" + "id(",
        "allocate_internal_" + "id(",
        "allocate_mod_" + "id(",
        "INSERT INTO " + "mods",
        "import_orphan_" + "candidates(",
    )
    for token in forbidden:
        if token in stripped:
            raise AssertionError(f"identity recovery tool must not contain {token!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=str, default="", help="SQLite path")
    parser.add_argument("--library", type=str, default="", help="Mod library root")
    parser.add_argument("--backup", type=str, default="", help="mod_backup root")
    parser.add_argument(
        "--out",
        type=str,
        default="",
        help="Output JSON (default: tools/_audit_out/identity_recovery_report.json)",
    )
    args = parser.parse_args(argv)

    # Local imports kept minimal — avoid identity create / deploy / status writers.
    from core.paths import database_path, default_mod_library, data_dir

    db_path = Path(args.db) if args.db else database_path()
    library = Path(args.library) if args.library else default_mod_library()
    backup = (
        Path(args.backup) if args.backup else (data_dir() / BACKUP_DIR_NAME)
    )
    out = (
        Path(args.out)
        if args.out
        else (ROOT / "tools" / "_audit_out" / "identity_recovery_report.json")
    )

    report = scan_identity_recovery(
        db_path=db_path, library=library, backup_root=backup
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # Also write a dated copy next to latest.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dated = out.with_name(f"identity_recovery_report_{stamp}.json")
    dated.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"report={out}")
    print(f"dated={dated}")
    print(f"mods_count={report['mods_count']}")
    print(f"findings_count={report['findings_count']}")
    print(f"counts={json.dumps(report['counts'], ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
