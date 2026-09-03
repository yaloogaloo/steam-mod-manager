"""P0-1B gated production ID semantic repair.

Read-only unless invoked with --apply. Never uses DatabaseManager (avoids
workspace backfill on connect). Never deletes mods/folders. Never touches
data/identity_repair_production_backup/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.mod_platform import (  # noqa: E402
    PLATFORM_GITHUB,
    PLATFORM_MODIO,
    PLATFORM_NEXUS,
    PLATFORM_OTHER,
    PLATFORM_STEAM,
    _nexus_numeric_id_from_url,
    generate_unique_workspace_id,
    is_internal_mod_id,
    normalize_platform,
    steam_workshop_url,
)
from core.paths import data_dir, database_path  # noqa: E402
from services.identity_invariants import (  # noqa: E402
    audit_production_id_semantics,
    classify_production_id_row,
    scan_id_architecture_source,
)
from services.identity_repair import open_readonly_sqlite  # noqa: E402

LEGAL_NEXUS_INTERNAL = frozenset({"9000000000003451", "9000000000003452"})
POLLUTED_URL_IDS = (
    "9000000000000354",
    "9000000000000360",
    "9000000000000361",
    "9000000000000362",
    "9000000000003031",
    "9000000000003054",
    "9000000000003225",
)
HISTORICAL_GHOSTS = tuple(
    str(i) for i in range(9_000_000_000_003_438, 9_000_000_000_003_451)
)
CAT_A_PLATFORMS = frozenset({PLATFORM_GITHUB, PLATFORM_MODIO, PLATFORM_OTHER})
SOURCE = "p0_1b_id_semantic_repair"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _load_mod_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT mod_id, platform, external_id, workspace_id, source_url,
               title, display_name, last_known_path, app_id
        FROM mods
        """
    ).fetchall()
    return [_row_dict(r) for r in rows]


def _url_embeds_internal(url: str, internal_id: str) -> bool:
    if not url or not internal_id or not is_internal_mod_id(internal_id):
        return False
    compact = url.replace(" ", "")
    return f"id={internal_id}" in compact


def _is_fake_steam_url(url: str, internal_id: str) -> bool:
    if not _url_embeds_internal(url, internal_id):
        return False
    low = url.lower()
    return "steamcommunity.com" in low and "filedetails" in low


def _proposed_source_url(row: dict[str, Any]) -> str:
    url = _text(row.get("source_url"))
    mid = _text(row.get("mod_id"))
    plat = normalize_platform(_text(row.get("platform")))
    ext = _text(row.get("external_id"))
    if not _url_embeds_internal(url, mid):
        return url
    if plat == PLATFORM_STEAM and ext.isdigit() and not is_internal_mod_id(ext) and ext != mid:
        return steam_workshop_url(ext)
    if _is_fake_steam_url(url, mid):
        return ""
    return url


def _workspace_from_internal(internal_id: str, workspace_id: str) -> bool:
    ws = _text(workspace_id)
    mid = _text(internal_id)
    return bool(ws) and ws == mid


