"""Identity Data Migration Phase 4-A — human review package only.

Reads ``identity_data_hygiene_report.json`` and emits repair *candidates*.

Never mutates DB / .info / backup.
Never generates UUID, merges, deletes, or updates workspace.
Never calls IdentityService / Reconcile / Import.

Outputs::

    tools/_audit_out/identity_migration_review.json
    tools/_audit_out/identity_migration_review.csv

Usage::

    python tools/identity_data_migration_review.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_AUDIT = ROOT / "tools" / "_audit_out" / "identity_data_hygiene_report.json"
OUT_DIR = ROOT / "tools" / "_audit_out"
DEFAULT_JSON = OUT_DIR / "identity_migration_review.json"
DEFAULT_CSV = OUT_DIR / "identity_migration_review.csv"

CATEGORIES = (
    "EMPTY_INTERNAL_ID",
    "WORKSPACE_EXTERNAL_MISMATCH",
    "EMPTY_WORKSPACE",
    "SAME_APP_WORKSPACE_CONFLICT",
)

ACTIONS = (
    "KEEP",
    "UPDATE_WORKSPACE_ONLY",
    "DELETE_POLLUTION",
    "MANUAL_REVIEW",
)

# Hard stop: this module must never contain write SQL or lifecycle creates.
_FORBIDDEN_SOURCE_PATTERNS = (
    r"\bUPDATE\s+\w+",
    r"\bINSERT\s+INTO\b",
    r"\bDELETE\s+FROM\b",
    r"\bDROP\s+TABLE\b",
    r"create_mod_identity\s*\(",
    r"reconcile_library\s*\(",
    r"allocate_internal_id\s*\(",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any) -> str:
    return str(value or "").strip()


def assert_tool_source_is_read_only(source: str | None = None) -> None:
    """Raise if this tool's source contains forbidden mutation patterns."""
    text = source if source is not None else Path(__file__).read_text(encoding="utf-8")
    # Strip module docstring + this checker’s pattern constant to avoid self-hits.
    body = re.sub(r'"""[\s\S]*?"""', '""""""', text, count=1)
    body = re.sub(
        r"_FORBIDDEN_SOURCE_PATTERNS\s*=\s*\([\s\S]*?\)\n",
        "_FORBIDDEN_SOURCE_PATTERNS = ()\n",
        body,
        count=1,
    )
    for pattern in _FORBIDDEN_SOURCE_PATTERNS:
        if re.search(pattern, body, re.I):
            raise RuntimeError(
                f"migration review tool must stay read-only; found {pattern}"
            )


def _url_digits(source_url: str) -> str:
    url = _text(source_url).lower()
    if not url:
        return ""
    try:
        path = urlparse(url).path or ""
    except Exception:  # noqa: BLE001
        path = url
    # Prefer trailing numeric path segment (Nexus / Steam / Mod.io style).
    parts = [p for p in path.split("/") if p]
    for part in reversed(parts):
        if part.isdigit():
            return part
    m = re.search(r"[?&]id=(\d+)", url)
    return m.group(1) if m else ""


def _backup_ref(mod_id: str) -> str:
    mid = _text(mod_id)
    if not mid:
        return ""
    return str(ROOT / "data" / "mod_backup" / mid / "metadata.json")


def _load_title_map(db_path: Path) -> dict[str, str]:
    """Read-only title lookup. Never writes."""
    if not db_path.is_file():
        return {}
    out: dict[str, str] = {}
    con = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        for row in con.execute("SELECT mod_id, title FROM mods"):
            out[str(row["mod_id"])] = _text(row["title"])
    finally:
        con.close()
    return out


def _recommend_empty_workspace(row: dict[str, Any]) -> tuple[str, str]:
    ext = _text(row.get("external_id"))
    url = _text(row.get("source_url"))
    url_digits = _url_digits(url)
    if ext.isdigit() and (not url_digits or url_digits == ext):
        return (
            "UPDATE_WORKSPACE_ONLY",
            f"suggest workspace_id={ext!r} from digit external_id (human must approve)",
        )
    if url_digits and ext and ext != url_digits:
        return (
            "MANUAL_REVIEW",
            "external_id is non-digit or disagrees with URL; do not auto-fill",
        )
    if not ext:
        return ("MANUAL_REVIEW", "empty workspace and empty external")
    return (
        "MANUAL_REVIEW",
        "external_id present but not a safe digit registration number",
    )


def _recommend_mismatch(row: dict[str, Any]) -> tuple[str, str]:
    ws = _text(row.get("workspace_id"))
    ext = _text(row.get("external_id"))
    url_digits = _url_digits(_text(row.get("source_url")))
    plat = _text(row.get("platform")).lower()

    # Generated-looking workspace vs digit external + URL proof → pollution candidate
    if ext.isdigit() and url_digits == ext and ws and ws != ext:
        if len(ws) >= 14 and ws.isdigit():
            return (
                "UPDATE_WORKSPACE_ONLY",
                f"workspace looks generated ({ws}); URL/external agree on {ext}",
            )
        return (
            "MANUAL_REVIEW",
            f"workspace={ws!r} != external={ext!r}; URL favors {url_digits or 'n/a'}",
        )
    if url_digits == ws and ws:
        return (
            "KEEP",
            "workspace_id matches URL; leave external_id as legacy audit only",
        )
    if plat in ("github", "modio", "other") and ws and ext and ws != ext:
        return (
            "KEEP",
            "non-Nexus/Steam: workspace may be display id; external is platform path key",
        )
    return ("MANUAL_REVIEW", "cannot decide workspace vs external without stronger proof")


