"""Audit and delete legacy ``data/mod_backup/<workspace_id>/`` buckets.

Current Backup storage key is ``mods.mod_id``. A numeric top-level directory
that is **not** a current ``mods.mod_id`` may be a leftover workspace-id
bucket from the previous storage-key scheme.

Never delete ``data/mod_backup/<mod_id>/``, including the historical Steam
case where ``workspace_id == mod_id``. Pairing uses DB
``mods.workspace_id`` → unique ``mods.mod_id``, not folder-name guessing.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.paths import project_root
from services.backup_storage_cleanup import (
    BACKUP_METADATA_NAME,
    BACKUP_OFFLINE_DIR,
    BACKUP_OFFLINE_INDEX,
    _backup_snapshot,
    _content_not_unique_to_orphan,
    _cover_file,
    _dir_file_bytes,
)
from services.metadata_backup import BACKUP_DIR_NAME, backup_root, prove_backup_storage_key

logger = logging.getLogger(__name__)

REASON_SAFE = "safe_redundant_workspace_bucket"
REASON_MULTIPLE = "multiple_entity_candidates"
REASON_CURRENT_MISSING = "current_backup_missing"
REASON_CURRENT_INVALID = "current_backup_invalid"
REASON_UNIQUE_DATA = "legacy_has_unique_content"
REASON_WRITER = "runtime_writer_risk"
REASON_NO_ENTITY = "no_current_entity"
REASON_CURRENT_KEY = "current_mod_id_storage_key"
REASON_NOT_NUMERIC = "not_numeric_bucket"


def backup_tree_root(root: Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    from core.paths import data_dir

    return data_dir() / BACKUP_DIR_NAME


def backup_writer_locked_to_mod_id() -> dict[str, Any]:
    """True when Backup writers cannot emit a workspace-id storage key."""
    from core.models import ModMetadata

    issues: list[str] = []
    meta = ModMetadata(published_file_id="3691316854", title="Probe")
    if meta.entity_internal_id() == "3691316854":
        issues.append("entity_internal_id falls back to published_file_id")
    if str(meta.entity_internal_id() or "").strip():
        issues.append("unbound DTO entity_internal_id is non-empty")

    models_src = (project_root() / "core" / "models.py").read_text(encoding="utf-8")
    start = models_src.find("def entity_internal_id")
    end = models_src.find("\n    def ", start + 1) if start >= 0 else -1
    body = models_src[start:end] if start >= 0 and end > start else models_src
    if "return str(self.published_file_id" in body:
        issues.append("entity_internal_id source still mentions published_file_id")

    backup_src = (project_root() / "services" / "metadata_backup.py").read_text(
        encoding="utf-8"
    )
    if "def prove_backup_storage_key" not in backup_src:
        issues.append("prove_backup_storage_key missing")
    if "prove_backup_storage_key(owner_mod_id" not in backup_src:
        issues.append("snapshot_from_mod_folder does not prove storage key")

    sync_src = (
        project_root() / "services" / "metadata_backup_sync.py"
    ).read_text(encoding="utf-8")
    if "prove_backup_storage_key" not in sync_src:
        issues.append("_sync_backup_now does not prove storage key")

    try:
        proven = prove_backup_storage_key("3691316854")
    except Exception as exc:  # noqa: BLE001
        proven = ""
        issues.append(f"prove_backup_storage_key raised: {exc}")
    if proven == "3691316854":
        row = None
        try:
            from core.db_manager import get_db

            row = get_db().get_mod("3691316854")
        except Exception:  # noqa: BLE001
            row = None
        if row is None:
            issues.append(
                "prove_backup_storage_key accepted a workshop id that is not a PK"
            )

    return {
        "locked": not issues,
        "issues": issues,
        "runtime_reference_risk": bool(issues),
    }


def _load_entities(db: Any | None = None) -> list[dict[str, str]]:
    if db is None:
        from core.db_manager import get_db

        db = get_db()
    return list(db.iter_mod_backup_key_rows())


def _index_entities(
    rows: Iterable[dict[str, str]],
) -> tuple[
    set[str],
    dict[str, list[dict[str, str]]],
    dict[str, list[dict[str, str]]],
    set[str],
]:
    current_mod_ids: set[str] = set()
    by_workspace: dict[str, list[dict[str, str]]] = {}
    by_frozen: dict[str, list[dict[str, str]]] = {}
    frozen_ids: set[str] = set()
    for row in rows:
        mid = str(row.get("mod_id") or "").strip()
        if not mid.isdigit():
            continue
        current_mod_ids.add(mid)
        frozen = str(row.get("internal_id") or "").strip()
        if frozen:
            frozen_ids.add(frozen)
            by_frozen.setdefault(frozen, []).append(row)
        wid = str(row.get("workspace_id") or "").strip()
        if wid:
            by_workspace.setdefault(wid, []).append(row)
    return current_mod_ids, by_workspace, by_frozen, frozen_ids


def _pair_legacy_entity(
    *,
    bucket_name: str,
    snap: dict[str, Any],
    by_workspace: dict[str, list[dict[str, str]]],
    by_frozen: dict[str, list[dict[str, str]]],
) -> tuple[list[dict[str, str]], str]:
    """Match leftover bucket → unique current Entity. Folder name is never identity."""
    named = list(by_workspace.get(bucket_name) or [])
    if named:
        return named, "workspace_id_folder_name"
    meta_ws = str(snap.get("workspace_id") or "").strip()
    if meta_ws:
        hits = list(by_workspace.get(meta_ws) or [])
        if hits:
            return hits, "metadata_workspace_id"
    iid = str(snap.get("internal_id") or "").strip()
    if iid:
        hits = list(by_frozen.get(iid) or [])
        if hits:
            return hits, "metadata_internal_id"
    return [], ""


def _legacy_extra_names(bucket: Path) -> list[str]:
    extras: list[str] = []
    if not bucket.is_dir():
        return extras
    for child in bucket.iterdir():
        name = child.name.lower()
        if name == BACKUP_METADATA_NAME:
            continue
        if name.startswith("cover.") and child.is_file():
            continue
        if name == "offline" and child.is_dir():
            continue
        extras.append(child.name)
    return extras


def _current_backup_state(mod_id: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    hit = cache.get(mod_id)
    if hit is not None:
        return hit
    dest = backup_root(mod_id)
    if not dest.is_dir():
        state = {
            "exists": False,
            "valid": False,
            "reason": REASON_CURRENT_MISSING,
            "issues": ["current backup directory missing"],
        }
        cache[mod_id] = state
        return state
    from services.metadata_backup_validator import (
        BACKUP_STATUS_COMPLETE,
        status_from_validation,
        validate_backup,
    )

    result = validate_backup(mod_id)
    status = status_from_validation(result)
    valid = status == BACKUP_STATUS_COMPLETE
    state = {
        "exists": True,
        "valid": valid,
        "reason": "" if valid else REASON_CURRENT_INVALID,
        "issues": list(result.get("issues") or []),
        "status": status,
        "metadata_ok": bool(result.get("metadata_ok")),
        "cover_ok": bool(result.get("cover_ok")),
        "offline_ok": bool(result.get("offline_ok")),
    }
    cache[mod_id] = state
    return state


def _orphan_category(
    snap: dict[str, Any],
    *,
    frozen_ids: set[str],
) -> tuple[str, str]:
    """Classify a leftover with no unique current Entity."""
    iid = str(snap.get("internal_id") or "").strip()
    has_recovery = bool(
        snap.get("has_cover")
        or snap.get("has_index")
        or snap.get("title")
        or iid
        or snap.get("metadata_exists")
    )
    if not has_recovery:
        return "safe_redundant", "empty_or_unreadable_legacy_bucket"
    if iid and iid not in frozen_ids:
        return "recovery_valuable", "abandoned_frozen_id_not_in_mods"
    if iid and iid in frozen_ids:
        return "recovery_valuable", "frozen_id_exists_but_workspace_unmatched"
    if snap.get("has_cover") or snap.get("has_index"):
        return "recovery_valuable", "unique_or_unpaired_cover_or_offline"
    if snap.get("metadata_exists"):
        return "recovery_valuable", "historical_metadata_without_current_entity"
    return "unknown", "cannot_judge_legacy_bucket"


def inventory_legacy_workspace_buckets(
    *,
    backup_root_path: Path | None = None,
    db: Any | None = None,
) -> dict[str, Any]:
    """Top-level numeric census. Does not walk payload trees for identity."""
    root = backup_tree_root(backup_root_path)
    rows = _load_entities(db)
    current_mod_ids, by_workspace, _by_frozen, _frozen = _index_entities(rows)
    buckets: list[dict[str, Any]] = []
    total_bytes = 0
    children = sorted(root.iterdir(), key=lambda p: p.name) if root.is_dir() else []
    for child in children:
        if not child.is_dir() or not child.name.isdigit():
            continue
        if child.name in current_mod_ids:
            continue
        files, nbytes = _dir_file_bytes(child)
        total_bytes += nbytes
        matches = list(by_workspace.get(child.name) or [])
        current = matches[0] if len(matches) == 1 else None
        buckets.append(
            {
                "legacy_bucket": child.name,
                "legacy_path": str(child),
                "workspace_id": child.name,
                "current_mod_id": str((current or {}).get("mod_id") or ""),
                "current_internal_id": str((current or {}).get("internal_id") or ""),
                "entity_candidates": [
                    {
                        "mod_id": str(row.get("mod_id") or ""),
                        "internal_id": str(row.get("internal_id") or ""),
                    }
                    for row in matches
                ],
                "candidate_count": len(matches),
                "bytes": nbytes,
                "file_count": files,
            }
        )
    return {
        "backup_root": str(root),
        "legacy_bucket_count": len(buckets),
        "legacy_bucket_total_bytes": total_bytes,
        "current_entity_count": len(current_mod_ids),
        "buckets": buckets,
    }


def classify_legacy_workspace_buckets(
    *,
    backup_root_path: Path | None = None,
    db: Any | None = None,
    writer_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dry-run classification. Never deletes."""
    root = backup_tree_root(backup_root_path)
    writer = writer_gate if writer_gate is not None else backup_writer_locked_to_mod_id()
    writer_blocked = bool(writer.get("runtime_reference_risk"))
    rows = _load_entities(db)
    current_mod_ids, by_workspace, by_frozen, frozen_ids = _index_entities(rows)
    entity_pairs = {(r["mod_id"], r.get("internal_id") or "") for r in rows}

    safe: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    multiple: list[dict[str, Any]] = []
    unique_data: list[dict[str, Any]] = []
    no_entity: list[dict[str, Any]] = []
    validity_cache: dict[str, dict[str, Any]] = {}

    children = sorted(root.iterdir(), key=lambda p: p.name) if root.is_dir() else []
    scanned = 0
    for child in children:
        if not child.is_dir() or not child.name.isdigit():
            continue
        if child.name in current_mod_ids:
            continue
        scanned += 1
        if scanned % 200 == 0:
            logger.info("legacy workspace classify scanned=%s", scanned)
        snap = _backup_snapshot(child)
        files, nbytes = int(snap.get("files") or 0), int(snap.get("bytes") or 0)
        extras = _legacy_extra_names(child)
        matches, pair_via = _pair_legacy_entity(
            bucket_name=child.name,
            snap=snap,
            by_workspace=by_workspace,
            by_frozen=by_frozen,
        )
        base = {
            "legacy_bucket": child.name,
            "legacy_path": str(child),
            "workspace_id": child.name,
            "bytes": nbytes,
            "file_count": files,
            "current_mod_id": "",
            "current_internal_id": "",
            "extras": extras,
            "pair_via": pair_via,
        }

        if len(matches) > 1:
            row = {
                **base,
                "reason": REASON_MULTIPLE,
                "category": "multiple_entity_candidates",
                "entity_candidates": [
                    {
                        "mod_id": str(m.get("mod_id") or ""),
                        "internal_id": str(m.get("internal_id") or ""),
                    }
                    for m in matches
                ],
            }
            multiple.append(row)
            blocked.append(row)
            continue

        if not matches:
            cat, why = _orphan_category(snap, frozen_ids=frozen_ids)
            row = {
                **base,
                "reason": REASON_NO_ENTITY,
                "category": cat,
                "why": why,
                "bucket_internal_id": str(snap.get("internal_id") or ""),
                "title": str(snap.get("title") or ""),
            }
            if cat == "safe_redundant":
                row["reason"] = REASON_SAFE
                row["why"] = why
                safe.append(row)
                continue
            no_entity.append(row)
            blocked.append(row)
            continue

        entity = matches[0]
        current_mod_id = str(entity.get("mod_id") or "").strip()
        current_internal_id = str(entity.get("internal_id") or "").strip()
        base["current_mod_id"] = current_mod_id
        base["current_internal_id"] = current_internal_id
        if not current_mod_id.isdigit() or current_mod_id == child.name:
            blocked.append(
                {
                    **base,
                    "reason": REASON_CURRENT_KEY,
                    "category": "unknown",
                    "why": "workspace_id equals current mod_id; not a legacy bucket",
                }
            )
            continue

        state = _current_backup_state(current_mod_id, validity_cache)
        if not state.get("exists"):
            row = {
                **base,
                "reason": REASON_CURRENT_MISSING,
                "category": "current_backup_missing",
                "issues": list(state.get("issues") or []),
            }
            missing.append(row)
            blocked.append(row)
            continue
        if not state.get("valid"):
            row = {
                **base,
                "reason": REASON_CURRENT_INVALID,
                "category": "current_backup_invalid",
                "issues": list(state.get("issues") or []),
                "status": state.get("status"),
            }
            invalid.append(row)
            blocked.append(row)
            continue

        live = _backup_snapshot(backup_root(current_mod_id))
        unique = extras or not _content_not_unique_to_orphan(snap, live)
        if unique:
            row = {
                **base,
                "reason": REASON_UNIQUE_DATA,
                "category": "unique_data",
                "why": "legacy cover/offline/extra not present on current backup",
                "extras": extras,
            }
            unique_data.append(row)
            blocked.append(row)
            continue

        safe.append(
            {
                **base,
                "reason": REASON_SAFE,
                "category": "safe_to_delete",
                "why": (
                    f"{child.name} → current mod_id={current_mod_id} "
                    "→ current backup valid → no unique content → safe delete"
                ),
            }
        )

    retained_by_cat: dict[str, int] = {}
    retained_bytes_by_cat: dict[str, int] = {}
    for row in blocked:
        cat = str(row.get("category") or "unknown")
        retained_by_cat[cat] = retained_by_cat.get(cat, 0) + 1
        retained_bytes_by_cat[cat] = retained_bytes_by_cat.get(cat, 0) + int(
            row.get("bytes") or 0
        )

    safe_bytes = sum(int(r.get("bytes") or 0) for r in safe)
    blocked_bytes = sum(int(r.get("bytes") or 0) for r in blocked)
    recovery_valuable = [
        r for r in no_entity if str(r.get("category") or "") == "recovery_valuable"
    ]
    manual_decision = [
        r
        for r in blocked
        if str(r.get("category") or "")
        not in {"recovery_valuable", "safe_to_delete"}
    ]
    return {
        "backup_root": str(root),
        "writer": writer,
        "entity_count": len(current_mod_ids),
        "entity_identity_pairs": sorted(entity_pairs),
        "legacy_bucket_count": len(safe) + len(blocked),
        "legacy_bucket_total_bytes": safe_bytes + blocked_bytes,
        "safe_to_delete": safe,
        "blocked": blocked,
        "current_backup_missing": missing,
        "current_backup_invalid": invalid,
        "multiple_entity_candidates": multiple,
        "unique_data": unique_data,
        "no_current_entity": no_entity,
        "recovery_valuable": recovery_valuable,
        "manual_decision": manual_decision,
        "runtime_reference_risk": writer_blocked,
        "bytes_reclaimable": safe_bytes,
        "counts": {
            "total_legacy_buckets": len(safe) + len(blocked),
            "safe_to_delete": len(safe),
            "safe_redundant": len(safe),
            "blocked": len(blocked),
            "current_backup_missing": len(missing),
            "current_backup_invalid": len(invalid),
            "multiple_entity_candidates": len(multiple),
            "unique_data": len(unique_data),
            "runtime_reference_risk": 1 if writer_blocked else 0,
            "no_current_entity": len(no_entity),
            "recovery_valuable": len(recovery_valuable),
            "manual_decision": len(manual_decision),
            "bytes_reclaimable": safe_bytes,
        },
        "retained_by_category": retained_by_cat,
        "retained_bytes_by_category": retained_bytes_by_cat,
        "safe_deletion_examples": [
            {
                "legacy_bucket": row["legacy_bucket"],
                "current_mod_id": row["current_mod_id"],
                "current_internal_id": row["current_internal_id"],
                "why": row["why"],
            }
            for row in safe[:20]
        ],
    }


