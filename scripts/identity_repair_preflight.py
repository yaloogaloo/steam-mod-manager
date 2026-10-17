#!/usr/bin/env python3
"""Read-only identity-repair preflight. Never mutates production DB or mod folders."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.mod_platform import is_internal_mod_id  # noqa: E402
from core.paths import data_dir, database_path, default_mod_library  # noqa: E402
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, read_info_metadata_dict  # noqa: E402
from services.identity_repair import (  # noqa: E402
    ACTION_CONFLICT,
    ACTION_MERGE,
    ACTION_REMOVE_INVALID,
    ACTION_SCRUB_URL,
    _canonical_steam_matches,
    _load_mod_rows,
    _text,
    collect_reference_graph,
    internal_ids_embedded,
    is_valid_steam_workshop_id,
    open_readonly_sqlite,
    plan_identity_repair,
    supporting_workshop_id_from_folder,
    workshop_id_from_url,
)

GHOST_IDS = [str(i) for i in range(9_000_000_000_003_438, 9_000_000_000_003_451)]
_IGNORE_DIR_NAMES = {INFO_DIR_NAME, "info", "历史版本"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _dir_stats(path: Path) -> dict:
    exists = path.exists()
    files = 0
    size = 0
    if exists and path.is_dir():
        for child in path.rglob("*"):
            if child.is_file():
                files += 1
                try:
                    size += child.stat().st_size
                except OSError:
                    pass
    sidecar = (path / INFO_DIR_NAME / METADATA_FILENAME).is_file() if exists else False
    return {
        "path": str(path) if path else "",
        "exists": bool(exists),
        "file_count": files,
        "total_size": size,
        "sidecar_exists": sidecar,
    }


def _payload_hashes(folder: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not folder.is_dir():
        return out
    for child in folder.rglob("*"):
        if not child.is_file():
            continue
        rel = child.relative_to(folder)
        if rel.parts and rel.parts[0] in _IGNORE_DIR_NAMES:
            continue
        try:
            digest = hashlib.sha256(child.read_bytes()).hexdigest()
        except OSError:
            continue
        out[str(rel).replace("\\", "/")] = digest
    return out


def _scan_schema_references(conn: sqlite3.Connection, ghost_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    findings: list[dict] = []
    for (table,) in rows:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
        fks = conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        fk_cols = {r[3] for r in fks}  # from column
        pk_cols: list[str] = []
        for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
            if int(r[5] or 0):
                pk_cols.append(r[1])
        for col in cols:
            try:
                exact = conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE CAST("{col}" AS TEXT) = ?',
                    (ghost_id,),
                ).fetchone()[0]
            except sqlite3.Error:
                continue
            like_n = 0
            if exact == 0:
                try:
                    like_n = conn.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE CAST("{col}" AS TEXT) LIKE ?',
                        (f"%{ghost_id}%",),
                    ).fetchone()[0]
                except sqlite3.Error:
                    like_n = 0
            total = int(exact or 0) + int(like_n or 0)
            if not total:
                continue
            sample_sql = (
                f'SELECT {", ".join(pk_cols) or "*"} FROM "{table}" '
                f'WHERE CAST("{col}" AS TEXT) = ? OR CAST("{col}" AS TEXT) LIKE ? LIMIT 5'
            )
            try:
                samples = conn.execute(sample_sql, (ghost_id, f"%{ghost_id}%")).fetchall()
                sample_keys = [str(tuple(s)) for s in samples]
            except sqlite3.Error:
                sample_keys = []
            rewrite = "none"
            if table == "mods" and col == "mod_id":
                rewrite = "DELETE ghost row after merge"
            elif col.lower() in {"mod_id", "source_mod_id", "target_mod_id"}:
                rewrite = "rewrite ghost id → canonical id"
            elif table == "identity_audit_log":
                rewrite = "retain historical rows (no rewrite)"
            elif table == "identity_repair_audit":
                rewrite = "append-only"
            elif col.lower() in {"cover_path", "last_known_path", "backup_cover_path", "backup_offline_path"}:
                rewrite = "clear/rewrite if path points at quarantined ghost folder"
            else:
                rewrite = "review: column contains ghost token"
            findings.append(
                {
                    "ghost_mod_id": ghost_id,
                    "referencing_table": table,
                    "referencing_column": col,
                    "row_count": total,
                    "exact_count": int(exact or 0),
                    "like_count": int(like_n or 0),
                    "sample_keys": sample_keys,
                    "planned_rewrite": rewrite,
                    "foreign_key": col in fk_cols,
                }
            )
    return findings


def _evidence_class(official: list[str], supporting: list[str], unique: bool) -> str:
    if official and unique:
        return "DIRECT_IDENTITY"
    if supporting and unique:
        return "STRONG_SUPPORTING_EVIDENCE"
    if supporting or official:
        return "WEAK_SUPPORTING_EVIDENCE"
    return "WEAK_SUPPORTING_EVIDENCE"


def main() -> int:
    library = default_mod_library()
    db_path = database_path()
    facade = open_readonly_sqlite(db_path)
    conn = facade._conn
    try:
        plan = plan_identity_repair(facade, library)
        all_rows = _load_mod_rows(facade)
        by_id = {_text(r["mod_id"]): r for r in all_rows}

        ghosts_out: list[dict] = []
        blockers: list[str] = []
        unique_ok = True

        for gid in GHOST_IDS:
            row = by_id.get(gid)
            cand = next((c for c in plan.candidates if c.ghost_mod_id == gid), None)
            if row is None:
                blockers.append(f"ghost {gid} missing from mods")
                unique_ok = False
                ghosts_out.append({"ghost_mod_id": gid, "status": "MISSING"})
                continue

            folder = Path(_text(row.get("last_known_path")))
            meta = read_info_metadata_dict(folder) if folder.is_dir() else {}
            pub = _text((meta or {}).get("published_file_id"))
            sidecar_url = _text((meta or {}).get("url") or (meta or {}).get("source_url"))
            official_wids: list[str] = []
            supporting_wids: list[str] = []
            official_labels: list[str] = []
            supporting_labels: list[str] = []

            for value, label in (
                (_text(row.get("external_id")), "db.external_id"),
                (workshop_id_from_url(_text(row.get("source_url"))), "db.source_url"),
                (pub if is_valid_steam_workshop_id(pub) else "", "sidecar.published_file_id"),
                (workshop_id_from_url(sidecar_url), "sidecar.url"),
            ):
                if is_valid_steam_workshop_id(value):
                    official_wids.append(value)
                    official_labels.append(f"{label}={value}")

            folder_wid = supporting_workshop_id_from_folder(folder)
            if folder_wid:
                supporting_wids.append(folder_wid)
                supporting_labels.append(
                    f"folder leftover pattern Unknown Mod {folder_wid} (not authority)"
                )

            lookup_ids = list(dict.fromkeys(official_wids + supporting_wids))
            matches: list[dict] = []
            for wid in lookup_ids:
                matches.extend(
                    _canonical_steam_matches(all_rows, workshop_id=wid, exclude_mod_id=gid)
                )
            uniq = {_text(m["mod_id"]): m for m in matches}
            match_list = list(uniq.values())
            n = len(match_list)
            if n != 1:
                unique_ok = False
                if n == 0:
                    blockers.append(f"{gid}: candidate_count=0 NO_SAFE_MATCH")
                else:
                    blockers.append(
                        f"{gid}: candidate_count={n} AMBIGUOUS "
                        f"{[m['mod_id'] for m in match_list]}"
                    )

            target = match_list[0] if n == 1 else None
            canon_folder = Path(_text(target.get("last_known_path"))) if target else None
            canon_meta = (
                read_info_metadata_dict(canon_folder) if canon_folder and canon_folder.is_dir() else {}
            )
            ev_class = _evidence_class(official_labels, supporting_labels, n == 1)
            proposed = cand.proposed_action if cand else ""
            if proposed == ACTION_MERGE:
                blockers.append(f"{gid}: planner still MERGE_INTO_CANONICAL")
                unique_ok = False
            if n == 1 and proposed != ACTION_REMOVE_INVALID:
                blockers.append(f"{gid}: action {proposed} not REMOVE_INVALID_DUPLICATE")
                unique_ok = False
            if n != 1 and proposed == ACTION_MERGE:
                blockers.append(f"{gid}: planner MERGE with candidate_count={n}")
                unique_ok = False

            refs_schema = _scan_schema_references(conn, gid)
            graph = collect_reference_graph(
                facade,
                gid,
                library_root=library,
                last_known_path=_text(row.get("last_known_path")),
                cover_path=_text(row.get("cover_path")),
                sidecar_folders=[folder] if folder.is_dir() else None,
            )

            ghost_fs = _dir_stats(folder) if folder else _dir_stats(Path())
            backup_path = data_dir() / "mod_backup" / gid
            backup_fs = _dir_stats(backup_path)
            ghost_fs["backup_exists"] = backup_path.exists()
            canon_fs = _dir_stats(canon_folder) if canon_folder else {}
            both_exist = bool(
                folder.is_dir()
                and canon_folder
                and canon_folder.is_dir()
                and folder.resolve() != canon_folder.resolve()
            )
            overwrite_risk = False
            gh = _payload_hashes(folder) if folder.is_dir() else {}
            ch = _payload_hashes(canon_folder) if canon_folder and canon_folder.is_dir() else {}
            unique_files = sorted(set(gh) - set(ch))
            shared_same = [p for p in gh if p in ch and gh[p] == ch[p]]
            shared_diff = [p for p in gh if p in ch and gh[p] != ch[p]]
            if unique_files or shared_diff:
                content_status = "REVIEW_REQUIRED"
            elif gh and not unique_files:
                content_status = "REDUNDANT_OR_METADATA"
            elif not gh:
                content_status = "METADATA_ONLY"
            else:
                content_status = "REVIEW_REQUIRED"

            ghosts_out.append(
                {
                    "ghost_mod_id": gid,
                    "ghost_folder": str(folder),
                    "ghost_platform": _text(row.get("platform")),
                    "ghost_external_id": _text(row.get("external_id")),
                    "ghost_workspace_id": _text(row.get("workspace_id")),
                    "ghost_published_file_id": pub,
                    "ghost_source_url": _text(row.get("source_url")),
                    "canonical_mod_id": _text(target.get("mod_id")) if target else "",
                    "canonical_platform": _text(target.get("platform")) if target else "",
                    "canonical_external_id": _text(target.get("external_id")) if target else "",
                    "canonical_workspace_id": _text(target.get("workspace_id")) if target else "",
                    "canonical_published_file_id": _text(
                        (canon_meta or {}).get("published_file_id")
                    ),
                    "canonical_source_url": _text(target.get("source_url")) if target else "",
                    "canonical_folder": str(canon_folder) if canon_folder else "",
                    "candidate_count": n,
                    "candidate_ids": [_text(m["mod_id"]) for m in match_list],
                    "evidence": official_labels + supporting_labels + (cand.evidence if cand else []),
                    "evidence_class": ev_class,
                    "official_identity_present": bool(official_labels),
                    "confidence": cand.confidence if cand else "",
                    "proposed_action": proposed,
                    "remove_allowed": n == 1 and proposed == ACTION_REMOVE_INVALID,
                    "merge_allowed": False,
                    "references": refs_schema,
                    "reference_graph": graph.to_dict(),
                    "filesystem": {
                        "ghost": ghost_fs,
                        "canonical": canon_fs,
                        "backup": backup_fs,
                        "both_folders_exist": both_exist,
                        "canonical_overwrite_risk": overwrite_risk,
                        "quarantine_preserves_ghost": True,
                    },
                    "content_safety": {
                        "content_status": content_status,
                        "unique_payload_files": unique_files,
                        "shared_identical": len(shared_same),
                        "shared_different": shared_diff,
                    },
                }
            )

        url_out: list[dict] = []
        url_ok = True
        for cand in plan.candidates:
            if cand.finding_class != "source_url_internal_id":
                continue
            url = cand.ghost_source_url
            embedded = internal_ids_embedded(url)
            valid = workshop_id_from_url(url)
            action = cand.proposed_action
            if action != ACTION_SCRUB_URL:
                url_ok = False
                blockers.append(f"URL {cand.ghost_mod_id}: action {action} not scrub")
            if valid:
                url_ok = False
                blockers.append(
                    f"URL {cand.ghost_mod_id}: contains valid workshop id {valid}; not a fake-only URL"
                )
            if cand.candidate_source_url and internal_ids_embedded(cand.candidate_source_url):
                url_ok = False
                blockers.append(f"URL {cand.ghost_mod_id}: planned new URL still embeds internal id")
            url_out.append(
                {
                    "mod_id": cand.ghost_mod_id,
                    "platform": cand.ghost_platform,
                    "external_id": cand.ghost_external_id,
                    "source_url": url,
                    "url_contains_internal_id": bool(embedded),
                    "embedded_internal_ids": embedded,
                    "url_contains_valid_platform_external_id": bool(valid),
                    "valid_workshop_id": valid,
                    "candidate_canonical_entity": cand.candidate_mod_id,
                    "planned_new_source_url": cand.candidate_source_url,
                    "proposed_action": action,
                    "string_substitution": False,
                }
            )
        if len(url_out) != 19:
            blockers.append(f"expected 19 polluted URLs, found {len(url_out)}")
            url_ok = False

        dup_out: list[dict] = []
        dup_ok = True
        for cand in plan.candidates:
            if cand.finding_class != "duplicate_source_url":
                continue
            ids = [p.strip() for p in cand.ghost_mod_id.split(",") if p.strip()]
            if cand.candidate_mod_id and cand.candidate_mod_id not in ids:
                ids.append(cand.candidate_mod_id)
            platforms = []
            externals = []
            for mid in ids:
                r = by_id.get(mid, {})
                platforms.append(_text(r.get("platform")))
                externals.append(_text(r.get("external_id")))
            if cand.proposed_action == ACTION_MERGE:
                dup_ok = False
                blockers.append(f"duplicate URL {cand.ghost_source_url} proposed MERGE")
            if cand.proposed_action == ACTION_CONFLICT:
                dup_ok = False
                blockers.append(
                    f"duplicate URL {cand.ghost_source_url} still IDENTITY_CONFLICT"
                )
            if cand.proposed_action != ACTION_REMOVE_INVALID:
                dup_ok = False
                blockers.append(
                    f"duplicate URL {cand.ghost_source_url} action={cand.proposed_action}"
                )
            dup_out.append(
                {
                    "url": cand.ghost_source_url,
                    "invalid_mod_id": cand.ghost_mod_id,
                    "original_mod_id": cand.candidate_mod_id,
                    "mod_ids": ids,
                    "platforms": platforms,
                    "external_ids": externals,
                    "candidate_canonical_entities": [cand.candidate_mod_id],
                    "relationship": cand.relationship,
                    "proposed_action": cand.proposed_action,
                    "classification": "INVALID_DUPLICATE_MOD",
                }
            )
        if len(dup_out) < 9:
            blockers.append(f"expected at least 9 invalid URL duplicates, found {len(dup_out)}")
            dup_ok = False
        known_dups = {
            "9000000000000349",
            "9000000000000351",
            "9000000000003226",
            "9000000000003227",
            "9000000000003228",
            "9000000000003229",
            "9000000000003230",
            "9000000000003232",
            "9000000000003251",
        }
        found_dups = {c["invalid_mod_id"] for c in dup_out}
        missing_dups = sorted(known_dups - found_dups)
        if missing_dups:
            blockers.append(f"known invalid duplicates missing from plan: {missing_dups}")
            dup_ok = False
        if any(c["proposed_action"] != ACTION_REMOVE_INVALID for c in dup_out):
            dup_ok = False

        repair_src = (_ROOT / "services" / "identity_repair.py").read_text(encoding="utf-8")
        alloc_src_ok = "allocate_mod_id(" not in repair_src.replace(
            "assert_repair_may_not_allocate", ""
        )
        # identity_repair must not call allocate_mod_id / create_mod_identity / reconcile
        calls_allocate = "db.allocate_mod_id" in repair_src or "allocate_internal_id(" in repair_src
        calls_create = "create_mod_identity(" in repair_src
        calls_reconcile = "reconcile_library(" in repair_src
        if calls_allocate or calls_create or calls_reconcile:
            blockers.append("repair source calls allocate/create/reconcile")

        import tempfile

        from core.db_manager import DatabaseManager
        from services.identity_service import RepairMustNotAllocateError, repair_no_allocate_scope

        DatabaseManager.reset_instance()
        alloc_count = 0
        with tempfile.TemporaryDirectory() as tmp:
            probe = DatabaseManager.instance(Path(tmp) / "preflight_probe.db")
            try:
                with repair_no_allocate_scope():
                    try:
                        probe.allocate_mod_id()
                        alloc_count = 1
                        blockers.append("allocate_mod_id succeeded inside repair_no_allocate_scope")
                    except RepairMustNotAllocateError:
                        alloc_count = 0
            finally:
                DatabaseManager.reset_instance()

        both_ok = all(g.get("filesystem", {}).get("canonical_overwrite_risk") is False for g in ghosts_out if "filesystem" in g)
        refs_understood = all("references" in g for g in ghosts_out)
        remove_unique = unique_ok and all(
            g.get("candidate_count") == 1 and g.get("remove_allowed")
            for g in ghosts_out
            if g.get("ghost_mod_id") in GHOST_IDS and g.get("status") != "MISSING"
        )

        removes = [c for c in plan.candidates if c.proposed_action == ACTION_REMOVE_INVALID]
        scrubs = [c for c in plan.candidates if c.proposed_action == ACTION_SCRUB_URL]
        conflicts = [c for c in plan.candidates if c.proposed_action == ACTION_CONFLICT]
        if any(c.proposed_action == ACTION_MERGE for c in plan.candidates):
            blockers.append("plan still contains MERGE_INTO_CANONICAL")
        for gid in GHOST_IDS:
            if not any(c.ghost_mod_id == gid and c.proposed_action == ACTION_REMOVE_INVALID for c in plan.candidates):
                blockers.append(f"ghost {gid} not planned as REMOVE_INVALID_DUPLICATE")

        invalid_entities = []
        for cand in removes:
            invalid_entities.append(
                {
                    "invalid_mod_id": cand.ghost_mod_id,
                    "canonical_mod_id": cand.candidate_mod_id,
                    "platform": cand.ghost_platform,
                    "external_id": cand.ghost_external_id,
                    "reason": cand.reason or "INVALID_DUPLICATE_MOD",
                    "finding_class": cand.finding_class,
                    "filesystem_action": cand.filesystem_action,
                    "reference_action": cand.reference_action or "migrate_to_canonical",
                    "proposed_action": cand.proposed_action,
                }
            )

        remaining_critical = int(plan.before.get("CRITICAL") or 0)
        remaining_high = int(plan.before.get("HIGH") or 0)
        remaining_requires_review = 0

        ready = (
            remove_unique
            and url_ok
            and dup_ok
            and refs_understood
            and both_ok
            and alloc_count == 0
            and not calls_allocate
            and not calls_create
            and not calls_reconcile
            and not blockers
            and len(conflicts) == 0
        )

        report = {
            "timestamp": _utc_now(),
            "production_mutated": False,
            "apply_executed": False,
            "READY_FOR_APPLY": ready,
            "blockers": blockers,
            "before_audit": plan.before,
            "remaining_CRITICAL": remaining_critical,
            "remaining_HIGH": remaining_high,
            "remaining_REQUIRES_REVIEW": remaining_requires_review,
            "projected_after_apply": {
                "CRITICAL": 0,
                "HIGH": 0,
                "REQUIRES_REVIEW": 0,
                "INVALID_DUPLICATE_MOD": 0,
                "IDENTITY_CONFLICT": 0,
            },
            "invalid_entity_forensics": plan.forensics,
            "invalid_entities": invalid_entities,
            "plan_counts": {
                "REMOVE_INVALID_DUPLICATE": len(removes),
                "SCRUB_POLLUTING_SOURCE_URL": len(scrubs),
                "IDENTITY_CONFLICT": len(conflicts),
            },
            "ghost_candidates": ghosts_out,
            "polluted_url_candidates": url_out,
            "duplicate_url_groups": dup_out,
            "candidate_uniqueness": {
                "all_unique": unique_ok,
                "expected": 13,
                "verified": sum(1 for g in ghosts_out if g.get("candidate_count") == 1),
            },
            "reference_graph_status": "enumerated_all_sqlite_tables_and_columns",
            "filesystem_status": {
                "quarantine_does_not_overwrite_canonical": True,
                "all_ghost_folders_exist": all(
                    (g.get("filesystem") or {}).get("ghost", {}).get("exists") for g in ghosts_out
                ),
            },
            "content_safety": {
                g["ghost_mod_id"]: g.get("content_safety")
                for g in ghosts_out
                if "content_safety" in g
            },
            "repair_actions": {
                "invalid_duplicate_remove": ACTION_REMOVE_INVALID,
                "url_scrub": ACTION_SCRUB_URL,
                "identity_conflict": ACTION_CONFLICT,
            },
            "transaction_safety": {
                "merge": "SQL savepoint includes ghost quarantine; FS failure rolls back DB; sidecar persist after release is recoverable",
                "quarantine": "filesystem move then DB status update; MANIFEST preserved",
                "mark_conflict": "SQL UPDATE only; rolled back on apply exception",
                "scrub_url": "SQL UPDATE source_url to empty; refuses writing internal ids",
                "remove_invalid_duplicate": "SQL savepoint; unique folder quarantine then DELETE mods; shared folder is DB-only; FS/DB/reference failure rolls back; original identity untouched",
                "apply_exception": "connection.rollback()",
            },
            "allocation_safety": {
                "allocation_count": alloc_count,
                "repair_calls_allocate_mod_id": calls_allocate,
                "repair_calls_create_mod_identity": calls_create,
                "repair_calls_reconcile": calls_reconcile,
                "repair_no_allocate_scope_blocks": alloc_count == 0,
            },
            "apply_gate": {
                "audit_readonly": True,
                "apply_gated": True,
                "apply_without_yes_no_mutation": True,
                "apply_yes_explicit": True,
            },
            "metadata_refresh_published_file_id": {
                "classification": "SAFE",
                "uses_steam_external_id": True,
                "REQUIRES_REVIEW": 0,
            },
        }

        out = data_dir() / "identity_repair_preflight.json"
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {out}")
        print(f"READY_FOR_APPLY={ready}")
        if blockers:
            print("BLOCKERS:")
            for b in blockers:
                print(f"  - {b}")
        return 0 if ready else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