def _candidate(
    *,
    category: str,
    identity: dict[str, Any],
    evidence: dict[str, Any],
    recommended_action: str,
    rationale: str,
    case_id: str,
) -> dict[str, Any]:
    assert category in CATEGORIES
    assert recommended_action in ACTIONS
    return {
        "case_id": case_id,
        "identity": identity,
        "problem": {"category": category},
        "evidence": evidence,
        "recommended_action": recommended_action,
        "rationale": rationale,
        "requires_manual_confirm": True,
        "auto_apply": False,
        "forbidden": [
            "generate_uuid",
            "merge_mods",
            "delete_mods",
            "update_workspace_without_approval",
            "call_IdentityService",
            "call_Reconcile",
            "call_Import",
        ],
    }


def build_review(
    audit: dict[str, Any],
    *,
    titles: dict[str, str] | None = None,
) -> dict[str, Any]:
    titles = titles or {}
    items: list[dict[str, Any]] = []

    mismatch = audit.get("workspace_external_mismatch") or {}
    for row in mismatch.get("A_empty_workspace_has_external") or []:
        mid = _text(row.get("mod_id"))
        action, why = _recommend_empty_workspace(row)
        items.append(
            _candidate(
                category="EMPTY_WORKSPACE",
                case_id=f"EMPTY_WS-{mid}",
                identity={
                    "internal_id": _text(row.get("internal_id")) or mid,
                    "mod_id": mid,
                    "platform": _text(row.get("platform")),
                    "app_id": int(row.get("app_id") or 0),
                    "workspace_id": _text(row.get("workspace_id")),
                    "external_id": _text(row.get("external_id")),
                    "title": titles.get(mid, ""),
                },
                evidence={
                    "db_row": dict(row),
                    "info_path": _text(row.get("info_path")),
                    "backup_reference": _backup_ref(mid),
                    "related_mods": [],
                },
                recommended_action=action,
                rationale=why,
            )
        )

    for row in mismatch.get("B_workspace_ne_external") or []:
        mid = _text(row.get("mod_id"))
        action, why = _recommend_mismatch(row)
        items.append(
            _candidate(
                category="WORKSPACE_EXTERNAL_MISMATCH",
                case_id=f"MISMATCH-{mid}",
                identity={
                    "internal_id": _text(row.get("internal_id")) or mid,
                    "mod_id": mid,
                    "platform": _text(row.get("platform")),
                    "app_id": int(row.get("app_id") or 0),
                    "workspace_id": _text(row.get("workspace_id")),
                    "external_id": _text(row.get("external_id")),
                    "title": titles.get(mid, ""),
                },
                evidence={
                    "db_row": dict(row),
                    "info_path": _text(row.get("info_path")),
                    "backup_reference": _backup_ref(mid),
                    "related_mods": [],
                },
                recommended_action=action,
                rationale=why,
            )
        )

    integrity = audit.get("internal_id_integrity") or {}
    for row in integrity.get("db_internal_id_empty") or []:
        mid = _text(row.get("mod_id"))
        items.append(
            _candidate(
                category="EMPTY_INTERNAL_ID",
                case_id=f"EMPTY_IID-{mid}",
                identity={
                    "internal_id": "",
                    "mod_id": mid,
                    "platform": "",
                    "app_id": 0,
                    "workspace_id": "",
                    "external_id": "",
                    "title": titles.get(mid, ""),
                },
                evidence={
                    "db_row": dict(row),
                    "info_path": _text(row.get("info_path")),
                    "backup_reference": _backup_ref(mid),
                    "related_mods": [],
                },
                recommended_action="MANUAL_REVIEW",
                rationale=(
                    "DB internal_id empty — hygiene must NOT mint UUID; escalate manually"
                ),
            )
        )

    cross = audit.get("cross_game_workspace_report") or {}
    for row in cross.get("same_app_conflict") or []:
        entities = list(row.get("entities") or [])
        related = [
            {
                "mod_id": _text(e.get("mod_id")),
                "internal_id": _text(e.get("internal_id")),
                "app_id": int(e.get("app_id") or 0),
            }
            for e in entities
        ]
        for ent in entities:
            mid = _text(ent.get("mod_id"))
            items.append(
                _candidate(
                    category="SAME_APP_WORKSPACE_CONFLICT",
                    case_id=f"CONFLICT-{row.get('platform')}-{row.get('app_id')}-{row.get('workspace_id')}-{mid}",
                    identity={
                        "internal_id": _text(ent.get("internal_id")) or mid,
                        "mod_id": mid,
                        "platform": _text(row.get("platform")),
                        "app_id": int(row.get("app_id") or 0),
                        "workspace_id": _text(row.get("workspace_id")),
                        "external_id": "",
                        "title": titles.get(mid, ""),
                    },
                    evidence={
                        "db_row": dict(row),
                        "info_path": "",
                        "backup_reference": _backup_ref(mid),
                        "related_mods": [r for r in related if r.get("mod_id") != mid],
                    },
                    recommended_action="MANUAL_REVIEW",
                    rationale=(
                        "Same (platform, app_id, workspace_id) maps to multiple entities; "
                        "do NOT merge or delete automatically"
                    ),
                )
            )

    by_cat: dict[str, int] = {}
    by_action: dict[str, int] = {}
    for item in items:
        cat = str(item["problem"]["category"])
        act = str(item["recommended_action"])
        by_cat[cat] = by_cat.get(cat, 0) + 1
        by_action[act] = by_action.get(act, 0) + 1

    return {
        "generated_at": _now(),
        "phase": "identity_data_migration_4a",
        "mode": "review_only",
        "production_mutation": "NONE",
        "auto_apply": False,
        "source_audit": _text(audit.get("db_path")),
        "counts": {
            "candidates": len(items),
            "by_category": by_cat,
            "by_recommended_action": by_action,
        },
        "note": (
            "Human approval document only. Suggestions must not be applied by this tool."
        ),
        "candidates": items,
    }


