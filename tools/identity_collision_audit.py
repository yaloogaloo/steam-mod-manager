"""Identity Collision Recovery — Phase 1 read-only audit.

Scans DB + managed ``.info`` for historical identity pollution.

Does NOT mutate data. Does NOT guess identity from folder/path.
Does NOT call Sync / Import / Reconcile / Deploy / create_mod_identity.

Usage::

    python tools/identity_collision_audit.py
    python tools/identity_collision_audit.py --db PATH --library PATH --out PATH
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.identity_collision_common import (  # noqa: E402
    CODE_EXTERNAL_COLLISION,
    CODE_INFO_DB_MISMATCH,
    CODE_INTERNAL_ID_COLLISION,
    CODE_WORKSPACE_COLLISION,
    db_summary,
    default_paths,
    dump_json,
    entity_key,
    identity_fingerprint,
    info_summary,
    iter_managed_folders,
    load_db_rows,
    norm_path,
    now_utc,
    read_info,
    text,
)


def _finding(
    *,
    code: str,
    reason: str,
    old_internal_id: str = "",
    affected_paths: list[str] | None = None,
    db_record: dict[str, Any] | None = None,
    info_records: list[dict[str, Any]] | None = None,
    evidence: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "reason": reason,
        "old_internal_id": old_internal_id,
        "affected_paths": list(affected_paths or []),
        "db_record": dict(db_record or {}),
        "info_records": list(info_records or []),
        "evidence": list(evidence or []),
        "extra": dict(extra or {}),
    }


def run_audit(*, db_path: Path, library: Path) -> dict[str, Any]:
    rows = load_db_rows(db_path)
    by_entity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_mod_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        mid = text(row.get("mod_id"))
        if mid:
            by_mod_id[mid] = row
        key = entity_key(row)
        if key:
            by_entity[key].append(row)

    findings: list[dict[str, Any]] = []

    # --- WORKSPACE_COLLISION (DB display collision across games) ---
    by_ws: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        ws = text(row.get("workspace_id"))
        if ws:
            by_ws[ws].append(row)
    for ws, group in by_ws.items():
        apps = {int(g.get("app_id") or 0) for g in group}
        if len(group) > 1 and len(apps) > 1:
            findings.append(
                _finding(
                    code=CODE_WORKSPACE_COLLISION,
                    reason=(
                        f"workspace_id={ws!r} spans app_ids={sorted(apps)} "
                        "(display collision; not an entity key)"
                    ),
                    old_internal_id=entity_key(group[0]),
                    db_record=db_summary(group[0]),
                    evidence=[
                        f"mod_id={text(g.get('mod_id'))} app_id={int(g.get('app_id') or 0)} "
                        f"title={text(g.get('title') or g.get('display_name'))!r}"
                        for g in group
                    ],
                    extra={
                        "workspace_id": ws,
                        "rows": [db_summary(g) for g in group],
                    },
                )
            )

    # --- EXTERNAL_COLLISION (same platform+external across games) ---
    by_ext: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        plat = text(row.get("platform")).lower()
        ext = text(row.get("external_id"))
        if not plat or not ext or ext.startswith("local/") or ext.startswith("stub:"):
            continue
        by_ext[(plat, ext)].append(row)
    for (plat, ext), group in by_ext.items():
        apps = {int(g.get("app_id") or 0) for g in group}
        if len(group) > 1 and len(apps) > 1:
            findings.append(
                _finding(
                    code=CODE_EXTERNAL_COLLISION,
                    reason=(
                        f"(platform={plat}, external_id={ext}) spans "
                        f"app_ids={sorted(apps)}"
                    ),
                    old_internal_id=entity_key(group[0]),
                    db_record=db_summary(group[0]),
                    evidence=[
                        f"mod_id={text(g.get('mod_id'))} app_id={int(g.get('app_id') or 0)} "
                        f"url={text(g.get('source_url'))!r}"
                        for g in group
                    ],
                    extra={
                        "platform": plat,
                        "external_id": ext,
                        "rows": [db_summary(g) for g in group],
                    },
                )
            )

    # --- Scan disk .info ---
    info_by_iid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    folders = iter_managed_folders(library)
    for folder in folders:
        payload, info_path = read_info(folder)
        if payload is None or payload.get("_read_error"):
            continue
        summary = info_summary(payload, folder, info_path)
        iid = text(summary.get("internal_id"))
        if not iid:
            continue
        info_by_iid[iid].append(summary)

    # --- INTERNAL_ID_COLLISION: same .info/entity_key, divergent fingerprints ---
    for iid, infos in info_by_iid.items():
        fps = {identity_fingerprint(i) for i in infos}
        if len(infos) < 2:
            continue
        if len(fps) < 2 and len({text(i.get("folder")) for i in infos}) < 2:
            continue
        # Multiple folders share one internal_id → collision when fingerprints
        # differ OR when >1 folder exists (merged disk proof).
        if len(fps) > 1 or len(infos) > 1:
            db_hits = by_entity.get(iid) or (
                [by_mod_id[iid]] if iid in by_mod_id else []
            )
            findings.append(
                _finding(
                    code=CODE_INTERNAL_ID_COLLISION,
                    reason=(
                        f"internal_id={iid!r} appears on {len(infos)} .info folders "
                        f"with {len(fps)} distinct identity fingerprints"
                    ),
                    old_internal_id=iid,
                    affected_paths=[text(i.get("folder")) for i in infos],
                    db_record=db_summary(db_hits[0]) if db_hits else {},
                    info_records=infos,
                    evidence=[
                        f"folder={i.get('folder')} title={i.get('title')!r} "
                        f"app_id={i.get('app_id')} url={i.get('source_url')!r} "
                        f"fp={identity_fingerprint(i)}"
                        for i in infos
                    ],
                    extra={
                        "db_rows": [db_summary(r) for r in db_hits],
                        "fingerprint_count": len(fps),
                    },
                )
            )

    # --- INFO_DB_IDENTITY_MISMATCH ---
    for iid, infos in info_by_iid.items():
        db_hits = by_entity.get(iid) or ([by_mod_id[iid]] if iid in by_mod_id else [])
        if not db_hits:
            continue
        db = db_summary(db_hits[0])
        for info in infos:
            mismatches: list[str] = []
            db_title = text(db.get("title")).casefold()
            info_title = text(info.get("title")).casefold()
            if db_title and info_title and db_title != info_title:
                mismatches.append(f"title db={db.get('title')!r} info={info.get('title')!r}")
            db_app = int(db.get("app_id") or 0)
            info_app = int(info.get("app_id") or 0)
            if db_app > 0 and info_app > 0 and db_app != info_app:
                mismatches.append(f"app_id db={db_app} info={info_app}")
            db_url = text(db.get("source_url")).lower()
            info_url = text(info.get("source_url")).lower()
            if db_url and info_url and db_url != info_url:
                mismatches.append("source_url differs")
            db_ext = text(db.get("external_id"))
            info_ext = text(info.get("external_id"))
            if db_ext and info_ext and db_ext != info_ext:
                mismatches.append(f"external_id db={db_ext!r} info={info_ext!r}")
            # Path ownership mismatch: DB last_known_path != this folder but
            # .info claims DB's internal_id (classic merged foreign folder).
            lkp = norm_path(db.get("last_known_path"))
            folder_n = norm_path(info.get("folder"))
            if lkp and folder_n and lkp != folder_n and mismatches:
                pass  # already mismatched
            if not mismatches:
                continue
            findings.append(
                _finding(
                    code=CODE_INFO_DB_MISMATCH,
                    reason="; ".join(mismatches),
                    old_internal_id=iid,
                    affected_paths=[text(info.get("folder"))],
                    db_record=db,
                    info_records=[info],
                    evidence=mismatches,
                )
            )

    by_code: dict[str, int] = defaultdict(int)
    for f in findings:
        by_code[text(f.get("code"))] += 1

    return {
        "phase": "collision_audit",
        "generated_at": now_utc(),
        "db_path": str(db_path.resolve()),
        "library": str(library.resolve()),
        "production_mutation": "NONE",
        "counts": {
            "db_rows": len(rows),
            "managed_folders": len(folders),
            "info_with_internal_id": sum(len(v) for v in info_by_iid.values()),
            "findings": len(findings),
            "by_code": dict(by_code),
        },
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    db_default, lib_default, out_dir = default_paths(ROOT)
    parser = argparse.ArgumentParser(description="Identity collision read-only audit")
    parser.add_argument("--db", type=Path, default=db_default)
    parser.add_argument("--library", type=Path, default=lib_default)
    parser.add_argument(
        "--out",
        type=Path,
        default=out_dir / "identity_collision_report.json",
    )
    args = parser.parse_args(argv)
    report = run_audit(db_path=args.db, library=args.library)
    dump_json(args.out, report)
    print(f"wrote {args.out}")
    print(f"findings={report['counts']['findings']} by_code={report['counts']['by_code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
