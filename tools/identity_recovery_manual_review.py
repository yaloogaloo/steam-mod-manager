"""Identity Recovery Phase 2-D — generate human review CSV.

Read-only. Never mutates DB / .info / backup / lifecycle code.

Merges Phase 1 report, 2-A plan, 2-B preview, and 2-C apply log into a
single auditable spreadsheet for human confirmation. Does not repair.

Usage::

    python tools/identity_recovery_manual_review.py
    python tools/identity_recovery_manual_review.py --out path/to.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_REPORT = ROOT / "tools" / "_audit_out" / "identity_recovery_report.json"
DEFAULT_PLAN = ROOT / "tools" / "_audit_out" / "identity_recovery_plan.json"
DEFAULT_PREVIEW = ROOT / "tools" / "_audit_out" / "identity_repair_preview.json"
DEFAULT_APPLY_LOG = ROOT / "tools" / "_audit_out" / "identity_recovery_apply_log.json"
DEFAULT_OUT = ROOT / "tools" / "_audit_out" / "identity_recovery_manual_review.csv"

CSV_COLUMNS = [
    "finding_id",
    "category",
    "internal_id",
    "platform",
    "app_id",
    "external_id",
    "workspace_id",
    "db_record",
    "info_record",
    "backup_record",
    "problem_description",
    "risk_level",
    "recommended_action",
    "manual_decision",
]

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_cell(value: Any) -> str:
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def assert_tool_is_read_only(source: str) -> None:
    """Static self-check used by tests (ignores comments / docstrings)."""
    import re

    stripped = re.sub(r'"""[\s\S]*?"""', "", source)
    stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
    stripped = re.sub(r"#.*?$", "", stripped, flags=re.M)
    stripped = re.sub(
        r"def assert_tool_is_read_only\([\s\S]*?(?=\ndef |\Z)",
        "",
        stripped,
    )
    forbidden = (
        "create_mod_" + "identity(",
        "ensure_mod_" + "identity(",
        "find_mod_by_workspace_" + "id(",
        "find_mod_id_by_workspace_" + "id(",
        "Identity" + "Service",
        "Library" + "Reconcile",
        "Mod" + "Deployer",
        "upsert_" + "mods(",
        "INSERT INTO " + "mods",
        "DELETE FROM " + "mods",
    )
    for token in forbidden:
        if token in stripped:
            raise AssertionError(f"Phase 2-D tool must not contain {token!r}")


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"missing input: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected object JSON: {path}")
    return data


def _file_digest(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _index_plan_by_finding_id(plan: dict[str, Any], preview_items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map finding_id -> plan item via preview plan_index alignment."""
    plan_items = list(plan.get("items") or [])
    out: dict[str, dict[str, Any]] = {}
    for prev in preview_items:
        fid = _text(prev.get("finding_id"))
        idx = prev.get("plan_index")
        if fid and isinstance(idx, int) and 0 <= idx < len(plan_items):
            out[fid] = plan_items[idx]
    return out


