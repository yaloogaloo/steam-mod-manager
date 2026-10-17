#!/usr/bin/env python3
"""Phase 2 — build old_identity → new_internal_id migration mapping.

One-shot. Never mutates production data. Never uses title/folder-name as identity.

Match priority (migration evidence only):
1. Backup DB row ↔ live row via unique (app_id>0, workspace_id)
2. Backup last_known_path ↔ live last_known_path (auxiliary; must not invent identity)
3. Backup UUID internal_id echoed in live .info is impossible post-rebuild —
   path/workspace evidence only.

Usage::

    python tools/identity_rebuild_mapping.py
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

OUT_DIR = ROOT / "tools" / "_audit_out"
DEFAULT_DB = ROOT / "data" / "mod_manager.db"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(v: Any) -> str:
    return str(v or "").strip()


def _norm_path(p: Any) -> str:
    raw = _text(p)
    if not raw:
        return ""
    try:
        return str(Path(raw).resolve()).lower().replace("/", "\\")
    except OSError:
        return raw.lower().replace("/", "\\")


def _latest_backup(data_dir: Path) -> Path | None:
    cands = sorted(data_dir.glob("mod_manager.db.pre_identity_rebuild_*"))
    return cands[-1] if cands else None


def _load_mods(db_path: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(mods)")}
        want = [
            "mod_id",
            "internal_id",
            "workspace_id",
            "app_id",
            "last_known_path",
            "title",
            "display_name",
            "favorite",
            "enabled",
            "cover_path",
            "description",
            "custom_description",
            "user_notes",
            "offline_status",
            "offline_provider",
            "platform",
            "source_url",
            "external_id",
            "deploy_status",
            "deploy_path",
            "custom_deploy_path",
        ]
        select = ", ".join(c for c in want if c in cols)
        return [dict(r) for r in con.execute(f"SELECT {select} FROM mods")]
    finally:
        con.close()


def _old_identity(row: dict[str, Any]) -> str:
    iid = _text(row.get("internal_id"))
    if iid:
        return iid
    return _text(row.get("mod_id"))


def build_mapping(*, live_db: Path, backup_db: Path) -> dict[str, Any]:
    bak_rows = _load_mods(backup_db)
    live_rows = _load_mods(live_db)

    live_by_key: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    live_by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in live_rows:
        app_id = int(row.get("app_id") or 0)
        ws = _text(row.get("workspace_id"))
        if app_id > 0 and ws:
            live_by_key[(app_id, ws)].append(row)
        np = _norm_path(row.get("last_known_path"))
        if np:
            live_by_path[np].append(row)

    mappings: list[dict[str, Any]] = []
    used_new: set[str] = set()
    stats = {"verified": 0, "auxiliary": 0, "unresolved": 0, "collision_skipped": 0}

    for bak in bak_rows:
        old_id = _old_identity(bak)
        old_mod_id = _text(bak.get("mod_id"))
        app_id = int(bak.get("app_id") or 0)
        ws = _text(bak.get("workspace_id"))
        np = _norm_path(bak.get("last_known_path"))

        candidate: dict[str, Any] | None = None
        confidence = "unresolved"
        evidence: list[str] = []

        key_hits = live_by_key.get((app_id, ws), []) if app_id > 0 and ws else []
        if len(key_hits) == 1:
            candidate = key_hits[0]
            confidence = "verified"
            evidence.append("unique_app_workspace")

        path_hits = live_by_path.get(np, []) if np else []
        if candidate is None and len(path_hits) == 1:
            live = path_hits[0]
            live_ws = _text(live.get("workspace_id"))
            live_app = int(live.get("app_id") or 0)
            # Path is auxiliary only — accept when workspace agrees, or bak app_id
            # was unset (historical pollution) but workspace digits match.
            if ws and live_ws and ws == live_ws:
                candidate = live
                confidence = "verified"
                evidence.append("path_plus_workspace")
            elif (app_id <= 0 or live_app <= 0) and ws and live_ws and ws == live_ws:
                candidate = live
                confidence = "auxiliary"
                evidence.append("path_plus_workspace_app_gap")
            elif not ws and live_ws:
                # No workspace on bak — path alone is weak; mark auxiliary.
                candidate = live
                confidence = "auxiliary"
                evidence.append("path_only_empty_bak_workspace")
        elif candidate is not None and path_hits:
            # Strengthen verified with path agreement when available.
            if any(_norm_path(h.get("last_known_path")) == np for h in path_hits):
                evidence.append("path_agrees")

        if candidate is None:
            stats["unresolved"] += 1
            mappings.append(
                {
                    "old_internal_id": old_id,
                    "old_mod_id": old_mod_id,
                    "new_internal_id": "",
                    "new_mod_id": "",
                    "confidence": "unresolved",
                    "app_id": app_id,
                    "workspace_id": ws,
                    "evidence": evidence,
                    "backup_path": _text(bak.get("last_known_path")),
                }
            )
            continue

        new_uuid = _text(candidate.get("internal_id"))
        new_mod_id = _text(candidate.get("mod_id"))
        if new_uuid in used_new:
            stats["collision_skipped"] += 1
            mappings.append(
                {
                    "old_internal_id": old_id,
                    "old_mod_id": old_mod_id,
                    "new_internal_id": "",
                    "new_mod_id": "",
                    "confidence": "unresolved",
                    "app_id": app_id,
                    "workspace_id": ws,
                    "evidence": evidence + ["new_internal_id_already_mapped"],
                    "backup_path": _text(bak.get("last_known_path")),
                }
            )
            continue

        used_new.add(new_uuid)
        stats[confidence if confidence in stats else "auxiliary"] = (
            stats.get(confidence, 0) + 1
        )
        mappings.append(
            {
                "old_internal_id": old_id,
                "old_mod_id": old_mod_id,
                "new_internal_id": new_uuid,
                "new_mod_id": new_mod_id,
                "confidence": confidence,
                "app_id": int(candidate.get("app_id") or app_id or 0),
                "workspace_id": _text(candidate.get("workspace_id")) or ws,
                "evidence": evidence,
                "backup_path": _text(bak.get("last_known_path")),
                "live_path": _text(candidate.get("last_known_path")),
            }
        )

    return {
        "generated_at": _now(),
        "tool": "identity_rebuild_mapping",
        "live_db": str(live_db.resolve()),
        "backup_db": str(backup_db.resolve()),
        "counts": {
            "backup_mods": len(bak_rows),
            "live_mods": len(live_rows),
            **stats,
            "mapped": stats["verified"] + stats["auxiliary"],
        },
        "mappings": mappings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backup", type=Path, default=None)
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT_DIR / "identity_rebuild_mapping.json",
    )
    args = parser.parse_args()
    backup = args.backup or _latest_backup(args.db.parent)
    if backup is None or not Path(backup).is_file():
        print("backup DB not found", file=sys.stderr)
        return 2
    report = build_mapping(live_db=args.db, backup_db=Path(backup))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote={args.out}")
    print(f"counts={report['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
