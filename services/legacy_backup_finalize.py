"""Final leftover Backup classification: migrate, prove redundant, or keep with a reason."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from services.backup_storage_cleanup import (
    BACKUP_OFFLINE_DIR,
    BACKUP_OFFLINE_INDEX,
    _backup_snapshot,
    _content_not_unique_to_orphan,
    _cover_file,
    _sha256_file,
)
from services.legacy_workspace_backup import (
    REASON_SAFE,
    classify_legacy_workspace_buckets,
    delete_safe_legacy_workspace_buckets,
    repair_invalid_current_from_legacy,
)
from services.metadata_backup import backup_root

logger = logging.getLogger(__name__)


def leftover_manifest_row(classified_row: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(classified_row.get("legacy_path") or ""))
    snap = _backup_snapshot(path) if path.is_dir() else {}
    current = str(classified_row.get("current_mod_id") or "").strip()
    current_snap = _backup_snapshot(backup_root(current)) if current.isdigit() else {}
    return {
        "legacy_path": str(path),
        "legacy_bucket": str(classified_row.get("legacy_bucket") or path.name),
        "size": int(classified_row.get("bytes") or snap.get("bytes") or 0),
        "file_count": int(classified_row.get("file_count") or snap.get("files") or 0),
        "historical_internal_id": str(
            classified_row.get("bucket_internal_id")
            or snap.get("internal_id")
            or ""
        ),
        "workspace_id": str(
            classified_row.get("workspace_id") or snap.get("workspace_id") or path.name
        ),
        "metadata_availability": bool(snap.get("metadata_exists")),
        "cover_availability": bool(snap.get("has_cover")),
        "offline_availability": bool(snap.get("has_index")),
        "current_entity_match": current,
        "current_internal_id": str(classified_row.get("current_internal_id") or ""),
        "current_backup_match": bool(current_snap.get("metadata_exists")),
        "current_mod_id": current,
        "unique_data": str(classified_row.get("category") or "") == "unique_data",
        "unique_metadata": bool(snap.get("metadata_exists")),
        "unique_cover": bool(snap.get("has_cover")),
        "unique_offline": bool(snap.get("has_index")),
        "category": str(classified_row.get("category") or ""),
        "why": str(classified_row.get("why") or classified_row.get("reason") or ""),
        "title": str(classified_row.get("title") or snap.get("title") or ""),
        "pair_via": str(classified_row.get("pair_via") or ""),
        "meta_sha": str(snap.get("meta_sha") or ""),
        "cover_sha": str(snap.get("cover_sha") or ""),
        "index_sha": str(snap.get("index_sha") or ""),
        "extras": list(classified_row.get("extras") or []),
        "issues": list(classified_row.get("issues") or []),
        "entity_candidates": list(classified_row.get("entity_candidates") or []),
    }


def _live_offline_sha(mod_id: str, *, db: Any = None) -> str | None:
    try:
        from core.db_manager import get_db
        from services.offline.paths import resolve_offline_page

        database = db if db is not None else get_db()
        row = database.get_mod_backup_row(mod_id) or {}
        lkp = Path(str(row.get("last_known_path") or "").strip())
        if not lkp.is_dir():
            return None
        live = resolve_offline_page(lkp)
        if live is None or not live.is_file():
            return None
        return _sha256_file(live)
    except Exception:  # noqa: BLE001
        return None


def _current_offline_sha(mod_id: str) -> str | None:
    idx = backup_root(mod_id) / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    return _sha256_file(idx) if idx.is_file() else None


def _current_matches_live_offline(mod_id: str, *, db: Any = None) -> bool:
    live = _live_offline_sha(mod_id, db=db)
    current = _current_offline_sha(mod_id)
    return bool(live and current and live == current)


def _migrate_unique_into_current(row: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    """Copy only missing cover/offline into current. Never overwrite newer current files."""
    current = str(row.get("current_mod_id") or "").strip()
    legacy = Path(str(row.get("legacy_path") or ""))
    dest = backup_root(current) if current.isdigit() else None
    record: dict[str, Any] = {
        "legacy_bucket": str(row.get("legacy_bucket") or ""),
        "current_mod_id": current,
        "actions": [],
        "ok": False,
        "dry_run": dry_run,
    }
    extras = list(row.get("extras") or [])
    if dest is None or not legacy.is_dir() or extras:
        record["reason"] = "extras_or_missing_dest"
        return record
    live = _backup_snapshot(dest)
    snap = _backup_snapshot(legacy)
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    if snap.get("has_cover") and not live.get("has_cover"):
        src = _cover_file(legacy)
        if src is not None:
            record["actions"].append(f"cover:{src.name}")
            if not dry_run:
                shutil.copy2(src, dest / src.name)
    if snap.get("has_index") and not live.get("has_index"):
        src_index = legacy / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
        dest_index = dest / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
        if src_index.is_file():
            record["actions"].append("offline/index.html")
            if not dry_run:
                dest_index.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_index, dest_index)
        src_assets = legacy / BACKUP_OFFLINE_DIR / "assets"
        if src_assets.is_dir() and not dry_run:
            # Phase 5: ingest into Asset Store + Backup manifest; no durable dual-write.
            from services.offline.backup_closure import clear_backup_offline_assets_dir
            from services.backup_asset_migration import sync_backup_offline_manifest

            dest_offline = dest / BACKUP_OFFLINE_DIR
            dest_assets = dest_offline / "assets"
            for child in src_assets.rglob("*"):
                if not child.is_file():
                    continue
                target = dest_assets / child.relative_to(src_assets)
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, target)
            sync_backup_offline_manifest(dest_offline, dry_run=False)
            clear_backup_offline_assets_dir(dest_offline)
            record["actions"].append("offline/manifest+cas (no durable assets)")
    if not record["actions"]:
        record["reason"] = "hashes_differ_or_nothing_to_copy"
        return record
    record["ok"] = True
    return record


def _mark_safe(
    extra_safe: list[dict[str, Any]],
    row: dict[str, Any],
    man: dict[str, Any],
    why: str,
) -> None:
    man["final"] = "safe_redundant"
    man["final_why"] = why
    payload = {**row, "why": why, "reason": REASON_SAFE}
    if str(row.get("current_mod_id") or "").strip().isdigit():
        payload["current_mod_id"] = str(row.get("current_mod_id") or "").strip()
    elif man.get("current_entity_match"):
        payload["current_mod_id"] = str(man.get("current_entity_match") or "")
    extra_safe.append(payload)


def finalize_leftover_legacy_buckets(
    *,
    db: Any = None,
    dry_run: bool = True,
    backup_root_path: Path | None = None,
) -> dict[str, Any]:
    """Migrate unique files, mark proven duplicates safe, keep the rest with reasons."""
    try:
        from core.db_manager import get_db

        database = db if db is not None else get_db()
    except Exception:  # noqa: BLE001
        database = db

    classified = classify_legacy_workspace_buckets(
        db=database, backup_root_path=backup_root_path
    )
    repair = repair_invalid_current_from_legacy(classified, dry_run=dry_run)
    if not dry_run:
        classified = classify_legacy_workspace_buckets(
            db=database, backup_root_path=backup_root_path
        )

    migrated: list[dict[str, Any]] = []
    for row in list(classified.get("unique_data") or []):
        result = _migrate_unique_into_current(row, dry_run=dry_run)
        if result.get("ok"):
            migrated.append(result)
    if not dry_run and migrated:
        classified = classify_legacy_workspace_buckets(
            db=database, backup_root_path=backup_root_path
        )

    extra_safe: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []

    for row in classified.get("safe_to_delete") or []:
        man = leftover_manifest_row(row)
        man["final"] = "safe_redundant"
        man["final_why"] = str(
            row.get("why") or "paired current backup valid; no unique bytes"
        )
        manifests.append(man)

    for row in classified.get("unique_data") or []:
        man = leftover_manifest_row(row)
        extras = list(row.get("extras") or [])
        current = str(row.get("current_mod_id") or "")
        live = _backup_snapshot(backup_root(current)) if current.isdigit() else {}
        snap = _backup_snapshot(Path(str(row.get("legacy_path") or "")))
        if extras:
            man["final"] = "manual_decision"
            man["final_why"] = f"unique extras {extras}; will not auto-merge"
            manual.append(man)
            manifests.append(man)
            continue
        # Prefer migrate only when current is missing the asset.
        if snap.get("has_cover") and not live.get("has_cover"):
            mig = _migrate_unique_into_current(row, dry_run=dry_run)
            if mig.get("ok"):
                migrated.append(mig)
                _mark_safe(
                    extra_safe,
                    row,
                    man,
                    "migrated missing cover into current Backup; legacy superseded",
                )
                manifests.append(man)
                continue
        if snap.get("has_index") and not live.get("has_index"):
            mig = _migrate_unique_into_current(row, dry_run=dry_run)
            if mig.get("ok"):
                migrated.append(mig)
                _mark_safe(
                    extra_safe,
                    row,
                    man,
                    "migrated missing offline into current Backup; legacy superseded",
                )
                manifests.append(man)
                continue
        # Current already has assets: if current offline matches LIVE source,
        # legacy alternate hash is superseded (latest information wins).
        if current.isdigit() and _current_matches_live_offline(current, db=database):
            _mark_safe(
                extra_safe,
                row,
                man,
                (
                    "current offline snapshot matches LIVE source; "
                    "legacy cover/offline alternate superseded "
                    f"(cover L={man.get('cover_sha','')[:12]} "
                    f"C={live.get('cover_sha','')[:12]}; "
                    f"offline L={man.get('index_sha','')[:12]} "
                    f"C={live.get('index_sha','')[:12]})"
                ),
            )
            manifests.append(man)
            continue
        # Legacy matches live but current does not → migrate offline (never overwrite
        # metadata; only replace offline/cover when current is stale vs live).
        live_sha = _live_offline_sha(current, db=database) if current.isdigit() else None
        leg_sha = str(snap.get("index_sha") or "") or None
        cur_sha = str(live.get("index_sha") or "") or None
        if live_sha and leg_sha and live_sha == leg_sha and live_sha != cur_sha:
            # Replace stale current offline with live-matching legacy copy.
            dest = backup_root(current)
            src_index = Path(str(row.get("legacy_path") or "")) / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
            if src_index.is_file():
                if not dry_run:
                    dest_index = dest / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
                    dest_index.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_index, dest_index)
                migrated.append(
                    {
                        "legacy_bucket": row.get("legacy_bucket"),
                        "current_mod_id": current,
                        "actions": ["offline/index.html:from_legacy_matching_live"],
                        "ok": True,
                        "dry_run": dry_run,
                    }
                )
                _mark_safe(
                    extra_safe,
                    row,
                    man,
                    "legacy offline matched LIVE source; migrated over stale current offline",
                )
                manifests.append(man)
                continue
        man["final"] = "manual_decision"
        man["final_why"] = (
            "legacy and current both have cover/offline with differing hashes; "
            f"live_offline_match=none "
            f"L_cover={man.get('cover_sha','')[:12]} C_cover={live.get('cover_sha','')[:12]} "
            f"L_off={man.get('index_sha','')[:12]} C_off={live.get('index_sha','')[:12]} "
            f"bytes={man.get('size')}; keep until human picks authoritative snapshot"
        )
        manual.append(man)
        manifests.append(man)

    for row in classified.get("multiple_entity_candidates") or []:
        man = leftover_manifest_row(row)
        legacy = Path(str(row.get("legacy_path") or ""))
        snap = _backup_snapshot(legacy)
        duplicate_of: list[str] = []
        unique_vs: list[str] = []
        live_matched: list[str] = []
        for cand in row.get("entity_candidates") or []:
            mid = str(cand.get("mod_id") or "").strip()
            if not mid.isdigit():
                continue
            live = _backup_snapshot(backup_root(mid))
            if not live.get("metadata_exists"):
                unique_vs.append(mid)
                continue
            if _content_not_unique_to_orphan(snap, live) and not (row.get("extras") or []):
                duplicate_of.append(mid)
            else:
                unique_vs.append(mid)
            if _current_matches_live_offline(mid, db=database):
                live_matched.append(mid)
        if duplicate_of and not unique_vs:
            _mark_safe(
                extra_safe,
                {**row, "current_mod_id": duplicate_of[0]},
                man,
                (
                    "multiple current entities share workspace_id; leftover bytes are "
                    f"a subset of current Backup {duplicate_of}"
                ),
            )
        elif live_matched and len(live_matched) == len(
            [c for c in (row.get("entity_candidates") or []) if str(c.get("mod_id") or "").isdigit()]
        ):
            # workspace_id collision across entities: every current Backup already
            # matches its own LIVE offline — leftover remint debris is superseded.
            _mark_safe(
                extra_safe,
                {**row, "current_mod_id": live_matched[0]},
                man,
                (
                    "workspace_id collision mapped leftover to multiple Entities; "
                    f"each current Backup offline matches its LIVE source {live_matched}; "
                    "legacy remint debris superseded "
                    f"(historical_internal_id={man.get('historical_internal_id') or '-'})"
                ),
            )
        elif duplicate_of and not (row.get("extras") or []):
            _mark_safe(
                extra_safe,
                {**row, "current_mod_id": duplicate_of[0]},
                man,
                (
                    "multiple current entities; leftover is a duplicate of current "
                    f"Backup {duplicate_of} (other candidates {unique_vs})"
                ),
            )
        else:
            man["final"] = "manual_decision"
            man["final_why"] = (
                "one leftover workspace maps to multiple current entities; "
                f"unique vs {unique_vs}; duplicate of {duplicate_of}; "
                f"live_matched={live_matched}; "
                f"historical_internal_id={man.get('historical_internal_id') or '-'}"
            )
            manual.append(man)
        manifests.append(man)

    sha_groups: dict[str, list[dict[str, Any]]] = {}
    no_entity_rows = list(classified.get("no_current_entity") or [])
    for row in no_entity_rows:
        man = leftover_manifest_row(row)
        sha = str(man.get("meta_sha") or "") or f"nosha:{man['legacy_bucket']}"
        sha_groups.setdefault(sha, []).append((row, man))

    for _sha, group in sha_groups.items():
        keep_idx = 0
        for i, (row, man) in enumerate(group):
            has_assets = bool(
                man.get("cover_availability") or man.get("offline_availability")
            )
            has_meta = bool(man.get("metadata_availability"))
            empty = (
                not has_assets
                and not has_meta
                and int(man.get("file_count") or 0) == 0
            )
            if empty:
                _mark_safe(
                    extra_safe,
                    row,
                    man,
                    "empty leftover bucket; no metadata/cover/offline",
                )
                manifests.append(man)
                continue
            if i != keep_idx and not has_assets:
                _mark_safe(
                    extra_safe,
                    row,
                    man,
                    (
                        "duplicate historical metadata.json of leftover "
                        f"{group[keep_idx][1]['legacy_bucket']}"
                    ),
                )
                manifests.append(man)
                continue
            # No current Entity + no assets → abandoned historical metadata only.
            if not has_assets:
                _mark_safe(
                    extra_safe,
                    row,
                    man,
                    (
                        "no current Entity; abandoned historical metadata only "
                        f"(frozen={man.get('historical_internal_id') or '-'} "
                        f"workspace={man.get('workspace_id')} "
                        f"title={man.get('title') or '-'} bytes={man.get('size')}); "
                        "no cover/offline recovery evidence"
                    ),
                )
                manifests.append(man)
                continue
            # Unpaired cover/offline with no Entity and no Frozen id → not recoverable.
            frozen = str(man.get("historical_internal_id") or "").strip()
            if has_assets and not frozen:
                _mark_safe(
                    extra_safe,
                    row,
                    man,
                    (
                        "no current Entity; unpaired cover/offline without Frozen "
                        f"internal_id (workspace={man.get('workspace_id')} "
                        f"bytes={man.get('size')}); no unique recovery identity"
                    ),
                )
                manifests.append(man)
                continue
            man["final"] = "manual_decision"
            man["final_why"] = (
                "no current Entity; leftover holds cover/offline with Frozen id "
                f"frozen={frozen} workspace={man.get('workspace_id')} "
                f"bytes={man.get('size')}; retain for possible recovery"
            )
            manual.append(man)
            manifests.append(man)

    for row in classified.get("current_backup_invalid") or []:
        man = leftover_manifest_row(row)
        man["final"] = "manual_decision"
        man["final_why"] = (
            "paired current Backup still invalid: "
            + ", ".join(str(i) for i in (row.get("issues") or []))
        )
        manual.append(man)
        manifests.append(man)

    if extra_safe:
        classified = dict(classified)
        classified["safe_to_delete"] = list(classified.get("safe_to_delete") or []) + extra_safe

    delete = delete_safe_legacy_workspace_buckets(classified, dry_run=dry_run)
    after = classify_legacy_workspace_buckets(
        db=database, backup_root_path=backup_root_path
    )
    return {
        "repair": repair,
        "migrated": migrated,
        "delete": delete,
        "manifests": manifests,
        "manual_decision": manual,
        "after": after,
        "counts": {
            "manifests": len(manifests),
            "manual_decision": len(manual),
            "migrated": len(migrated),
            "deleted": int(delete.get("deleted") or 0),
            "safe_marked": len(extra_safe)
            + len(classified.get("safe_to_delete") or [])
            - len(extra_safe),
            "after_legacy": int(after.get("counts", {}).get("total_legacy_buckets") or 0),
            "after_invalid": int(after.get("counts", {}).get("current_backup_invalid") or 0),
        },
        "dry_run": dry_run,
    }