def _remap_project_file(raw: str) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        direct = Path(text)
        if direct.is_file():
            return direct
    except OSError:
        pass
    from core.paths import project_root

    root = str(project_root())
    lowered = text.replace("/", "\\").lower()
    needle = "\\project\\steam-mod-manager"
    idx = lowered.find(needle)
    if idx < 0:
        return None
    tail = text[idx + len("\\project\\steam-mod-manager") :].lstrip("\\/")
    cand = Path(root) / Path(tail)
    try:
        return cand if cand.is_file() else None
    except OSError:
        return None


def _copy_as_backup_cover(src: Path, dest: Path) -> str:
    suffix = src.suffix.lower() or ".jpg"
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        suffix = ".jpg"
    target = dest / f"cover{suffix}"
    shutil.copy2(src, target)
    return target.name


def _load_backup_metadata(bucket: Path) -> dict[str, Any]:
    meta = bucket / BACKUP_METADATA_NAME
    if not meta.is_file():
        return {}
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_backup_metadata(bucket: Path, payload: dict[str, Any]) -> None:
    (bucket / BACKUP_METADATA_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def repair_invalid_current_from_legacy(
    classified: dict[str, Any],
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Repair current ``mod_id`` Backup from leftover / live known paths. Never mkdir Mod."""
    repaired: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    try:
        from core.db_manager import get_db

        database = get_db()
    except Exception:  # noqa: BLE001
        database = None

    for row in list(classified.get("current_backup_invalid") or []):
        current_mod_id = str(row.get("current_mod_id") or "").strip()
        legacy_path = Path(str(row.get("legacy_path") or ""))
        dest = backup_root(current_mod_id) if current_mod_id.isdigit() else None
        record: dict[str, Any] = {
            "legacy_bucket": str(row.get("legacy_bucket") or ""),
            "current_mod_id": current_mod_id,
            "issues": list(row.get("issues") or []),
            "actions": [],
            "dry_run": dry_run,
        }
        if dest is None:
            record["ok"] = False
            record["error"] = "missing dest"
            failed.append(record)
            continue
        issues_text = " ".join(str(i).lower() for i in record["issues"])
        need_cover = "cover" in issues_text
        need_offline = "offline" in issues_text or "index.html" in issues_text
        need_url = "source_url" in issues_text
        db_row = {}
        if database is not None:
            try:
                db_row = database.get_mod_backup_row(current_mod_id) or {}
            except Exception:  # noqa: BLE001
                db_row = {}
        lkp = Path(str(db_row.get("last_known_path") or "").strip())
        dest_meta = _load_backup_metadata(dest)
        legacy_meta = _load_backup_metadata(legacy_path) if legacy_path.is_dir() else {}
        try:
            if not dry_run:
                dest.mkdir(parents=True, exist_ok=True)
            if need_cover and not list(dest.glob("cover.*")):
                src_cover = _cover_file(legacy_path) if legacy_path.is_dir() else None
                if src_cover is None:
                    src_cover = _remap_project_file(str(dest_meta.get("cover_path") or ""))
                if src_cover is None and lkp.is_dir():
                    for cand in (
                        *sorted(lkp.glob("thumbnail.*")),
                        *sorted((lkp / ".info").glob("cover.*")),
                    ):
                        if cand.is_file():
                            src_cover = cand
                            break
                if src_cover is not None:
                    record["actions"].append(f"cover:{src_cover.name}")
                    if not dry_run:
                        _copy_as_backup_cover(src_cover, dest)
            if need_offline:
                dest_index = dest / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
                src_index = (
                    legacy_path / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
                    if legacy_path.is_dir()
                    else None
                )
                live_index = None
                if lkp.is_dir():
                    for cand in (
                        lkp / ".info" / "index.html",
                        lkp / ".info" / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX,
                    ):
                        if cand.is_file():
                            live_index = cand
                            break
                chosen = None
                if src_index is not None and src_index.is_file():
                    chosen = src_index
                elif live_index is not None:
                    chosen = live_index
                if chosen is not None and not dest_index.is_file():
                    record["actions"].append("offline/index.html")
                    if not dry_run:
                        dest_index.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(chosen, dest_index)
                elif not dest_index.is_file():
                    record["actions"].append("clear_stale_offline_status")
                    if not dry_run:
                        dest_meta["offline_status"] = "none"
                        dest_meta.pop("offline_page_path", None)
                        dest_meta.pop("offline_page", None)
                        _write_backup_metadata(dest, dest_meta)
                        if database is not None:
                            database.update_mod_offline_status(
                                current_mod_id, status="none"
                            )
                src_assets = legacy_path / BACKUP_OFFLINE_DIR / "assets"
                dest_offline = dest / BACKUP_OFFLINE_DIR
                dest_assets = dest_offline / "assets"
                if src_assets.is_dir():
                    staged = 0
                    for child in src_assets.rglob("*"):
                        if not child.is_file():
                            continue
                        rel = child.relative_to(src_assets)
                        target = dest_assets / rel
                        if target.exists():
                            continue
                        staged += 1
                        if not dry_run:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(child, target)
                    if staged:
                        record["actions"].append(
                            f"offline/cas-ingest:{staged} (no durable assets)"
                        )
                    if not dry_run and staged:
                        from services.backup_asset_migration import (
                            sync_backup_offline_manifest,
                        )
                        from services.offline.backup_closure import (
                            clear_backup_offline_assets_dir,
                        )

                        sync_backup_offline_manifest(dest_offline, dry_run=False)
                        clear_backup_offline_assets_dir(dest_offline)
            if need_url:
                url = str(
                    legacy_meta.get("source_url")
                    or legacy_meta.get("url")
                    or dest_meta.get("url")
                    or ""
                ).strip()
                if url:
                    record["actions"].append("source_url")
                    if not dry_run:
                        dest_meta["source_url"] = url
                        dest_meta.setdefault("url", url)
                        _write_backup_metadata(dest, dest_meta)
            if dry_run:
                record["ok"] = True
                repaired.append(record)
                continue
            state = _current_backup_state(current_mod_id, {})
            record["valid_after"] = bool(state.get("valid"))
            record["issues_after"] = list(state.get("issues") or [])
            record["ok"] = bool(state.get("valid"))
            if record["ok"]:
                repaired.append(record)
            else:
                failed.append(record)
        except OSError as exc:
            record["ok"] = False
            record["error"] = str(exc)
            failed.append(record)
    return {
        "repaired": repaired,
        "failed": failed,
        "repaired_count": len(repaired),
        "failed_count": len(failed),
        "dry_run": dry_run,
    }


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def delete_safe_legacy_workspace_buckets(
    classified: dict[str, Any],
    *,
    dry_run: bool = True,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    """Delete only classified ``safe_to_delete`` rows after a writer lock check."""
    writer = classified.get("writer") or backup_writer_locked_to_mod_id()
    if writer.get("runtime_reference_risk"):
        return {
            "deleted": 0,
            "bytes": 0,
            "errors": 0,
            "skipped": len(classified.get("safe_to_delete") or []),
            "dry_run": dry_run,
            "refused": "runtime_writer_risk",
            "writer_issues": list(writer.get("issues") or []),
            "audit_rows": [],
        }

    audit: list[dict[str, Any]] = []
    deleted = 0
    bytes_removed = 0
    files_removed = 0
    errors = 0
    current_still_valid = 0
    current_invalid_after = 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    live_ids = {str(mid) for mid, _iid in classified.get("entity_identity_pairs") or []}

    for row in list(classified.get("safe_to_delete") or []):
        legacy_path = Path(str(row.get("legacy_path") or ""))
        name = str(row.get("legacy_bucket") or legacy_path.name)
        current_mod_id = str(row.get("current_mod_id") or "").strip()
        record = {
            "ts": stamp,
            "legacy_path": str(legacy_path),
            "workspace_id": str(row.get("workspace_id") or name),
            "current_mod_id": current_mod_id,
            "current_internal_id": str(row.get("current_internal_id") or ""),
            "size": int(row.get("bytes") or 0),
            "file_count": int(row.get("file_count") or 0),
            "reason": str(row.get("reason") or REASON_SAFE),
            "dry_run": dry_run,
        }
        if not name.isdigit() or name in live_ids:
            record["status"] = "refused_current_mod_id"
            errors += 1
            audit.append(record)
            continue
        if current_mod_id == name:
            record["status"] = "refused_workspace_equals_mod_id"
            errors += 1
            audit.append(record)
            continue
        if dry_run:
            record["status"] = "dry_run"
            audit.append(record)
            continue
        if not legacy_path.is_dir():
            record["status"] = "already_absent"
            audit.append(record)
            continue
        try:
            shutil.rmtree(legacy_path)
        except OSError as exc:
            logger.warning("failed to delete legacy backup %s: %s", legacy_path, exc)
            record["status"] = "error"
            record["error"] = str(exc)
            errors += 1
            audit.append(record)
            continue
        if legacy_path.exists():
            record["status"] = "half_delete"
            errors += 1
            audit.append(record)
            continue
        if current_mod_id.isdigit():
            state = _current_backup_state(current_mod_id, {})
            if not state.get("valid"):
                record["status"] = "deleted_but_current_invalid"
                record["current_issues"] = list(state.get("issues") or [])
                current_invalid_after += 1
                errors += 1
                audit.append(record)
                continue
            record["current_backup_valid"] = True
            current_still_valid += 1
        record["status"] = "deleted"
        record["legacy_path_exists"] = False
        deleted += 1
        bytes_removed += int(row.get("bytes") or 0)
        files_removed += int(row.get("file_count") or 0)
        audit.append(record)

    if audit_path is not None:
        _write_jsonl(audit_path, audit)
    return {
        "deleted": deleted,
        "bytes": bytes_removed,
        "files": files_removed,
        "errors": errors,
        "dry_run": dry_run,
        "current_still_valid": current_still_valid,
        "current_invalid_after": current_invalid_after,
        "audit_path": str(audit_path) if audit_path else "",
        "audit_rows": audit,
    }


def summarize_report(
    *,
    before: dict[str, Any],
    delete_result: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    before_count = int(before.get("legacy_bucket_count") or 0)
    before_bytes = int(before.get("legacy_bucket_total_bytes") or 0)
    deleted = int(delete_result.get("deleted") or 0)
    deleted_bytes = int(delete_result.get("bytes") or 0)
    after_count = int(after.get("legacy_bucket_count") or 0)
    after_bytes = int(after.get("legacy_bucket_total_bytes") or 0)
    return {
        "before": {
            "legacy_bucket_count": before_count,
            "legacy_bytes": before_bytes,
        },
        "deleted": {"count": deleted, "bytes": deleted_bytes},
        "retained": {
            "count": after_count,
            "bytes": after_bytes,
            "by_category": dict(after.get("retained_by_category") or {}),
            "bytes_by_category": dict(after.get("retained_bytes_by_category") or {}),
        },
        "after": {
            "legacy_bucket_count": after_count,
            "legacy_bytes": after_bytes,
        },
        "safe_remaining": int((after.get("counts") or {}).get("safe_to_delete") or 0),
    }
