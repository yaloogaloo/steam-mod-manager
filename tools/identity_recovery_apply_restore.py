"""Identity Recovery Phase 2-C — apply confirmed RESTORE_INFO_FROM_BACKUP only.

Executes filesystem ``.info`` restore for preview items whose
``proposed_action == RESTORE_INFO_FROM_BACKUP``.

Never:
- creates / deletes / merges Mods
- mutates DB identity or status fields
- calls create_mod_identity / workspace resolver / reconcile

On first failure: rollback that item and abort remaining work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ALLOWED_ACTION = "RESTORE_INFO_FROM_BACKUP"
INFO_DIR = ".info"
METADATA = "metadata.json"
BACKUP_METADATA = "metadata.json"

FROZEN_FIELDS = (
    "internal_id",
    "workspace_id",
    "deploy_status",
    "deploy_path",
    "content_status",
    "conflict_status",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def snapshot_mods(db_path: Path) -> dict[str, Any]:
    con = _connect_ro(db_path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(mods)")}
        wanted = [
            "mod_id",
            "internal_id",
            "workspace_id",
            "external_id",
            "platform",
            "app_id",
            "deploy_status",
            "deploy_path",
            "content_status",
            "conflict_status",
            "last_known_path",
            "folder_present",
            "title",
            "source_url",
        ]
        select = ", ".join(c for c in wanted if c in cols)
        rows = []
        for r in con.execute(f"SELECT {select} FROM mods ORDER BY mod_id"):
            rows.append({k: r[k] for k in r.keys()})
        count = con.execute("SELECT COUNT(*) FROM mods").fetchone()[0]
    finally:
        con.close()
    return {
        "generated_at": _now(),
        "db_path": str(db_path),
        "mods_count": int(count),
        "frozen_fields": list(FROZEN_FIELDS),
        "rows": rows,
    }


def _load_backup(backup_root: Path, mod_id: str) -> dict[str, Any] | None:
    meta = backup_root / str(mod_id) / BACKUP_METADATA
    if not meta.is_file():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _read_info(folder: Path) -> dict[str, Any] | None:
    meta = folder / INFO_DIR / METADATA
    if not meta.is_file():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return {"_read_error": True}
    return data if isinstance(data, dict) else {"_read_error": True}


def _identity_match(backup: dict[str, Any], db: dict[str, Any]) -> tuple[bool, list[str]]:
    """Strict Phase 2-C match: all four identity axes must agree."""
    fails: list[str] = []
    bak_iid = _text(backup.get("internal_id"))
    db_iid = _text(db.get("internal_id"))
    mid = _text(db.get("mod_id"))
    if not bak_iid:
        fails.append("backup.internal_id empty")
    elif db_iid and bak_iid != db_iid:
        fails.append(f"internal_id backup={bak_iid!r} db={db_iid!r}")
    elif not db_iid and bak_iid != mid:
        fails.append(f"internal_id backup={bak_iid!r} db_mod_id={mid!r}")

    bak_plat = _text(backup.get("platform") or backup.get("source_type")).lower()
    db_plat = _text(db.get("platform")).lower()
    if bak_plat != db_plat:
        fails.append(f"platform backup={bak_plat!r} db={db_plat!r}")

    try:
        bak_app = int(backup.get("app_id") or 0)
    except (TypeError, ValueError):
        bak_app = 0
    db_app = int(db.get("app_id") or 0)
    if bak_app != db_app:
        fails.append(f"app_id backup={bak_app} db={db_app}")

    bak_ext = _text(backup.get("external_id") or backup.get("published_file_id"))
    db_ext = _text(db.get("external_id"))
    if bak_ext != db_ext:
        fails.append(f"external_id backup={bak_ext!r} db={db_ext!r}")

    return (not fails), fails


def preflight_restore_items(
    items: list[dict[str, Any]],
    *,
    backup_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split preview restore items into executable vs skipped."""
    ready: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in items:
        finding_id = _text(item.get("finding_id"))
        action = _text(item.get("proposed_action"))
        if action != ALLOWED_ACTION:
            skipped.append(
                {
                    "finding_id": finding_id,
                    "reason": f"action not allowed: {action}",
                }
            )
            continue
        db = (item.get("before") or {}).get("db_record") or {}
        mid = _text(db.get("mod_id"))
        path = _text(db.get("last_known_path"))
        if not mid or not path or not Path(path).is_dir():
            skipped.append(
                {
                    "finding_id": finding_id,
                    "mod_id": mid,
                    "reason": "missing mod_id or last_known_path directory",
                }
            )
            continue
        backup = _load_backup(backup_root, mid)
        if backup is None:
            skipped.append(
                {
                    "finding_id": finding_id,
                    "mod_id": mid,
                    "reason": "backup metadata missing",
                }
            )
            continue
        ok, fails = _identity_match(backup, db)
        if not ok:
            skipped.append(
                {
                    "finding_id": finding_id,
                    "mod_id": mid,
                    "reason": "backup identity mismatch",
                    "fails": fails,
                }
            )
            continue
        ready.append(item)
    return ready, skipped