def build_plan(conn: sqlite3.Connection) -> dict[str, Any]:
    all_rows = _load_mod_rows(conn)
    facade = _Readonly(conn)
    audit = audit_production_id_semantics(facade)
    taken_ws = {
        _text(r.get("workspace_id"))
        for r in all_rows
        if _text(r.get("workspace_id"))
    }
    existing_nexus_ext: dict[tuple[str, str], list[str]] = defaultdict(list)
    existing_ws_owners: dict[str, list[str]] = defaultdict(list)
    by_id = {_text(r.get("mod_id")): r for r in all_rows}
    for row in all_rows:
        mid = _text(row.get("mod_id"))
        plat = normalize_platform(_text(row.get("platform")))
        ext = _text(row.get("external_id"))
        ws = _text(row.get("workspace_id"))
        app = _text(row.get("app_id"))
        if ws:
            existing_ws_owners[ws].append(mid)
        if plat == PLATFORM_NEXUS and ext:
            existing_nexus_ext[(app, ext)].append(mid)

    needs = [
        r
        for r in all_rows
        if classify_production_id_row(r) == "NEEDS_REPAIR"
    ]
    extra_url = [
        by_id[i] for i in POLLUTED_URL_IDS if i in by_id and by_id[i] not in needs
    ]
    candidates = needs + extra_url

    items: list[dict[str, Any]] = []
    for row in candidates:
        items.append(
            _plan_one(
                row,
                taken_ws=taken_ws,
                existing_nexus_ext=existing_nexus_ext,
                existing_ws_owners=existing_ws_owners,
                by_id=by_id,
            )
        )

    counts = audit.get("counts") or {}
    by_cat: dict[str, int] = defaultdict(int)
    by_pre: dict[str, int] = defaultdict(int)
    for item in items:
        by_cat[item["repair_category"]] += 1
        by_pre[item["preflight_status"]] += 1
    empty_ws = {
        plat: sum(
            1
            for r in all_rows
            if normalize_platform(_text(r.get("platform"))) == plat
            and not _text(r.get("workspace_id"))
        )
        for plat in ("github", "modio", "other")
    }
    proof = all(
        not _workspace_from_internal(i["internal_id"], i["proposed_workspace_id"])
        for i in items
        if i["preflight_status"] == "APPROVED"
    )
    proof = proof and all(
        i["proposed_workspace_id"] != i["internal_id"]
        for i in items
        if i["preflight_status"] == "APPROVED" and i["platform"] != PLATFORM_STEAM
    )
    return {
        "created_at": _utc_now(),
        "db": str(database_path()),
        "forensic_before": {
            "scanned": audit.get("scanned"),
            "counts": counts,
            "needs_repair_by_platform": _by_platform(needs),
            "empty_workspace_remaining": empty_ws,
        },
        "items": items,
        "category_counts": dict(by_cat),
        "preflight_counts": dict(by_pre),
        "proof_no_workspace_from_internal": proof,
        "legal_nexus_untouched": True,
    }