def _index_report_by_finding_id(
    report: dict[str, Any], preview_items: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    findings = list(report.get("findings") or [])
    out: dict[str, dict[str, Any]] = {}
    for prev in preview_items:
        fid = _text(prev.get("finding_id"))
        if ":" not in fid:
            continue
        _, idx_s = fid.rsplit(":", 1)
        try:
            idx = int(idx_s)
        except ValueError:
            continue
        if 0 <= idx < len(findings):
            out[fid] = findings[idx]
    return out


def _apply_skip_by_finding_id(apply_log: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in apply_log.get("skipped") or []:
        if not isinstance(row, dict):
            continue
        fid = _text(row.get("finding_id"))
        if fid:
            out[fid] = _text(row.get("reason"))
    return out


def _pick_identity(
    db: dict[str, Any] | None,
    info: dict[str, Any] | None,
    backup: dict[str, Any] | None,
    preview: dict[str, Any],
    report_finding: dict[str, Any] | None,
) -> dict[str, str]:
    sources = [x for x in (db, info, backup) if isinstance(x, dict)]
    report_finding = report_finding or {}

    def first(*keys: str) -> str:
        for src in sources:
            for key in keys:
                val = _text(src.get(key))
                if val:
                    return val
        for key in keys:
            val = _text(preview.get(key)) or _text(report_finding.get(key))
            if val:
                return val
        return ""

    app_raw = first("app_id")
    if not app_raw:
        for src in sources:
            if src.get("app_id") is not None:
                app_raw = str(int(src.get("app_id") or 0))
                break
        if not app_raw and report_finding.get("app_id") is not None:
            app_raw = str(int(report_finding.get("app_id") or 0))

    return {
        "internal_id": first("internal_id"),
        "platform": first("platform", "source_type"),
        "app_id": app_raw,
        "external_id": first("external_id", "published_file_id"),
        "workspace_id": first("workspace_id"),
    }


def _problem_description(
    preview: dict[str, Any],
    plan_item: dict[str, Any] | None,
    report_finding: dict[str, Any] | None,
    apply_skip_reason: str,
    apply_status: str,
) -> str:
    parts: list[str] = []
    evidence = preview.get("evidence") if isinstance(preview.get("evidence"), dict) else {}
    for piece in (
        _text(evidence.get("conflict_reason")),
        _text((plan_item or {}).get("current_state")),
        _text((plan_item or {}).get("needs_human_confirm_reason")),
        _text(preview.get("needs_human_confirm_reason")),
        _text((report_finding or {}).get("conflict_reason")),
    ):
        if piece and piece not in parts:
            parts.append(piece)

    blocked = evidence.get("restore_blocked_reasons")
    if isinstance(blocked, list) and blocked:
        parts.append("restore_blocked=" + "; ".join(_text(x) for x in blocked if _text(x)))

    if apply_skip_reason:
        parts.append(f"phase_2c_skip={apply_skip_reason}")
    elif _text(preview.get("proposed_action")) == "RESTORE_INFO_FROM_BACKUP" and apply_status:
        parts.append(f"phase_2c_status={apply_status}")

    return " | ".join(parts)


def build_manual_review_rows(
    *,
    report: dict[str, Any],
    plan: dict[str, Any],
    preview: dict[str, Any],
    apply_log: dict[str, Any],
) -> list[dict[str, str]]:
    preview_items = [x for x in (preview.get("items") or []) if isinstance(x, dict)]
    plan_by_fid = _index_plan_by_finding_id(plan, preview_items)
    report_by_fid = _index_report_by_finding_id(report, preview_items)
    skip_by_fid = _apply_skip_by_finding_id(apply_log)
    apply_status = _text(apply_log.get("status"))

    rows: list[dict[str, str]] = []
    for prev in preview_items:
        fid = _text(prev.get("finding_id"))
        before = prev.get("before") if isinstance(prev.get("before"), dict) else {}
        db = before.get("db_record") if isinstance(before.get("db_record"), dict) else None
        info = before.get("info_record") if isinstance(before.get("info_record"), dict) else None
        backup = before.get("backup_record") if isinstance(before.get("backup_record"), dict) else None
        plan_item = plan_by_fid.get(fid)
        report_finding = report_by_fid.get(fid)
        ident = _pick_identity(db, info, backup, prev, report_finding)

        rows.append(
            {
                "finding_id": fid,
                "category": _text(prev.get("plan_category") or (plan_item or {}).get("category")),
                "internal_id": ident["internal_id"],
                "platform": ident["platform"],
                "app_id": ident["app_id"],
                "external_id": ident["external_id"],
                "workspace_id": ident["workspace_id"],
                "db_record": _json_cell(db),
                "info_record": _json_cell(info),
                "backup_record": _json_cell(backup),
                "problem_description": _problem_description(
                    prev,
                    plan_item,
                    report_finding,
                    skip_by_fid.get(fid, ""),
                    apply_status,
                ),
                "risk_level": _text(prev.get("risk") or (plan_item or {}).get("risk")),
                "recommended_action": _text(
                    prev.get("proposed_action")
                    or (plan_item or {}).get("recommended_action")
                    or (report_finding or {}).get("recommended_action")
                ),
                "manual_decision": "",
            }
        )
    return rows


def write_manual_review_csv(rows: list[dict[str, str]], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})
    return out_path


def generate_manual_review(
    *,
    report_path: Path,
    plan_path: Path,
    preview_path: Path,
    apply_log_path: Path,
    out_path: Path,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Build CSV from prior phase artifacts. Only writes ``out_path``."""
    assert_tool_is_read_only(Path(__file__).read_text(encoding="utf-8"))

    report = _load_json(report_path)
    plan = _load_json(plan_path)
    preview = _load_json(preview_path)
    apply_log = _load_json(apply_log_path) if apply_log_path.is_file() else {}

    resolved_db = Path(_text(report.get("db_path")) or (db_path or ROOT / "data" / "mod_manager.db"))
    db_digest_before = _file_digest(resolved_db)
    db_bytes_before = resolved_db.read_bytes() if resolved_db.is_file() else b""

    rows = build_manual_review_rows(
        report=report, plan=plan, preview=preview, apply_log=apply_log
    )
    write_manual_review_csv(rows, out_path)

    db_digest_after = _file_digest(resolved_db)
    db_bytes_after = resolved_db.read_bytes() if resolved_db.is_file() else b""
    if db_bytes_before != db_bytes_after:
        raise RuntimeError("Phase 2-D violated read-only contract: DB bytes changed")

    return {
        "generated_at": _now(),
        "phase": "2-D",
        "mode": "manual_review_csv",
        "production_mutation": "NONE",
        "out_path": str(out_path),
        "rows_count": len(rows),
        "source_report": str(report_path),
        "source_plan": str(plan_path),
        "source_preview": str(preview_path),
        "source_apply_log": str(apply_log_path) if apply_log_path.is_file() else "",
        "db_path": str(resolved_db),
        "db_bytes_unchanged": True,
        "db_sha256": db_digest_after or db_digest_before,
        "category_counts": _count_field(rows, "category"),
        "recommended_action_counts": _count_field(rows, "recommended_action"),
        "risk_counts": _count_field(rows, "risk_level"),
        "guards": {
            "no_create": True,
            "no_delete": True,
            "no_merge": True,
            "no_internal_id_rewrite": True,
            "no_identity_service": True,
            "no_workspace_resolver": True,
            "manual_decision_empty": all(r.get("manual_decision", "") == "" for r in rows),
        },
    }


def _count_field(rows: list[dict[str, str]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = _text(row.get(field)) or "(empty)"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Identity Recovery Phase 2-D manual review CSV")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--preview", type=Path, default=DEFAULT_PREVIEW)
    parser.add_argument("--apply-log", type=Path, default=DEFAULT_APPLY_LOG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    summary = generate_manual_review(
        report_path=args.report,
        plan_path=args.plan,
        preview_path=args.preview,
        apply_log_path=args.apply_log,
        out_path=args.out,
        db_path=args.db,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"csv={args.out}")
    print(f"rows={summary['rows_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
