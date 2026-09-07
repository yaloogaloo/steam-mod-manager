"""Identity Model Convergence — Phase 0 read-only cleanup audit.

Never mutates DB / .info / backup / lifecycle code.

Outputs::

    tools/_audit_out/identity_cleanup_audit.json
    tools/_audit_out/identity_cleanup_audit.csv

Usage::

    python tools/identity_cleanup_audit.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT_JSON = ROOT / "tools" / "_audit_out" / "identity_cleanup_audit.json"
DEFAULT_OUT_CSV = ROOT / "tools" / "_audit_out" / "identity_cleanup_audit.csv"

SCAN_ROOTS = ("core", "services", "ui", "scripts", "tools")
SKIP_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "_audit_out",
    "node_modules",
}

IDENTITY_FIELDS = (
    "internal_id",
    "workspace_id",
    "external_id",
    "published_file_id",
    "workshop_id",
    "sidecar_published_file_id",
)

FIELD_CLASS = {
    "internal_id": "ENTITY_ID",
    "workspace_id": "REGISTRATION_KEY",
    "external_id": "LEGACY_IDENTITY",
    "published_file_id": "LEGACY_IDENTITY",
    "workshop_id": "LEGACY_IDENTITY",
    "sidecar_published_file_id": "LEGACY_IDENTITY",
}

QUERY_PATTERNS = (
    ("find_mod_by_external", r"find_mod_by_external\s*\("),
    ("find_mod_by_workspace_id", r"find_mod_by_workspace_id\s*\("),
    ("find_mod_id_by_workspace_id", r"find_mod_id_by_workspace_id\s*\("),
    ("find_mod_by_internal_id", r"find_mod_by_internal_id\s*\("),
    ("find_by_published_id", r"find_by_published_id\s*\("),
    ("find_mod_for_registration", r"find_mod_for_registration\s*\("),
    ("create_mod_identity", r"create_mod_identity\s*\("),
    ("ensure_mod_identity", r"ensure_mod_identity\s*\("),
    ("resolve_existing_mod_id", r"resolve_existing_mod_id\s*\("),
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _module_of(path: Path) -> str:
    try:
        rel = path.resolve().relative_to(ROOT)
    except ValueError:
        return str(path)
    return rel.as_posix()


def _iter_py_files() -> list[Path]:
    out: list[Path] = []
    for root_name in SCAN_ROOTS:
        base = ROOT / root_name
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            out.append(path)
    return sorted(out)


def _strip_comments(source: str) -> str:
    stripped = re.sub(r'"""[\s\S]*?"""', "", source)
    stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
    stripped = re.sub(r"#.*?$", "", stripped, flags=re.M)
    return stripped


def scan_code_identity_usage() -> dict[str, Any]:
    field_hits: dict[str, list[dict[str, Any]]] = {f: [] for f in IDENTITY_FIELDS}
    query_hits: list[dict[str, Any]] = []
    write_hits: list[dict[str, Any]] = []

    write_re = re.compile(
        r"(external_id|published_file_id|workshop_id|workspace_id|internal_id)\s*=",
        re.M,
    )

    for path in _iter_py_files():
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        body = _strip_comments(src)
        mod = _module_of(path)
        lines = src.splitlines()

        for field in IDENTITY_FIELDS:
            for i, line in enumerate(lines, start=1):
                if field in line and not line.lstrip().startswith("#"):
                    field_hits[field].append(
                        {
                            "file": mod,
                            "line": i,
                            "snippet": line.strip()[:200],
                            "classification": FIELD_CLASS[field],
                        }
                    )

        for name, pattern in QUERY_PATTERNS:
            for match in re.finditer(pattern, body):
                # approximate line via raw search
                pos = src.find(match.group(0))
                line_no = src[:pos].count("\n") + 1 if pos >= 0 else 0
                query_hits.append(
                    {
                        "api": name,
                        "file": mod,
                        "line": line_no,
                        "needs_migration": name
                        in {
                            "find_mod_by_external",
                            "find_by_published_id",
                            "find_mod_by_workspace_id",
                            "find_mod_id_by_workspace_id",
                        },
                    }
                )

        for match in write_re.finditer(body):
            field = match.group(1)
            pos = match.start()
            line_no = body[:pos].count("\n") + 1
            write_hits.append(
                {
                    "field": field,
                    "file": mod,
                    "line": line_no,
                    "classification": FIELD_CLASS.get(field, "METADATA"),
                }
            )

    field_summary = {
        field: {
            "classification": FIELD_CLASS[field],
            "hit_count": len(field_hits[field]),
            "files": sorted({h["file"] for h in field_hits[field]}),
            "sample_hits": field_hits[field][:40],
        }
        for field in IDENTITY_FIELDS
    }
    api_counts = Counter(h["api"] for h in query_hits)
    return {
        "fields": field_summary,
        "queries": query_hits,
        "query_counts": dict(api_counts),
        "writes": write_hits[:500],
        "write_counts_by_field": dict(Counter(h["field"] for h in write_hits)),
    }


def scan_db_data(db_path: Path) -> dict[str, Any]:
    if not db_path.is_file():
        return {"error": f"missing db: {db_path}"}
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(mods)")}
        required = {"mod_id", "internal_id", "workspace_id", "external_id", "platform", "app_id"}
        if not required.issubset(cols):
            return {"error": "mods schema missing identity columns", "cols": sorted(cols)}

        total = con.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
        with_external = con.execute(
            "SELECT COUNT(*) FROM mods WHERE TRIM(COALESCE(external_id,'')) != ''"
        ).fetchone()[0]
        with_ws = con.execute(
            "SELECT COUNT(*) FROM mods WHERE TRIM(COALESCE(workspace_id,'')) != ''"
        ).fetchone()[0]
        empty_ws_with_ext = con.execute(
            """
            SELECT COUNT(*) FROM mods
            WHERE TRIM(COALESCE(workspace_id,'')) = ''
              AND TRIM(COALESCE(external_id,'')) != ''
            """
        ).fetchone()[0]
        ext_ne_ws = con.execute(
            """
            SELECT COUNT(*) FROM mods
            WHERE TRIM(COALESCE(external_id,'')) != ''
              AND TRIM(COALESCE(workspace_id,'')) != ''
              AND TRIM(external_id) != TRIM(workspace_id)
            """
        ).fetchone()[0]

        cross_ext = con.execute(
            """
            SELECT platform, external_id, COUNT(DISTINCT app_id) AS games, COUNT(*) AS rows
            FROM mods
            WHERE TRIM(COALESCE(external_id,'')) != ''
            GROUP BY platform, external_id
            HAVING COUNT(DISTINCT app_id) > 1
            ORDER BY rows DESC
            LIMIT 50
            """
        ).fetchall()
        cross_ws = con.execute(
            """
            SELECT platform, workspace_id, COUNT(DISTINCT app_id) AS games, COUNT(*) AS rows
            FROM mods
            WHERE TRIM(COALESCE(workspace_id,'')) != ''
            GROUP BY platform, workspace_id
            HAVING COUNT(DISTINCT app_id) > 1
            ORDER BY rows DESC
            LIMIT 50
            """
        ).fetchall()
        app0 = con.execute(
            """
            SELECT COUNT(*) FROM mods
            WHERE COALESCE(app_id, 0) = 0
              AND TRIM(COALESCE(external_id,'')) != ''
            """
        ).fetchone()[0]

        return {
            "mods_count": total,
            "with_external_id": with_external,
            "with_workspace_id": with_ws,
            "empty_workspace_with_external": empty_ws_with_ext,
            "external_ne_workspace": ext_ne_ws,
            "app_id_0_with_external": app0,
            "cross_game_same_external": [
                {
                    "platform": r["platform"],
                    "external_id": r["external_id"],
                    "games": r["games"],
                    "rows": r["rows"],
                }
                for r in cross_ext
            ],
            "cross_game_same_workspace": [
                {
                    "platform": r["platform"],
                    "workspace_id": r["workspace_id"],
                    "games": r["games"],
                    "rows": r["rows"],
                }
                for r in cross_ws
            ],
        }
    finally:
        con.close()


def build_delete_keep_lists(code: dict[str, Any]) -> dict[str, Any]:
    keep = [
        {
            "name": "internal_id / mods.mod_id",
            "role": "ENTITY_ID",
            "reason": "sole entity authority",
        },
        {
            "name": "workspace_id",
            "role": "REGISTRATION_KEY + display",
            "reason": "Sync/Import registration only via (platform, app_id, workspace_id); display elsewhere",
        },
        {
            "name": "find_mod_by_internal_id",
            "role": "ENTITY_LOOKUP",
            "reason": "only entity finder",
        },
        {
            "name": "find_mod_for_registration",
            "role": "REGISTRATION_LOOKUP",
            "reason": "Sync/Import/create gate only (to be added in Phase 1)",
        },
        {
            "name": "find_mod_by_workspace_id (noop)",
            "role": "GUARD",
            "reason": "permanent no-op prevents regressions",
        },
        {
            "name": "create_mod_identity",
            "role": "CREATE_GATE",
            "reason": "sole mint entry",
        },
        {
            "name": "last_known_path / folder_present",
            "role": "STORAGE",
            "reason": "path bind only; not identity",
        },
        {
            "name": "source_url",
            "role": "METADATA",
            "reason": "metadata / UI; not entity key",
        },
    ]
    delete = [
        {
            "name": "external_id as identity",
            "role": "LEGACY_IDENTITY",
            "action": "stop reading for identity; migrate digits into workspace_id; drop later",
            "query_count": code.get("query_counts", {}).get("find_mod_by_external", 0),
        },
        {
            "name": "published_file_id as identity",
            "role": "LEGACY_IDENTITY",
            "action": "Sync temp parse only; remove find_by_published_id identity use",
            "query_count": code.get("query_counts", {}).get("find_by_published_id", 0),
        },
        {
            "name": "workshop_id as identity",
            "role": "LEGACY_IDENTITY",
            "action": "Import/Sync kwargs temp only",
        },
        {
            "name": "sidecar_published_file_id",
            "role": "LEGACY_IDENTITY",
            "action": "delete helper; do not write into .info",
        },
        {
            "name": "path / folder-name identity",
            "role": "LEGACY_IDENTITY",
            "action": "already forbidden; keep guards",
        },
        {
            "name": "unscoped workspace entity lookup",
            "role": "LEGACY_IDENTITY",
            "action": "keep API as permanent None",
            "query_count": code.get("query_counts", {}).get("find_mod_by_workspace_id", 0),
        },
    ]
    return {"keep": keep, "delete_or_retire": delete}


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "category",
        "name",
        "classification",
        "file",
        "line",
        "needs_migration",
        "notes",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, "") for c in cols})


def flatten_csv_rows(report: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for field, info in (report.get("code_scan", {}).get("fields") or {}).items():
        rows.append(
            {
                "category": "field",
                "name": field,
                "classification": str(info.get("classification") or ""),
                "file": ";".join(info.get("files") or [])[:500],
                "line": "",
                "needs_migration": "yes"
                if info.get("classification") == "LEGACY_IDENTITY"
                else "no",
                "notes": f"hits={info.get('hit_count')}",
            }
        )
    for q in report.get("code_scan", {}).get("queries") or []:
        rows.append(
            {
                "category": "query",
                "name": str(q.get("api") or ""),
                "classification": "QUERY",
                "file": str(q.get("file") or ""),
                "line": str(q.get("line") or ""),
                "needs_migration": "yes" if q.get("needs_migration") else "no",
                "notes": "",
            }
        )
    for item in report.get("policy", {}).get("delete_or_retire") or []:
        rows.append(
            {
                "category": "retire",
                "name": str(item.get("name") or ""),
                "classification": str(item.get("role") or ""),
                "file": "",
                "line": "",
                "needs_migration": "yes",
                "notes": str(item.get("action") or ""),
            }
        )
    for item in report.get("policy", {}).get("keep") or []:
        rows.append(
            {
                "category": "keep",
                "name": str(item.get("name") or ""),
                "classification": str(item.get("role") or ""),
                "file": "",
                "line": "",
                "needs_migration": "no",
                "notes": str(item.get("reason") or ""),
            }
        )
    return rows


def run_audit(*, db_path: Path, out_json: Path, out_csv: Path) -> dict[str, Any]:
    db_bytes_before = db_path.read_bytes() if db_path.is_file() else b""
    code = scan_code_identity_usage()
    data = scan_db_data(db_path)
    policy = build_delete_keep_lists(code)
    report = {
        "generated_at": _now(),
        "phase": "identity_convergence_0",
        "mode": "read_only",
        "production_mutation": "NONE",
        "db_path": str(db_path),
        "db_bytes_unchanged": True,
        "db_sha256_before": hashlib.sha256(db_bytes_before).hexdigest()
        if db_bytes_before
        else "",
        "code_scan": code,
        "db_scan": data,
        "policy": policy,
        "next_phases": [
            "Phase 1: find_mod_for_registration(platform, app_id, workspace_id)",
            "Phase 2: hot-path purge of external/published/workshop identity",
            "Phase 3: stop using legacy fields; migration report; no DROP yet",
        ],
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(flatten_csv_rows(report), out_csv)

    db_bytes_after = db_path.read_bytes() if db_path.is_file() else b""
    if db_bytes_before != db_bytes_after:
        raise RuntimeError("Phase 0 violated read-only: DB bytes changed")
    report["db_sha256_after"] = (
        hashlib.sha256(db_bytes_after).hexdigest() if db_bytes_after else ""
    )
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Identity cleanup Phase 0 audit")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "mod_manager.db")
    parser.add_argument("--out-json", type=Path, default=DEFAULT_OUT_JSON)
    parser.add_argument("--out-csv", type=Path, default=DEFAULT_OUT_CSV)
    args = parser.parse_args(argv)
    report = run_audit(db_path=args.db, out_json=args.out_json, out_csv=args.out_csv)
    summary = {
        "phase": report["phase"],
        "mods_count": (report.get("db_scan") or {}).get("mods_count"),
        "with_external_id": (report.get("db_scan") or {}).get("with_external_id"),
        "cross_game_external": len(
            (report.get("db_scan") or {}).get("cross_game_same_external") or []
        ),
        "find_mod_by_external_calls": (report.get("code_scan") or {})
        .get("query_counts", {})
        .get("find_mod_by_external", 0),
        "out_json": str(args.out_json),
        "out_csv": str(args.out_csv),
        "db_bytes_unchanged": True,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