def _validate_info_against_db(info: dict[str, Any], db: dict[str, Any]) -> tuple[bool, list[str]]:
    from services.mod_identity import read_entity_key

    fails: list[str] = []
    # Compare filesystem binding (entity_key / legacy) to Entity.internal_id.
    info_iid = read_entity_key(info)
    db_iid = _text(db.get("internal_id")) or _text(db.get("mod_id"))
    if info_iid != db_iid:
        fails.append(f"info.entity_key={info_iid!r} != db.internal_id={db_iid!r}")

    info_plat = _text(info.get("platform") or info.get("source_type")).lower()
    db_plat = _text(db.get("platform")).lower()
    if info_plat != db_plat:
        fails.append(f"platform info={info_plat!r} db={db_plat!r}")

    try:
        info_app = int(info.get("app_id") or 0)
    except (TypeError, ValueError):
        info_app = 0
    db_app = int(db.get("app_id") or 0)
    if info_app != db_app:
        fails.append(f"app_id info={info_app} db={db_app}")

    info_ext = _text(info.get("external_id") or info.get("published_file_id"))
    db_ext = _text(db.get("external_id"))
    if info_ext != db_ext:
        fails.append(f"external_id info={info_ext!r} db={db_ext!r}")
    return (not fails), fails


def _run_identity_validator(db: dict[str, Any], info: dict[str, Any]) -> list[str]:
    """Lightweight validator — no IdentityService create path."""
    from services.mod_identity_validator import validate_db_row_identity

    findings = validate_db_row_identity(
        mod_id=_text(db.get("mod_id")),
        platform=_text(db.get("platform")),
        external_id=_text(db.get("external_id")),
        workspace_id=_text(db.get("workspace_id")),
        source_url=_text(db.get("source_url")),
    )
    codes = [str(getattr(f, "code", f)) for f in findings]
    ok, fails = _validate_info_against_db(info, db)
    if not ok:
        codes.extend(fails)
    return codes


def _build_restored_payload(backup: dict[str, Any], db: dict[str, Any]) -> dict[str, Any]:
    from services.mod_identity import set_entity_key

    payload = dict(backup)
    # DB is authority for identity stamps — do not invent new ids.
    # entity_key is filesystem binding (value == Entity.internal_id), not a third ID.
    db_iid = _text(db.get("internal_id")) or _text(db.get("mod_id"))
    payload = set_entity_key(payload, db_iid)
    payload["platform"] = _text(db.get("platform"))
    payload["source_type"] = _text(db.get("platform")) or _text(
        backup.get("source_type")
    )
    payload["app_id"] = int(db.get("app_id") or 0)
    payload["external_id"] = _text(db.get("external_id"))
    # workspace_id: preserve DB value only (never invent / migrate)
    if _text(db.get("workspace_id")):
        payload["workspace_id"] = _text(db.get("workspace_id"))
    return payload


def _write_info(folder: Path, payload: dict[str, Any]) -> Path:
    from services.mod_identity import normalize_info_entity_key_payload

    info_dir = folder / INFO_DIR
    info_dir.mkdir(parents=True, exist_ok=True)
    meta = info_dir / METADATA
    normalized, _ = normalize_info_entity_key_payload(dict(payload))
    meta.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return meta


def _rollback_info(folder: Path, rollback_meta: Path | None, *, had_info: bool) -> None:
    meta = folder / INFO_DIR / METADATA
    if not had_info:
        if meta.is_file():
            meta.unlink()
        info_dir = folder / INFO_DIR
        if info_dir.is_dir() and not any(info_dir.iterdir()):
            info_dir.rmdir()
        return
    if rollback_meta is not None and rollback_meta.is_file():
        info_dir = folder / INFO_DIR
        info_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rollback_meta, meta)