def _by_platform(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for row in rows:
        out[normalize_platform(_text(row.get("platform")))] += 1
    return dict(out)


def _plan_one(
    row: dict[str, Any],
    *,
    taken_ws: set[str],
    existing_nexus_ext: dict[tuple[str, str], list[str]],
    existing_ws_owners: dict[str, list[str]],
    by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    mid = _text(row.get("mod_id"))
    plat = normalize_platform(_text(row.get("platform")))
    ext = _text(row.get("external_id"))
    ws = _text(row.get("workspace_id"))
    url = _text(row.get("source_url"))
    app = _text(row.get("app_id"))
    proposed_ws = ws
    proposed_ext = ext
    proposed_url = url
    categories: list[str] = []
    reasons: list[str] = []
    confidence = "HIGH"
    blocking = ""
    leftover = ""

    if mid in LEGAL_NEXUS_INTERNAL:
        return _item(
            row,
            proposed_ws=ws,
            proposed_ext=ext,
            proposed_url=url,
            category="SKIP_LEGAL_NEXUS",
            reason="legal Nexus identity 3451/3452 must be preserved",
            status="SKIPPED",
            confidence="HIGH",
            blocking="legal_nexus_preserve",
        )

    if plat in CAT_A_PLATFORMS and not ws:
        new_ws = generate_unique_workspace_id(taken_ws)
        if _workspace_from_internal(mid, new_ws) or new_ws == mid:
            leftover = "generated_workspace_equals_internal_id"
            new_ws = ws
            confidence = "LOW"
        elif not new_ws:
            leftover = "workspace_generator_failed"
            confidence = "LOW"
        else:
            taken_ws.add(new_ws)
            proposed_ws = new_ws
            categories.append("A_GENERATE_WORKSPACE")
            reasons.append("empty workspace_id; generate unique Workspace ID")
        proposed_ext = ext

    if plat == PLATFORM_NEXUS and not (ext.isdigit() and not is_internal_mod_id(ext)):
        extracted = _nexus_numeric_id_from_url(url)
        if (
            extracted
            and extracted.isdigit()
            and not is_internal_mod_id(extracted)
            and extracted == ws
        ):
            owners = [
                oid
                for oid in existing_nexus_ext.get((app, extracted), [])
                if oid != mid
            ]
            if owners:
                leftover = f"nexus_external_id_collision:{','.join(owners)}"
            else:
                proposed_ext = extracted
                proposed_ws = ws
                categories.append("B_NEXUS_ALIGN_EXTERNAL")
                reasons.append(
                    "non-numeric external_id; URL /mods/<id> equals workspace_id"
                )
                existing_nexus_ext[(app, extracted)].append(mid)
        else:
            leftover = (
                "nexus_url_id_mismatch_or_missing"
                if extracted
                else "nexus_no_numeric_mods_id_in_url"
            )

    url_needed = _url_embeds_internal(url, mid) or mid in POLLUTED_URL_IDS
    if url_needed:
        new_url = _proposed_source_url(row)
        if plat == PLATFORM_STEAM and is_internal_mod_id(ext):
            leftover = leftover or "steam_external_is_internal_id"
            confidence = "HIGH"
        elif new_url == url and _url_embeds_internal(url, mid):
            leftover = leftover or "url_pollution_not_rewritable"
        else:
            proposed_url = new_url
            categories.append("C_SCRUB_STEAM_URL")
            reasons.append("source_url embeds Internal ID as fake Steam Workshop URL")

    if plat != PLATFORM_STEAM and _workspace_from_internal(mid, proposed_ws):
        leftover = "proposed_workspace_equals_internal_id"
        proposed_ws = ws
        categories = [c for c in categories if c != "A_GENERATE_WORKSPACE"]
    if proposed_ws and proposed_ws != ws:
        owners = [oid for oid in existing_ws_owners.get(proposed_ws, []) if oid != mid]
        if owners:
            leftover = f"workspace_id_collision:{','.join(owners)}"
            proposed_ws = ws
            categories = [c for c in categories if c != "A_GENERATE_WORKSPACE"]
        else:
            existing_ws_owners[proposed_ws].append(mid)
    if plat == PLATFORM_STEAM and proposed_ws and proposed_ext and proposed_ws != proposed_ext:
        leftover = "steam_workspace_external_mismatch"
    if plat == PLATFORM_NEXUS and "B_NEXUS_ALIGN_EXTERNAL" in categories:
        if proposed_ws != proposed_ext:
            leftover = "nexus_workspace_external_mismatch"
            proposed_ext = ext
            categories = [c for c in categories if c != "B_NEXUS_ALIGN_EXTERNAL"]

    has_change = proposed_ws != ws or proposed_ext != ext or proposed_url != url
    collision = leftover.startswith("nexus_external_id_collision") or leftover.startswith(
        "workspace_id_collision"
    )
    if has_change and not (
        leftover in {"proposed_workspace_equals_internal_id", "steam_external_is_internal_id"}
    ):
        status = "APPROVED"
        blocking = leftover
    elif collision or leftover == "steam_external_is_internal_id":
        status = "CONFLICT"
        blocking = leftover
        proposed_ws, proposed_ext, proposed_url = ws, ext, url
        categories = []
    else:
        status = "UNRESOLVED"
        blocking = leftover or "no_matching_repair_category"
        proposed_ws, proposed_ext, proposed_url = ws, ext, url
        categories = []
        if leftover.startswith("nexus_"):
            confidence = "MEDIUM"

    category = "+".join(categories) if categories else "NONE"
    return _item(
        row,
        proposed_ws=proposed_ws,
        proposed_ext=proposed_ext,
        proposed_url=proposed_url,
        category=category,
        reason="; ".join(reasons) or blocking,
        status=status,
        confidence=confidence,
        blocking=blocking,
    )


def _item(
    row: dict[str, Any],
    *,
    proposed_ws: str,
    proposed_ext: str,
    proposed_url: str,
    category: str,
    reason: str,
    status: str,
    confidence: str,
    blocking: str,
) -> dict[str, Any]:
    mid = _text(row.get("mod_id"))
    ws = _text(row.get("workspace_id"))
    ext = _text(row.get("external_id"))
    url = _text(row.get("source_url"))
    return {
        "internal_id": mid,
        "platform": normalize_platform(_text(row.get("platform"))),
        "app_id": _text(row.get("app_id")),
        "current_workspace_id": ws,
        "current_external_id": ext,
        "source_url": url,
        "proposed_workspace_id": proposed_ws,
        "proposed_external_id": proposed_ext,
        "proposed_source_url": proposed_url,
        "repair_reason": reason,
        "repair_category": category,
        "confidence": confidence,
        "preflight_status": status,
        "blocking_reason": blocking,
        "workspace_from_internal": _workspace_from_internal(mid, proposed_ws),
        "display_name": _text(row.get("display_name") or row.get("title")),
        "folder_path": _text(row.get("last_known_path")),
        "changes": {
            "workspace_id": ws != proposed_ws,
            "external_id": ext != proposed_ext,
            "source_url": url != proposed_url,
        },
    }


class _Readonly:
    """Minimal facade for audit_production_id_semantics."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = _DummyLock()


class _DummyLock:
    def __enter__(self) -> "_DummyLock":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _open_rw(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def backup_production_db(src: Path, stamp: str) -> dict[str, Any]:
    dest_dir = data_dir() / "p0_1b_id_semantic_repair_backup" / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "mod_manager.db"
    src_conn = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
            dest_conn.commit()
        finally:
            dest_conn.close()
    finally:
        src_conn.close()
    src_sha = _sha256(src) if src.is_file() else ""
    dest_sha = _sha256(dest)
    verify_conn = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True)
    try:
        n = verify_conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()[0]
        tables = [
            r[0]
            for r in verify_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
    finally:
        verify_conn.close()
    old_backup = data_dir() / "identity_repair_production_backup"
    manifest = {
        "created_at": _utc_now(),
        "source_db": str(src),
        "backup_db": str(dest),
        "source_sha256": src_sha,
        "backup_sha256": dest_sha,
        "mods_count": n,
        "tables": tables,
        "previous_identity_repair_backup_untouched": old_backup.is_dir(),
        "previous_identity_repair_backup_path": str(old_backup),
        "note": "SQLite snapshot only. This repair mutates identity columns, not mod folders.",
    }
    (dest_dir / "BACKUP_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def apply_plan(conn: sqlite3.Connection, plan: dict[str, Any], stamp: str) -> dict[str, Any]:
    now = _utc_now()
    approved = [
        i
        for i in plan["items"]
        if i["preflight_status"] == "APPROVED"
        and any(i["changes"].values())
    ]
    skipped = [
        i
        for i in plan["items"]
        if i["preflight_status"] != "APPROVED" or not any(i["changes"].values())
    ]
    mutations: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS identity_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mod_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value TEXT NOT NULL DEFAULT '',
            new_value TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
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
    )
    for item in approved:
        mid = item["internal_id"]
        if mid in LEGAL_NEXUS_INTERNAL:
            skipped.append(item)
            continue
        before = conn.execute(
            """
            SELECT mod_id, platform, workspace_id, external_id, source_url,
                   title, display_name, last_known_path
            FROM mods WHERE CAST(mod_id AS TEXT)=?
            """,
            (mid,),
        ).fetchone()
        if before is None:
            failed.append({"internal_id": mid, "error": "row_missing"})
            continue
        old_ws = _text(before["workspace_id"])
        old_ext = _text(before["external_id"])
        old_url = _text(before["source_url"])
        new_ws = _text(item["proposed_workspace_id"])
        new_ext = _text(item["proposed_external_id"])
        new_url = _text(item["proposed_source_url"])
        if _text(before["platform"]) != PLATFORM_STEAM and new_ws == mid:
            failed.append({"internal_id": mid, "error": "refuse_workspace_from_internal"})
            continue
        try:
            conn.execute(
                """
                UPDATE mods
                SET workspace_id = ?, external_id = ?, source_url = ?, updated_at = ?
                WHERE CAST(mod_id AS TEXT)=?
                """,
                (new_ws, new_ext, new_url, now, mid),
            )
            field_pairs = (
                ("workspace_id", old_ws, new_ws),
                ("external_id", old_ext, new_ext),
                ("source_url", old_url, new_url),
            )
            for field, old, new in field_pairs:
                if old == new:
                    continue
                conn.execute(
                    """
                    INSERT INTO identity_audit_log (
                        mod_id, field_name, old_value, new_value,
                        source, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (mid, field, old, new, SOURCE, item["repair_reason"], now),
                )
            conn.execute(
                """
                INSERT INTO identity_repair_audit (
                    created_at, operation, ghost_mod_id, canonical_mod_id,
                    platform, external_id, app_id, relationship, confidence,
                    action, reason, before_state, after_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    SOURCE,
                    mid,
                    mid,
                    item["platform"],
                    new_ext,
                    int(item.get("app_id") or 0),
                    item["repair_category"],
                    item["confidence"],
                    "APPLY_ID_SEMANTIC_REPAIR",
                    item["repair_reason"],
                    json.dumps(
                        {
                            "workspace_id": old_ws,
                            "external_id": old_ext,
                            "source_url": old_url,
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "workspace_id": new_ws,
                            "external_id": new_ext,
                            "source_url": new_url,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            mutations.append(
                {
                    "internal_id": mid,
                    "old_workspace_id": old_ws,
                    "new_workspace_id": new_ws,
                    "old_external_id": old_ext,
                    "new_external_id": new_ext,
                    "old_source_url": old_url,
                    "new_source_url": new_url,
                    "reason": item["repair_reason"],
                    "timestamp": now,
                    "repair_category": item["repair_category"],
                }
            )
        except sqlite3.Error as exc:
            failed.append({"internal_id": mid, "error": str(exc)})
    conn.commit()
    log_path = data_dir() / "p0_1b_id_semantic_repair_backup" / stamp / "mutations.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as handle:
        for row in mutations:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {
        "rows_changed": len(mutations),
        "rows_skipped": len(skipped),
        "rows_failed": len(failed),
        "failed": failed,
        "mutation_log": str(log_path),
        "mutations": mutations,
    }


def post_verify(conn: sqlite3.Connection) -> dict[str, Any]:
    facade = _Readonly(conn)
    audit = audit_production_id_semantics(facade)
    rows = _load_mod_rows(conn)
    ws_owners: dict[str, list[str]] = defaultdict(list)
    internal_to_ws = 0
    internal_to_ext = 0
    internal_to_url = 0
    steam_bad = 0
    nexus_bad = 0
    unknown_mods = 0
    for row in rows:
        mid = _text(row.get("mod_id"))
        plat = normalize_platform(_text(row.get("platform")))
        ext = _text(row.get("external_id"))
        ws = _text(row.get("workspace_id"))
        url = _text(row.get("source_url"))
        title = _text(row.get("title"))
        if ws:
            ws_owners[ws].append(mid)
        if plat != PLATFORM_STEAM and ws == mid:
            internal_to_ws += 1
        if plat != PLATFORM_STEAM and ext == mid and is_internal_mod_id(mid):
            internal_to_ext += 1
        if _url_embeds_internal(url, mid):
            internal_to_url += 1
        if plat == PLATFORM_STEAM:
            if ext and ws and ws != ext:
                steam_bad += 1
            if ext and is_internal_mod_id(ext):
                steam_bad += 1
        if plat == PLATFORM_NEXUS and ext.isdigit() and ws and ws != ext:
            nexus_bad += 1
        if title.lower().startswith("unknown mod"):
            unknown_mods += 1
    dup_ws = {k: v for k, v in ws_owners.items() if k and len(v) > 1}
    ghosts = {}
    for gid in HISTORICAL_GHOSTS:
        found = conn.execute(
            "SELECT mod_id FROM mods WHERE CAST(mod_id AS TEXT)=?",
            (gid,),
        ).fetchone()
        ghosts[gid] = found is not None
    legal = {}
    for gid in LEGAL_NEXUS_INTERNAL:
        found = conn.execute(
            """
            SELECT mod_id, platform, external_id, workspace_id
            FROM mods WHERE CAST(mod_id AS TEXT)=?
            """,
            (gid,),
        ).fetchone()
        legal[gid] = None if found is None else _row_dict(found)
    src = scan_id_architecture_source()
    return {
        "audit": {
            "scanned": audit.get("scanned"),
            "counts": audit.get("counts"),
            "non_valid": audit.get("non_valid") or [],
        },
        "invariants": {
            "internal_to_workspace": internal_to_ws,
            "internal_to_external": internal_to_ext,
            "internal_to_platform_url": internal_to_url,
            "ui_internal_id_exposure": len(src),
            "steam_consistency_violations": steam_bad,
            "nexus_consistency_violations": nexus_bad,
            "duplicate_workspace_ids": dup_ws,
        },
        "ghosts_present": [g for g, present in ghosts.items() if present],
        "legal_nexus": legal,
        "unknown_mod_titles": unknown_mods,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0-1B ID semantic repair")
    parser.add_argument("--mode", choices=("plan", "apply"), default="plan")
    parser.add_argument("--plan-file", type=Path, default=None)
    args = parser.parse_args(argv)
    dbp = database_path()
    if not dbp.is_file():
        print("NO_PRODUCTION_DB")
        return 2
    stamp = _utc_stamp()
    out_dir = data_dir() / "p0_1b_id_semantic_repair_backup" / stamp
    if args.mode == "plan":
        db = open_readonly_sqlite(dbp)
        try:
            plan = build_plan(db._conn)  # noqa: SLF001
        finally:
            db._conn.close()  # noqa: SLF001
        plan_path = out_dir / "repair_plan.json"
        _write_json(plan_path, plan)
        print("PLAN_WRITTEN", plan_path)
        print("FORENSIC_BEFORE", plan["forensic_before"])
        print("CATEGORY_COUNTS", plan["category_counts"])
        print("PREFLIGHT", plan["preflight_counts"])
        print("PROOF_NO_WS_FROM_INTERNAL", plan["proof_no_workspace_from_internal"])
        unresolved = [i for i in plan["items"] if i["preflight_status"] != "APPROVED"]
        for item in unresolved:
            print("NOT_APPROVED", item["internal_id"], item["preflight_status"], item["blocking_reason"])
        return 0

    if args.mode == "apply":
        if args.plan_file and args.plan_file.is_file():
            plan = json.loads(args.plan_file.read_text(encoding="utf-8"))
        else:
            db = open_readonly_sqlite(dbp)
            try:
                plan = build_plan(db._conn)  # noqa: SLF001
            finally:
                db._conn.close()  # noqa: SLF001
        plan_path = out_dir / "repair_plan.json"
        _write_json(plan_path, plan)
        print("PLAN_WRITTEN", plan_path)
        print("FORENSIC_BEFORE", plan["forensic_before"])
        print("PREFLIGHT", plan["preflight_counts"])
        if not plan.get("proof_no_workspace_from_internal"):
            print("ABORT proof_no_workspace_from_internal=false")
            return 3
        backup = backup_production_db(dbp, stamp)
        print("BACKUP", backup["backup_db"], "mods", backup["mods_count"])
        conn = _open_rw(dbp)
        try:
            apply_result = apply_plan(conn, plan, stamp)
            print("APPLY", {k: apply_result[k] for k in ("rows_changed", "rows_skipped", "rows_failed")})
            verify = post_verify(conn)
        finally:
            conn.close()
        report = {
            "plan_file": str(plan_path),
            "backup": backup,
            "apply": {
                "rows_changed": apply_result["rows_changed"],
                "rows_skipped": apply_result["rows_skipped"],
                "rows_failed": apply_result["rows_failed"],
                "failed": apply_result["failed"],
                "mutation_log": apply_result["mutation_log"],
            },
            "post_verify": verify,
        }
        report_path = out_dir / "apply_report.json"
        _write_json(report_path, report)
        print("REPORT", report_path)
        print("POST_COUNTS", verify["audit"]["counts"])
        print("INVARIANTS", verify["invariants"])
        print("GHOSTS_PRESENT", verify["ghosts_present"])
        non = verify["audit"]["non_valid"]
        print("REMAINING_NON_VALID", len(non))
        for row in non[:40]:
            print(row)
        return 0 if apply_result["rows_failed"] == 0 else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