def flatten_csv_rows(review: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in review.get("candidates") or []:
        ident = item.get("identity") or {}
        ev = item.get("evidence") or {}
        related = ev.get("related_mods") or []
        rows.append(
            {
                "case_id": str(item.get("case_id") or ""),
                "internal_id": str(ident.get("internal_id") or ""),
                "mod_id": str(ident.get("mod_id") or ""),
                "platform": str(ident.get("platform") or ""),
                "app_id": str(ident.get("app_id") or ""),
                "workspace_id": str(ident.get("workspace_id") or ""),
                "external_id": str(ident.get("external_id") or ""),
                "title": str(ident.get("title") or ""),
                "category": str((item.get("problem") or {}).get("category") or ""),
                "recommended_action": str(item.get("recommended_action") or ""),
                "info_path": str(ev.get("info_path") or ""),
                "backup_reference": str(ev.get("backup_reference") or ""),
                "related_mods": ";".join(
                    str(r.get("mod_id") or r.get("internal_id") or "") for r in related
                ),
                "rationale": str(item.get("rationale") or ""),
                "requires_manual_confirm": "true",
            }
        )
    return rows


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "case_id",
        "internal_id",
        "mod_id",
        "platform",
        "app_id",
        "workspace_id",
        "external_id",
        "title",
        "category",
        "recommended_action",
        "info_path",
        "backup_reference",
        "related_mods",
        "rationale",
        "requires_manual_confirm",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def run_review(
    *,
    audit_path: Path,
    out_json: Path,
    out_csv: Path,
    db_path: Path | None = None,
) -> dict[str, Any]:
    assert_tool_source_is_read_only()
    if not audit_path.is_file():
        raise FileNotFoundError(
            f"hygiene report missing: {audit_path} "
            "(run tools/identity_data_hygiene_audit.py first)"
        )
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    resolved_db = Path(db_path or _text(audit.get("db_path")) or ROOT / "data" / "mod_manager.db")
    db_before = resolved_db.read_bytes() if resolved_db.is_file() else b""
    sha_before = hashlib.sha256(db_before).hexdigest() if db_before else ""

    titles = _load_title_map(resolved_db) if resolved_db.is_file() else {}
    review = build_review(audit, titles=titles)
    review["db_path"] = str(resolved_db)
    review["db_sha256_before"] = sha_before
    review["audit_path"] = str(audit_path)

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(flatten_csv_rows(review), out_csv)

    db_after = resolved_db.read_bytes() if resolved_db.is_file() else b""
    sha_after = hashlib.sha256(db_after).hexdigest() if db_after else ""
    review["db_sha256_after"] = sha_after
    review["db_bytes_unchanged"] = db_before == db_after
    # Concurrent live app may write; this tool never opens a write connection.
    review["tool_write_connection"] = False
    out_json.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    return review


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Identity migration review package (no data changes)"
    )
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--out-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--out-csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Optional DB for read-only title enrichment",
    )
    args = parser.parse_args(argv)
    review = run_review(
        audit_path=args.audit,
        out_json=args.out_json,
        out_csv=args.out_csv,
        db_path=args.db,
    )
    print(
        json.dumps(
            {
                "out_json": str(args.out_json),
                "out_csv": str(args.out_csv),
                "candidates": review["counts"]["candidates"],
                "by_category": review["counts"]["by_category"],
                "by_recommended_action": review["counts"]["by_recommended_action"],
                "db_bytes_unchanged": review.get("db_bytes_unchanged"),
                "production_mutation": "NONE",
                "auto_apply": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
