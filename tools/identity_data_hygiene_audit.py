"""Identity Data Hygiene — Phase 1 read-only audit.

Never mutates DB / .info / backup / lifecycle code.
Never creates, deletes, or merges Mods.

Outputs::

    tools/_audit_out/identity_data_hygiene_report.json
    tools/_audit_out/identity_data_hygiene_report.csv
    tools/_audit_out/identity_boundary_usage_report.json

Usage::

    python tools/identity_data_hygiene_audit.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
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
DEFAULT_OUT_JSON = OUT_DIR / "identity_data_hygiene_report.json"
DEFAULT_OUT_CSV = OUT_DIR / "identity_data_hygiene_report.csv"
DEFAULT_BOUNDARY_JSON = OUT_DIR / "identity_boundary_usage_report.json"

SCAN_ROOTS = ("core", "services", "ui", "scripts")
SKIP_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "_audit_out",
    "node_modules",
    "tests",
}

INFO_DIR = ".info"
METADATA_NAME = "metadata.json"

# Call sites classified for boundary usage report.
LEGAL_REGISTRATION_APIS = frozenset(
    {
        "find_mod_for_registration",
    }
)
ENTITY_LOOKUP_APIS = frozenset(
    {
        "find_mod_by_internal_id",
        "find_mod_by_workspace_id",
        "find_mod_id_by_workspace_id",
        "find_mod_by_external",
        "find_by_published_id",
        "resolve_existing_mod_id",
        "ensure_mod_identity",
    }
)

# Modules allowed to call registration API.
LEGAL_REGISTRATION_MODULES = (
    "services/sync.py",
    "services/importers/",
    "services/identity_service.py",
    "services/mod_identity_authority.py",
    "services/importers/duplicate_check.py",
    "services/identity_pollution.py",  # conflict gate → registration alias
    "services/mod_identity_repair.py",  # conflict gate → registration alias
    "core/db_manager.py",  # definition / deprecated alias only
)

# Static analyzers / docs that mention APIs without calling them as identity.
SKIP_BOUNDARY_MODULES = (
    "services/identity_invariants.py",
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


def _info_path_for(last_known_path: str) -> str:
    root = _text(last_known_path)
    if not root:
        return ""
    return str(Path(root) / INFO_DIR / METADATA_NAME)


def _read_info_internal_id(info_path: str) -> str:
    path = Path(info_path) if info_path else Path()
    if not path.is_file():
        return ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(raw, dict):
        return ""
    return _text(raw.get("internal_id"))


def scan_workspace_external_mismatch(con: sqlite3.Connection) -> dict[str, Any]:
    rows = con.execute(
        """
        SELECT mod_id, internal_id, platform, app_id, workspace_id, external_id,
               source_url, last_known_path
        FROM mods
        """
    ).fetchall()
    class_a: list[dict[str, Any]] = []
    class_b: list[dict[str, Any]] = []
    class_c: list[dict[str, Any]] = []
    for row in rows:
        ws = _text(row["workspace_id"])
        ext = _text(row["external_id"])
        info_path = _info_path_for(str(row["last_known_path"] or ""))
        base = {
            "mod_id": str(row["mod_id"]),
            "internal_id": _text(row["internal_id"]) or str(row["mod_id"]),
            "platform": _text(row["platform"]),
            "app_id": int(row["app_id"] or 0),
            "workspace_id": ws,
            "external_id": ext,
            "source_url": _text(row["source_url"]),
            "info_path": info_path,
            "last_known_path": _text(row["last_known_path"]),
        }
        if not ws and ext:
            class_a.append({**base, "class": "A_EMPTY_WORKSPACE_HAS_EXTERNAL"})
        elif ws and ext and ws != ext:
            class_b.append({**base, "class": "B_WORKSPACE_NE_EXTERNAL"})
        elif not ext:
            class_c.append({**base, "class": "C_EMPTY_EXTERNAL"})
    return {
        "A_empty_workspace_has_external": class_a,
        "B_workspace_ne_external": class_b,
        "C_empty_external": class_c,
        "counts": {
            "A": len(class_a),
            "B": len(class_b),
            "C": len(class_c),
            "total_mods": len(rows),
        },
    }


def scan_cross_game_workspace(con: sqlite3.Connection) -> dict[str, Any]:
    """(platform, workspace_id) across app_ids — allowed when app_ids differ."""
    rows = con.execute(
        """
        SELECT platform, workspace_id, app_id, mod_id, internal_id
        FROM mods
        WHERE TRIM(COALESCE(workspace_id, '')) != ''
          AND TRIM(COALESCE(platform, '')) != ''
        """
    ).fetchall()
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (_text(row["platform"]).lower(), _text(row["workspace_id"]))
        by_key[key].append(
            {
                "mod_id": str(row["mod_id"]),
                "internal_id": _text(row["internal_id"]) or str(row["mod_id"]),
                "app_id": int(row["app_id"] or 0),
            }
        )

    cross_game_ok: list[dict[str, Any]] = []
    same_app_conflict: list[dict[str, Any]] = []
    for (plat, ws), items in sorted(by_key.items()):
        app_ids = {int(i["app_id"] or 0) for i in items}
        if len(items) <= 1:
            continue
        # Same app_id with multiple entities sharing workspace → conflict
        by_app: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            by_app[int(item["app_id"] or 0)].append(item)
        for aid, group in by_app.items():
            if len(group) > 1:
                same_app_conflict.append(
                    {
                        "platform": plat,
                        "workspace_id": ws,
                        "app_id": aid,
                        "entities": group,
                        "status": "SAME_APP_CONFLICT",
                    }
                )
        if len(app_ids) > 1:
            cross_game_ok.append(
                {
                    "platform": plat,
                    "workspace_id": ws,
                    "app_ids": sorted(app_ids),
                    "entities": items,
                    "status": "CROSS_GAME_ALLOWED",
                }
            )
    return {
        "cross_game_allowed": cross_game_ok,
        "same_app_conflict": same_app_conflict,
        "counts": {
            "cross_game_keys": len(cross_game_ok),
            "same_app_conflicts": len(same_app_conflict),
        },
    }


def scan_internal_id_integrity(con: sqlite3.Connection) -> dict[str, Any]:
    rows = con.execute(
        """
        SELECT mod_id, internal_id, last_known_path, platform, app_id, workspace_id
        FROM mods
        """
    ).fetchall()

    db_missing_internal: list[dict[str, Any]] = []
    db_present_info_missing: list[dict[str, Any]] = []
    info_present_db_missing: list[dict[str, Any]] = []
    duplicate_internal: list[dict[str, Any]] = []

    by_internal: dict[str, list[str]] = defaultdict(list)
    db_internal_set: set[str] = set()

    for row in rows:
        mid = str(row["mod_id"])
        iid = _text(row["internal_id"])
        lkp = _text(row["last_known_path"])
        info_path = _info_path_for(lkp)
        if not iid:
            db_missing_internal.append(
                {
                    "mod_id": mid,
                    "last_known_path": lkp,
                    "info_path": info_path,
                    "issue": "DB_INTERNAL_ID_EMPTY",
                }
            )
            continue
        db_internal_set.add(iid)
        by_internal[iid].append(mid)

        info_iid = _read_info_internal_id(info_path) if info_path else ""
        folder_exists = bool(lkp and Path(lkp).is_dir())
        if folder_exists and (not Path(info_path).is_file() or not info_iid):
            db_present_info_missing.append(
                {
                    "mod_id": mid,
                    "internal_id": iid,
                    "last_known_path": lkp,
                    "info_path": info_path,
                    "issue": "DB_HAS_ENTITY_INFO_MISSING_OR_EMPTY_INTERNAL",
                }
            )
        elif Path(info_path).is_file() and info_iid and info_iid != iid:
            db_present_info_missing.append(
                {
                    "mod_id": mid,
                    "internal_id": iid,
                    "info_internal_id": info_iid,
                    "last_known_path": lkp,
                    "info_path": info_path,
                    "issue": "INFO_INTERNAL_MISMATCH_DB",
                }
            )

    for iid, mods in by_internal.items():
        if len(mods) > 1:
            duplicate_internal.append(
                {
                    "internal_id": iid,
                    "mod_ids": mods,
                    "issue": "DUPLICATE_INTERNAL_ID",
                }
            )

    # Lightweight orphan .info check: only folders that are last_known_path of some
    # row are already covered above. Also flag .info whose internal_id is set but
    # equals neither DB.internal_id nor any mod_id (read from that path only —
    # no sibling tree walk; keeps audit read-only and fast).
    mod_id_set = {str(r["mod_id"]) for r in rows}
    for row in rows:
        lkp = _text(row["last_known_path"])
        if not lkp:
            continue
        info_path = _info_path_for(lkp)
        info_iid = _read_info_internal_id(info_path)
        if not info_iid:
            continue
        if info_iid in db_internal_set or info_iid in mod_id_set:
            continue
        info_present_db_missing.append(
            {
                "info_path": info_path,
                "folder": lkp,
                "info_internal_id": info_iid,
                "issue": "INFO_INTERNAL_NOT_IN_DB",
            }
        )

    return {
        "db_internal_id_empty": db_missing_internal,
        "db_present_info_missing": db_present_info_missing,
        "info_present_db_missing": info_present_db_missing,
        "duplicate_internal_id": duplicate_internal,
        "counts": {
            "db_internal_id_empty": len(db_missing_internal),
            "db_present_info_missing": len(db_present_info_missing),
            "info_present_db_missing": len(info_present_db_missing),
            "duplicate_internal_id": len(duplicate_internal),
        },
    }


def _is_legal_registration_module(module: str) -> bool:
    mod = module.replace("\\", "/")
    for prefix in LEGAL_REGISTRATION_MODULES:
        if mod == prefix or mod.startswith(prefix):
            return True
    return False


def scan_boundary_usage() -> dict[str, Any]:
    """Classify every external_id / workspace_id / app_id identity call site."""
    patterns = [
        ("find_mod_by_external", r"find_mod_by_external\s*\("),
        ("find_mod_for_registration", r"find_mod_for_registration\s*\("),
        ("find_mod_by_workspace_id", r"find_mod_by_workspace_id\s*\("),
        ("find_mod_id_by_workspace_id", r"find_mod_id_by_workspace_id\s*\("),
        ("find_mod_by_internal_id", r"find_mod_by_internal_id\s*\("),
        ("find_by_published_id", r"find_by_published_id\s*\("),
        ("resolve_existing_mod_id", r"resolve_existing_mod_id\s*\("),
        ("WHERE.*external_id", r"WHERE[^\n]*external_id"),
        ("WHERE.*workspace_id", r"WHERE[^\n]*workspace_id"),
    ]
    hits: list[dict[str, Any]] = []
    stop_conditions: list[dict[str, Any]] = []

    for path in _iter_py_files():
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        body = _strip_comments(source)
        module = _module_of(path)
        if any(module.replace("\\", "/").endswith(s) or module.replace("\\", "/") == s
               for s in SKIP_BOUNDARY_MODULES):
            continue
        lines = body.splitlines()
        for api, pattern in patterns:
            for match in re.finditer(pattern, body, re.I):
                line_no = body[: match.start()].count("\n") + 1
                snippet = lines[line_no - 1].strip() if 0 < line_no <= len(lines) else ""
                classification = "UNKNOWN"
                legal = False
                if api == "find_mod_for_registration":
                    if _is_legal_registration_module(module):
                        classification = "LEGAL_REGISTRATION"
                        legal = True
                    else:
                        classification = "ILLEGAL_ENTITY_LOOKUP"
                elif api in ("find_mod_by_workspace_id", "find_mod_id_by_workspace_id"):
                    # Permanent no-op is OK at definition; call sites elsewhere flagged
                    if module.endswith("db_manager.py"):
                        classification = "DISABLED_API_DEFINITION"
                        legal = True
                    else:
                        classification = "ILLEGAL_ENTITY_LOOKUP"
                elif api == "find_mod_by_external":
                    if module.endswith("db_manager.py"):
                        classification = "DEPRECATED_ALIAS_DEFINITION"
                        legal = True
                    elif _is_legal_registration_module(module):
                        classification = "LEGACY_ALIAS_AS_REGISTRATION"
                        legal = True
                    else:
                        classification = "ILLEGAL_ENTITY_LOOKUP"
                elif api == "find_mod_by_internal_id":
                    classification = "LEGAL_ENTITY_LOOKUP"
                    legal = True
                elif api == "resolve_existing_mod_id":
                    classification = "LEGAL_ENTITY_LOOKUP_INTERNAL_ONLY"
                    legal = True
                elif api == "find_by_published_id":
                    # Path helper — not Mod entity identity; Deploy/remove path index
                    classification = "PATH_HELPER_NOT_ENTITY_ID"
                    legal = True
                elif "external_id" in api.lower():
                    classification = "SQL_EXTERNAL_ID_USAGE"
                    legal = module.endswith("db_manager.py")
                elif "workspace_id" in api.lower():
                    if "find_mod_for_registration" in snippet or module.endswith(
                        "db_manager.py"
                    ):
                        classification = "SQL_WORKSPACE_REGISTRATION_OR_STORE"
                        legal = True
                    else:
                        classification = "SQL_WORKSPACE_REVIEW"
                        legal = False

                item = {
                    "api": api,
                    "file": module,
                    "line": line_no,
                    "snippet": snippet[:200],
                    "classification": classification,
                    "legal": legal,
                }
                hits.append(item)
                if not legal and classification == "ILLEGAL_ENTITY_LOOKUP":
                    stop_conditions.append(item)

        # Flag treating app_id as standalone mod identity
        for match in re.finditer(
            r"find_mod_by_app_id\s*\(|get_mod_by_app_id\s*\(", body
        ):
            line_no = body[: match.start()].count("\n") + 1
            stop_conditions.append(
                {
                    "api": "app_id_as_mod_identity",
                    "file": module,
                    "line": line_no,
                    "snippet": lines[line_no - 1].strip()[:200]
                    if 0 < line_no <= len(lines)
                    else "",
                    "classification": "THIRD_IDENTITY_STOP",
                    "legal": False,
                }
            )

    legal_hits = [h for h in hits if h.get("legal")]
    illegal_hits = [h for h in hits if not h.get("legal")]
    return {
        "generated_at": _now(),
        "phase": "identity_boundary_usage",
        "production_mutation": "NONE",
        "model": {
            "entity": "internal_id",
            "registration": "(platform, app_id, workspace_id)",
            "legacy_audit_only": ["external_id"],
            "not_mod_identity": ["app_id", "published_file_id", "workshop_id"],
        },
        "hits": hits,
        "legal_registration_or_entity": legal_hits,
        "illegal_or_review": illegal_hits,
        "stop_conditions": stop_conditions,
        "counts": {
            "total_hits": len(hits),
            "legal": len(legal_hits),
            "illegal_or_review": len(illegal_hits),
            "stop_conditions": len(stop_conditions),
        },
        "verdict": (
            "STOP_THIRD_IDENTITY"
            if any(s.get("classification") == "THIRD_IDENTITY_STOP" for s in stop_conditions)
            else ("NEEDS_REVIEW" if stop_conditions else "OK")
        ),
    }


def flatten_csv_rows(report: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    mismatch = report.get("workspace_external_mismatch") or {}
    for key in (
        "A_empty_workspace_has_external",
        "B_workspace_ne_external",
        "C_empty_external",
    ):
        for item in mismatch.get(key) or []:
            rows.append(
                {
                    "section": "workspace_external",
                    "class": str(item.get("class") or key),
                    "internal_id": str(item.get("internal_id") or ""),
                    "platform": str(item.get("platform") or ""),
                    "app_id": str(item.get("app_id") or ""),
                    "workspace_id": str(item.get("workspace_id") or ""),
                    "external_id": str(item.get("external_id") or ""),
                    "info_path": str(item.get("info_path") or ""),
                    "issue": "",
                }
            )
    cross = report.get("cross_game_workspace_report") or {}
    for item in cross.get("same_app_conflict") or []:
        rows.append(
            {
                "section": "cross_game",
                "class": "SAME_APP_CONFLICT",
                "internal_id": ",".join(
                    str(e.get("internal_id") or "") for e in (item.get("entities") or [])
                ),
                "platform": str(item.get("platform") or ""),
                "app_id": str(item.get("app_id") or ""),
                "workspace_id": str(item.get("workspace_id") or ""),
                "external_id": "",
                "info_path": "",
                "issue": "SAME_APP_CONFLICT",
            }
        )
    for item in cross.get("cross_game_allowed") or []:
        rows.append(
            {
                "section": "cross_game",
                "class": "CROSS_GAME_ALLOWED",
                "internal_id": ",".join(
                    str(e.get("internal_id") or "") for e in (item.get("entities") or [])
                ),
                "platform": str(item.get("platform") or ""),
                "app_id": ",".join(str(a) for a in (item.get("app_ids") or [])),
                "workspace_id": str(item.get("workspace_id") or ""),
                "external_id": "",
                "info_path": "",
                "issue": "CROSS_GAME_ALLOWED",
            }
        )
    integrity = report.get("internal_id_integrity") or {}
    for bucket in (
        "db_internal_id_empty",
        "db_present_info_missing",
        "info_present_db_missing",
        "duplicate_internal_id",
    ):
        for item in integrity.get(bucket) or []:
            rows.append(
                {
                    "section": "internal_id",
                    "class": bucket,
                    "internal_id": str(
                        item.get("internal_id") or item.get("info_internal_id") or ""
                    ),
                    "platform": "",
                    "app_id": "",
                    "workspace_id": "",
                    "external_id": "",
                    "info_path": str(item.get("info_path") or ""),
                    "issue": str(item.get("issue") or bucket),
                }
            )
    return rows


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "section",
        "class",
        "internal_id",
        "platform",
        "app_id",
        "workspace_id",
        "external_id",
        "info_path",
        "issue",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def run_audit(
    *,
    db_path: Path,
    out_json: Path,
    out_csv: Path,
    boundary_json: Path,
) -> dict[str, Any]:
    db_bytes_before = db_path.read_bytes() if db_path.is_file() else b""
    boundary = scan_boundary_usage()

    if not db_path.is_file():
        report = {
            "generated_at": _now(),
            "phase": "identity_data_hygiene_1",
            "mode": "read_only",
            "production_mutation": "NONE",
            "db_path": str(db_path),
            "error": "db_missing",
            "boundary_usage_summary": boundary.get("counts"),
        }
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        boundary_json.write_text(
            json.dumps(boundary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        write_csv([], out_csv)
        return report

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        mismatch = scan_workspace_external_mismatch(con)
        cross = scan_cross_game_workspace(con)
        integrity = scan_internal_id_integrity(con)
    finally:
        con.close()

    report: dict[str, Any] = {
        "generated_at": _now(),
        "phase": "identity_data_hygiene_1",
        "mode": "read_only",
        "production_mutation": "NONE",
        "db_path": str(db_path),
        "db_sha256_before": hashlib.sha256(db_bytes_before).hexdigest(),
        "workspace_external_mismatch": mismatch,
        "cross_game_workspace_report": cross,
        "internal_id_integrity": integrity,
        "boundary_usage_file": str(boundary_json),
        "boundary_usage_summary": boundary.get("counts"),
        "boundary_verdict": boundary.get("verdict"),
        "forbidden_actions": [
            "create_mod",
            "delete_mod",
            "merge_mod",
            "modify_internal_id",
            "modify_app_id",
            "auto_apply_hygiene",
        ],
        "next": "python tools/identity_data_hygiene_plan.py",
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(flatten_csv_rows(report), out_csv)
    boundary_json.write_text(
        json.dumps(boundary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    db_bytes_after = db_path.read_bytes() if db_path.is_file() else b""
    report["db_sha256_after"] = (
        hashlib.sha256(db_bytes_after).hexdigest() if db_bytes_after else ""
    )
    report["db_bytes_unchanged"] = db_bytes_before == db_bytes_after
    # Concurrent writers (live app) may alter DB bytes while we use mode=ro.
    # This tool never opens a write connection; do not treat external writes as
    # hygiene mutation.
    report["audit_write_connection"] = False
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Identity data hygiene read-only audit")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "mod_manager.db")
    parser.add_argument("--out-json", type=Path, default=DEFAULT_OUT_JSON)
    parser.add_argument("--out-csv", type=Path, default=DEFAULT_OUT_CSV)
    parser.add_argument("--boundary-json", type=Path, default=DEFAULT_BOUNDARY_JSON)
    args = parser.parse_args(argv)
    report = run_audit(
        db_path=args.db,
        out_json=args.out_json,
        out_csv=args.out_csv,
        boundary_json=args.boundary_json,
    )
    mismatch = report.get("workspace_external_mismatch", {}).get("counts", {})
    cross = report.get("cross_game_workspace_report", {}).get("counts", {})
    integrity = report.get("internal_id_integrity", {}).get("counts", {})
    print(
        json.dumps(
            {
                "out_json": str(args.out_json),
                "out_csv": str(args.out_csv),
                "boundary_json": str(args.boundary_json),
                "workspace_external": mismatch,
                "cross_game": cross,
                "internal_id": integrity,
                "boundary_verdict": report.get("boundary_verdict"),
                "production_mutation": "NONE",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