def assert_frozen_unchanged(
    before: dict[str, Any], after: dict[str, Any]
) -> list[str]:
    errs: list[str] = []
    if before.get("mods_count") != after.get("mods_count"):
        errs.append(
            f"mods_count {before.get('mods_count')} -> {after.get('mods_count')}"
        )
    bmap = {_text(r.get("mod_id")): r for r in before.get("rows") or []}
    amap = {_text(r.get("mod_id")): r for r in after.get("rows") or []}
    if set(bmap) != set(amap):
        errs.append("mod_id set changed")
        return errs
    for mid, brow in bmap.items():
        arow = amap[mid]
        for field in FROZEN_FIELDS:
            if field not in brow and field not in arow:
                continue
            if _text(brow.get(field)) != _text(arow.get(field)):
                errs.append(
                    f"mod_id={mid} {field}: {_text(brow.get(field))!r} -> "
                    f"{_text(arow.get(field))!r}"
                )
        # Also forbid external_id mutation
        if _text(brow.get("external_id")) != _text(arow.get("external_id")):
            errs.append(f"mod_id={mid} external_id mutated")
    return errs


def apply_restore_items(
    items: list[dict[str, Any]],
    *,
    backup_root: Path,
    rollback_root: Path,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    rollback_root.mkdir(parents=True, exist_ok=True)

    for item in items:
        finding_id = _text(item.get("finding_id"))
        action = _text(item.get("proposed_action"))
        if action != ALLOWED_ACTION:
            raise RuntimeError(f"refusing non-restore action {action!r} for {finding_id}")

        before = item.get("before") or {}
        db = before.get("db_record") or {}
        if not isinstance(db, dict) or not _text(db.get("mod_id")):
            raise RuntimeError(f"{finding_id}: missing db_record.mod_id")

        mid = _text(db.get("mod_id"))
        path = _text(db.get("last_known_path"))
        folder = Path(path)
        if not folder.is_dir():
            raise RuntimeError(f"{finding_id}: last_known_path missing: {path}")

        backup = _load_backup(backup_root, mid)
        if backup is None:
            raise RuntimeError(f"{finding_id}: backup metadata missing for mod_id={mid}")

        ok, fails = _identity_match(backup, db)
        if not ok:
            raise RuntimeError(f"{finding_id}: backup identity mismatch: {fails}")

        meta_path = folder / INFO_DIR / METADATA
        had_info = meta_path.is_file()
        rb_dir = rollback_root / finding_id.replace(":", "_")
        rb_dir.mkdir(parents=True, exist_ok=True)
        rb_meta: Path | None = None
        if had_info:
            rb_meta = rb_dir / METADATA
            shutil.copy2(meta_path, rb_meta)

        payload = _build_restored_payload(backup, db)
        try:
            written = _write_info(folder, payload)
            info_after = _read_info(folder)
            if not info_after or info_after.get("_read_error"):
                raise RuntimeError("re-read .info failed after write")
            match_ok, match_fails = _validate_info_against_db(info_after, db)
            if not match_ok:
                raise RuntimeError(f"post-restore identity mismatch: {match_fails}")
            validator_codes = _run_identity_validator(db, info_after)
            # Soft: pollution findings on dirty DB rows may pre-exist; only fail hard
            # mismatches already checked. Record validator output.
            from services.mod_identity import read_entity_key

            results.append(
                {
                    "finding_id": finding_id,
                    "mod_id": mid,
                    "path": str(folder),
                    "status": "restored",
                    "info_path": str(written),
                    "had_info_before": had_info,
                    "rollback_copy": str(rb_meta) if rb_meta else "",
                    "validator_codes": [str(c) for c in validator_codes],
                    "info_internal_id": read_entity_key(info_after),
                }
            )
        except Exception as exc:
            _rollback_info(folder, rb_meta, had_info=had_info)
            raise RuntimeError(f"{finding_id}: restore failed and rolled back: {exc}") from exc

    return {
        "restored_count": len(results),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preview",
        type=str,
        default="",
        help="Phase 2-B preview JSON",
    )
    parser.add_argument("--db", type=str, default="", help="SQLite path")
    parser.add_argument("--backup", type=str, default="", help="mod_backup root")
    parser.add_argument(
        "--out-dir",
        type=str,
        default="",
        help="Audit output directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate candidates only; do not write .info",
    )
    args = parser.parse_args(argv)

    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else (ROOT / "tools" / "_audit_out")
    )
    preview_path = (
        Path(args.preview)
        if args.preview
        else (out_dir / "identity_repair_preview.json")
    )
    if not preview_path.is_file():
        print(f"missing preview: {preview_path}", file=sys.stderr)
        return 2

    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    db_path = Path(args.db) if args.db else Path(
        _text((preview.get("source_plan") or {}).get("db_path"))
        or str(ROOT / "data" / "mod_manager.db")
    )
    backup_root = Path(args.backup) if args.backup else Path(
        _text((preview.get("source_plan") or {}).get("backup_path"))
        or str(ROOT / "data" / "mod_backup")
    )

    items = [
        i
        for i in (preview.get("items") or [])
        if _text(i.get("proposed_action")) == ALLOWED_ACTION
    ]
    if not items:
        print("no RESTORE_INFO_FROM_BACKUP items in preview")
        return 0

    for i in items:
        if _text(i.get("proposed_action")) != ALLOWED_ACTION:
            print("forbidden action in apply set", file=sys.stderr)
            return 3

    before_path = out_dir / "identity_recovery_before.json"
    after_path = out_dir / "identity_recovery_after.json"
    apply_log_path = out_dir / "identity_recovery_apply_log.json"
    rollback_root = out_dir / "identity_recovery_rollback"

    db_bytes_before = db_path.read_bytes()
    before_snap = snapshot_mods(db_path)
    before_path.write_text(
        json.dumps(before_snap, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    ready, skipped = preflight_restore_items(items, backup_root=backup_root)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "preview_restore_count": len(items),
                    "ready_count": len(ready),
                    "skipped": skipped,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not ready:
        after_snap = snapshot_mods(db_path)
        after_path.write_text(
            json.dumps(after_snap, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log = {
            "generated_at": _now(),
            "phase": "2-C",
            "status": "NO_SAFE_CANDIDATES",
            "production_mutation": "NONE",
            "db_bytes_unchanged": db_path.read_bytes() == db_bytes_before,
            "frozen_fields_unchanged": assert_frozen_unchanged(before_snap, after_snap)
            == [],
            "preview_restore_count": len(items),
            "ready_count": 0,
            "skipped": skipped,
            "message": (
                "No RESTORE_INFO_FROM_BACKUP items passed strict identity match; "
                "nothing applied"
            ),
        }
        apply_log_path.write_text(
            json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"before={before_path}")
        print(f"after={after_path}")
        print(f"log={apply_log_path}")
        print("restored_count=0 (no safe candidates)")
        return 0

    try:
        apply_result = apply_restore_items(
            ready, backup_root=backup_root, rollback_root=rollback_root
        )
    except Exception as exc:
        after_snap = snapshot_mods(db_path)
        after_path.write_text(
            json.dumps(after_snap, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        frozen_errs = assert_frozen_unchanged(before_snap, after_snap)
        db_bytes_after = db_path.read_bytes()
        log = {
            "generated_at": _now(),
            "phase": "2-C",
            "status": "FAILED",
            "error": str(exc),
            "db_bytes_unchanged": db_bytes_before == db_bytes_after,
            "frozen_field_errors": frozen_errs,
            "skipped": skipped,
            "attempted": [i.get("finding_id") for i in ready],
        }
        apply_log_path.write_text(
            json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"FAILED: {exc}", file=sys.stderr)
        print(f"log={apply_log_path}")
        return 1

    after_snap = snapshot_mods(db_path)
    after_path.write_text(
        json.dumps(after_snap, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    db_bytes_after = db_path.read_bytes()
    frozen_errs = assert_frozen_unchanged(before_snap, after_snap)
    if db_bytes_before != db_bytes_after or frozen_errs:
        log = {
            "generated_at": _now(),
            "phase": "2-C",
            "status": "FAILED_INTEGRITY",
            "db_bytes_unchanged": db_bytes_before == db_bytes_after,
            "frozen_field_errors": frozen_errs,
            "skipped": skipped,
            "apply_result": apply_result,
        }
        apply_log_path.write_text(
            json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("FAILED: unexpected DB mutation detected", file=sys.stderr)
        print(f"frozen_errors={frozen_errs}")
        return 1

    log = {
        "generated_at": _now(),
        "phase": "2-C",
        "status": "OK",
        "production_mutation": "INFO_ONLY",
        "db_bytes_unchanged": True,
        "frozen_fields_unchanged": True,
        "allowed_action": ALLOWED_ACTION,
        "before_snapshot": str(before_path),
        "after_snapshot": str(after_path),
        "rollback_root": str(rollback_root),
        "preview_restore_count": len(items),
        "skipped": skipped,
        "apply_result": apply_result,
        "guards": {
            "creates_mods": False,
            "deletes_mods": False,
            "merges_mods": False,
            "modifies_internal_id": False,
            "modifies_workspace_id": False,
            "modifies_external_id": False,
            "calls_create_mod_identity": False,
            "calls_workspace_resolver": False,
            "triggers_reconcile": False,
        },
    }
    apply_log_path.write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"before={before_path}")
    print(f"after={after_path}")
    print(f"log={apply_log_path}")
    print(f"restored_count={apply_result['restored_count']}")
    print(f"skipped_count={len(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
