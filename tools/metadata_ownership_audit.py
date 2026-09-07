#!/usr/bin/env python3
"""Phase 0 — Metadata Ownership Isolation audit (read-only)."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.metadata_ownership_common import (  # noqa: E402
    BACKUP_DIR_NAME,
    CODE_BACKUP_FOREIGN_OWNER,
    CODE_BACKUP_LEGACY_KEY,
    CODE_INFO_METADATA_POLLUTED,
    CODE_METADATA_FOREIGN_OWNER,
    app_id_from_url,
    desc_fingerprint,
    description_foreign_app_markers,
    dump_json,
    entity_summary,
    load_db_rows,
    metadata_app_evidence,
    now_utc,
    read_info,
    text,
)


def _finding(
    code: str,
    entity: dict[str, Any],
    *,
    metadata_source: str,
    metadata_url: str = "",
    metadata_app_id: int = 0,
    conflict_reason: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = {
        "code": code,
        "internal_id": entity["internal_id"],
        "mod_id": entity["mod_id"],
        "app_id": entity["app_id"],
        "workspace_id": entity["workspace_id"],
        "title": entity["title"],
        "metadata_source": metadata_source,
        "metadata_url": metadata_url,
        "metadata_app_id": int(metadata_app_id or 0),
        "conflict_reason": conflict_reason,
    }
    if extra:
        out.update(extra)
    return out


def audit_db_metadata(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    by_ws: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        ent = entity_summary(row)
        by_ws[ent["workspace_id"] or f"__mid:{ent['mod_id']}"].append((row, ent))

        url_app = app_id_from_url(ent["source_url"])
        if ent["app_id"] > 0 and url_app > 0 and url_app != ent["app_id"]:
            findings.append(
                _finding(
                    CODE_METADATA_FOREIGN_OWNER,
                    ent,
                    metadata_source="db.source_url",
                    metadata_url=ent["source_url"],
                    metadata_app_id=url_app,
                    conflict_reason="entity.app_id != metadata.app_id(from source_url)",
                )
            )

    # Cross-game same workspace_id description pollution
    for ws, items in by_ws.items():
        if not ws or ws.startswith("__mid:") or len(items) < 2:
            continue
        apps = {ent["app_id"] for _, ent in items if ent["app_id"] > 0}
        if len(apps) < 2:
            continue
        fps: dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]] = defaultdict(list)
        for row, ent in items:
            for field in ("description", "custom_description"):
                fp = desc_fingerprint(text(row.get(field)))
                if fp:
                    fps[fp].append((row, ent, field))
        for fp, owners in fps.items():
            owner_apps = {ent["app_id"] for _, ent, _ in owners}
            if len(owner_apps) < 2:
                # Same fingerprint appears once but equals another entity's desc under same ws
                continue
            for _row, ent, field in owners:
                findings.append(
                    _finding(
                        CODE_METADATA_FOREIGN_OWNER,
                        ent,
                        metadata_source=f"db.{field}",
                        metadata_url=ent["source_url"],
                        metadata_app_id=0,
                        conflict_reason=(
                            "same workspace_id cross-app identical description fingerprint"
                        ),
                        extra={"workspace_id_group": ws, "field": field},
                    )
                )

        # One entity holds another entity's description verbatim
        texts: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
        for row, ent in items:
            for field in ("description", "custom_description"):
                body = text(row.get(field))
                if len(body) >= 40:
                    texts.append((row, ent, field, body))
        for i, (_r1, e1, f1, t1) in enumerate(texts):
            for _r2, e2, f2, t2 in texts[i + 1 :]:
                if e1["app_id"] == e2["app_id"]:
                    continue
                if t1 == t2 or (len(t1) > 80 and t1 in t2) or (len(t2) > 80 and t2 in t1):
                    for ent, field, body, peer in (
                        (e1, f1, t1, e2),
                        (e2, f2, t2, e1),
                    ):
                        foreign = description_foreign_app_markers(
                            body, entity_app_id=ent["app_id"]
                        )
                        findings.append(
                            _finding(
                                CODE_METADATA_FOREIGN_OWNER,
                                ent,
                                metadata_source=f"db.{field}",
                                metadata_url=ent["source_url"],
                                metadata_app_id=int(foreign or 0),
                                conflict_reason=(
                                    f"description matches peer internal_id={peer['internal_id']} "
                                    f"app_id={peer['app_id']}"
                                    + (
                                        "; foreign description markers"
                                        if foreign
                                        else ""
                                    )
                                ),
                                extra={
                                    "peer_internal_id": peer["internal_id"],
                                    "peer_mod_id": peer["mod_id"],
                                    "peer_app_id": peer["app_id"],
                                    "peer_field": field,
                                    "foreign_marker_app_id": foreign,
                                },
                            )
                        )
    return findings


def audit_info(rows: list[dict[str, Any]], mod_root: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    by_mid = {text(r.get("mod_id")): entity_summary(r) for r in rows}
    by_uuid = {
        text(r.get("internal_id")): entity_summary(r)
        for r in rows
        if text(r.get("internal_id"))
    }

    if not mod_root.is_dir():
        return findings

    for game_dir in sorted(p for p in mod_root.iterdir() if p.is_dir()):
        for folder in sorted(p for p in game_dir.iterdir() if p.is_dir()):
            payload, info_path = read_info(folder)
            if not payload or payload.get("_read_error"):
                continue
            info_mid = text(payload.get("internal_id") or payload.get("mod_id"))
            ent = by_mid.get(info_mid) or by_uuid.get(info_mid)
            if not ent:
                continue
            ev = metadata_app_evidence(payload)
            info_app = int(payload.get("app_id") or 0) or ev["metadata_app_id"]
            if ent["app_id"] > 0 and info_app > 0 and info_app != ent["app_id"]:
                findings.append(
                    _finding(
                        CODE_INFO_METADATA_POLLUTED,
                        ent,
                        metadata_source=info_path,
                        metadata_url=ev["metadata_url"],
                        metadata_app_id=info_app,
                        conflict_reason="entity.app_id != .info metadata app_id/url",
                        extra={"info_path": info_path, "info_internal_id": info_mid},
                    )
                )
            # URL evidence vs entity
            if (
                ent["app_id"] > 0
                and ev["url_app_id"] > 0
                and ev["url_app_id"] != ent["app_id"]
            ):
                findings.append(
                    _finding(
                        CODE_INFO_METADATA_POLLUTED,
                        ent,
                        metadata_source=info_path,
                        metadata_url=ev["metadata_url"],
                        metadata_app_id=ev["url_app_id"],
                        conflict_reason="entity.app_id != .info.source_url app",
                        extra={"info_path": info_path},
                    )
                )
            # Identity proof mismatch is out of scope for metadata ownership,
            # but polluted content with correct internal_id still matters:
            # description borrowed from peer with other app under same workspace.
    return findings


def audit_backup(rows: list[dict[str, Any]], data_root: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    backup_root = data_root / BACKUP_DIR_NAME
    if not backup_root.is_dir():
        return findings

    by_mid = {text(r.get("mod_id")): entity_summary(r) for r in rows}
    known_mids = set(by_mid)
    known_ws = {entity_summary(r)["workspace_id"] for r in rows}
    known_ws.discard("")

    for child in sorted(p for p in backup_root.iterdir() if p.is_dir()):
        key = child.name
        # Legacy: backup keyed by workspace_id / external digits that are not mod_id
        if key.isdigit() and key not in known_mids and key in known_ws:
            # Ambiguous: multiple entities may share this workspace_id
            peers = [entity_summary(r) for r in rows if text(r.get("workspace_id")) == key]
            findings.append(
                {
                    "code": CODE_BACKUP_LEGACY_KEY,
                    "internal_id": "",
                    "mod_id": "",
                    "app_id": 0,
                    "workspace_id": key,
                    "title": "",
                    "metadata_source": str(child),
                    "metadata_url": "",
                    "metadata_app_id": 0,
                    "conflict_reason": (
                        "backup folder keyed by workspace_id/digits, not internal_id/mod_id"
                    ),
                    "peer_mod_ids": [p["mod_id"] for p in peers],
                    "peer_app_ids": [p["app_id"] for p in peers],
                }
            )
            continue

        ent = by_mid.get(key)
        if not ent:
            continue

        meta_path = child / "metadata.json"
        if not meta_path.is_file():
            continue
        try:
            import json

            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        ev = metadata_app_evidence(payload)
        meta_app = ev["metadata_app_id"]
        if ent["app_id"] > 0 and meta_app > 0 and meta_app != ent["app_id"]:
            findings.append(
                _finding(
                    CODE_BACKUP_FOREIGN_OWNER,
                    ent,
                    metadata_source=str(meta_path),
                    metadata_url=ev["metadata_url"],
                    metadata_app_id=meta_app,
                    conflict_reason="entity.app_id != backup metadata app_id/url",
                )
            )
        # Backup internal_id must match entity when present
        b_mid = text(payload.get("internal_id") or payload.get("mod_id"))
        if b_mid and b_mid not in (ent["mod_id"], ent["internal_id"]):
            findings.append(
                _finding(
                    CODE_BACKUP_FOREIGN_OWNER,
                    ent,
                    metadata_source=str(meta_path),
                    metadata_url=ev["metadata_url"],
                    metadata_app_id=meta_app,
                    conflict_reason="backup metadata internal_id != entity",
                    extra={"backup_internal_id": b_mid},
                )
            )
    return findings


def run_audit(
    *,
    db_path: Path,
    mod_root: Path,
    data_root: Path,
) -> dict[str, Any]:
    rows = load_db_rows(db_path)
    findings = []
    findings.extend(audit_db_metadata(rows))
    findings.extend(audit_info(rows, mod_root))
    findings.extend(audit_backup(rows, data_root))

    # Deduplicate by code+internal_id+conflict_reason+metadata_source
    seen: set[tuple[Any, ...]] = set()
    unique: list[dict[str, Any]] = []
    for f in findings:
        key = (
            f.get("code"),
            f.get("internal_id"),
            f.get("conflict_reason"),
            f.get("metadata_source"),
            f.get("field"),
            f.get("peer_internal_id"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(f)

    by_code: dict[str, int] = defaultdict(int)
    for f in unique:
        by_code[str(f.get("code"))] += 1

    return {
        "generated_at": now_utc(),
        "db_path": str(db_path),
        "mod_root": str(mod_root),
        "data_root": str(data_root),
        "entity_count": len(rows),
        "finding_count": len(unique),
        "by_code": dict(by_code),
        "findings": unique,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Metadata ownership audit (read-only)")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--mod-root", type=Path, default=None)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    db, mod_root, data_root, out_dir = (
        args.db or (ROOT / "data" / "mod_manager.db"),
        args.mod_root or (ROOT / "mod"),
        args.data_root or (ROOT / "data"),
        (args.out.parent if args.out else (ROOT / "tools" / "_audit_out")),
    )
    out_path = args.out or (out_dir / "metadata_ownership_report.json")

    report = run_audit(db_path=db, mod_root=mod_root, data_root=data_root)
    dump_json(out_path, report)
    print(f"Wrote {out_path}")
    print(f"findings={report['finding_count']} by_code={report['by_code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
