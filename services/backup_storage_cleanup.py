"""One-shot data/ cleanup helpers for backup assets, orphan copies, and leftovers.

Does not touch SQLite. Callers pass filesystem paths and live mod_id sets.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

BACKUP_METADATA_NAME = "metadata.json"
BACKUP_COVER_PREFIX = "cover."
BACKUP_OFFLINE_DIR = "offline"
BACKUP_OFFLINE_INDEX = "index.html"


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _dir_file_bytes(path: Path) -> tuple[int, int]:
    files = 0
    nbytes = 0
    if not path.exists():
        return 0, 0
    for child in path.rglob("*"):
        if not child.is_file():
            continue
        try:
            nbytes += int(child.stat().st_size)
            files += 1
        except OSError:
            continue
    return files, nbytes


def _cover_file(bucket: Path) -> Path | None:
    matches = sorted(
        p for p in bucket.glob(f"{BACKUP_COVER_PREFIX}*") if p.is_file()
    )
    return matches[0] if matches else None


def _index_file(bucket: Path) -> Path | None:
    index = bucket / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX
    return index if index.is_file() else None


def _load_metadata(meta_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_internal_id(meta_path: Path) -> str:
    return str(_load_metadata(meta_path).get("internal_id") or "").strip()


def count_backup_assets(bucket: Path) -> tuple[int, int]:
    return _dir_file_bytes(bucket / BACKUP_OFFLINE_DIR / "assets")


def strip_live_backup_offline_assets(
    backup_root: Path,
    live_ids: Iterable[str],
) -> dict[str, int]:
    """Prune unreferenced files under live backup ``offline/``.

    Never ``rmtree`` the required dependency closure. Unreferenced leftovers
    (including unused ``assets/`` files) are deleted; required CSS/images stay.
    """
    from services.offline.backup_closure import prune_unreferenced_backup_offline

    dirs = 0
    files = 0
    nbytes = 0
    errors = 0
    for raw in live_ids:
        mid = str(raw or "").strip()
        if not mid.isdigit():
            continue
        dest = backup_root / mid / BACKUP_OFFLINE_DIR
        if not dest.exists():
            continue
        try:
            stats = prune_unreferenced_backup_offline(dest)
        except OSError as exc:
            logger.warning("failed to prune live backup offline %s: %s", dest, exc)
            errors += 1
            continue
        if int(stats.get("files") or 0) > 0:
            dirs += 1
            files += int(stats.get("files") or 0)
            nbytes += int(stats.get("bytes") or 0)
    return {
        "asset_directories": dirs,
        "files": files,
        "bytes": nbytes,
        "errors": errors,
    }


def _backup_snapshot(bucket: Path) -> dict[str, Any]:
    """Contract snapshot. Folder name is a storage locator, not identity."""
    files, nbytes = _dir_file_bytes(bucket)
    meta = bucket / BACKUP_METADATA_NAME
    payload = _load_metadata(meta) if meta.is_file() else {}
    cover = _cover_file(bucket)
    index = _index_file(bucket)
    iid = str(payload.get("internal_id") or "").strip()
    return {
        "id": bucket.name,
        "bytes": nbytes,
        "files": files,
        "metadata_exists": meta.is_file(),
        "meta_sha": _sha256_file(meta) if meta.is_file() else "",
        "internal_id": iid,
        "workspace_id": str(payload.get("workspace_id") or "").strip(),
        "published_file_id": str(payload.get("published_file_id") or "").strip(),
        "title": str(payload.get("title") or payload.get("display_name") or "").strip(),
        "cover_sha": _sha256_file(cover) if cover is not None else "",
        "has_cover": cover is not None,
        "index_sha": _sha256_file(index) if index is not None else "",
        "has_index": index is not None,
    }


def _live_contract(bucket: Path) -> dict[str, Any] | None:
    snap = _backup_snapshot(bucket)
    if not snap["metadata_exists"] or not snap["meta_sha"]:
        return None
    return snap


def _content_not_unique_to_orphan(orphan: dict[str, Any], live: dict[str, Any]) -> bool:
    """True when orphan cover/index do not hold recovery bytes live lacks."""
    if orphan["has_cover"] and not live["has_cover"]:
        return False
    if orphan["has_cover"] and live["cover_sha"] != orphan["cover_sha"]:
        return False
    if orphan["has_index"] and not live["has_index"]:
        return False
    if (
        orphan["has_index"]
        and live["has_index"]
        and live["index_sha"] != orphan["index_sha"]
    ):
        return False
    return True


def classify_orphan_backups(
    backup_root: Path,
    live_ids: Iterable[str],
) -> dict[str, Any]:
    """
    Conservative pairing.

    SAFE DELETE requires ALL of:
    - non-empty Frozen ``internal_id`` equal on orphan and live
    - exactly one live backup with that ``internal_id``
    - live ``metadata.json`` exists (complete counterpart)
    - orphan cover/index are not unique recovery vs that live
    Folder name, title, and workspace_id are never sufficient alone.
    Different ``internal_id`` is always KEEP.
    """
    live_set = {str(i).strip() for i in live_ids if str(i).strip().isdigit()}
    live_by_internal: dict[str, list[dict[str, Any]]] = {}
    for mid in live_set:
        bucket = backup_root / mid
        if not bucket.is_dir():
            continue
        contract = _live_contract(bucket)
        if contract is None:
            continue
        iid = str(contract.get("internal_id") or "")
        if iid:
            live_by_internal.setdefault(iid, []).append(contract)

    safe: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    children = (
        sorted(backup_root.iterdir(), key=lambda p: p.name) if backup_root.is_dir() else []
    )
    for child in children:
        if not child.is_dir() or not child.name.isdigit():
            continue
        if child.name in live_set:
            continue
        snap = _backup_snapshot(child)
        if not snap["metadata_exists"] or not snap["meta_sha"]:
            retained.append(
                {
                    **snap,
                    "reason": "missing_or_unreadable_metadata",
                    "category": "recovery_valuable",
                }
            )
            continue
        oid = str(snap.get("internal_id") or "")
        if not oid:
            retained.append(
                {
                    **snap,
                    "reason": "missing_internal_id",
                    "category": "recovery_valuable",
                }
            )
            continue
        by_id = live_by_internal.get(oid) or []
        if not by_id:
            retained.append(
                {
                    **snap,
                    "reason": "no_live_internal_id_match",
                    "category": "uncertain",
                }
            )
            continue
        if len(by_id) != 1:
            retained.append(
                {
                    **snap,
                    "reason": "live_internal_id_not_unique",
                    "category": "uncertain",
                    "live_ids": [row["id"] for row in by_id],
                }
            )
            continue
        live = by_id[0]
        lid = str(live.get("internal_id") or "")
        if oid != lid:
            retained.append(
                {
                    **snap,
                    "reason": "different_internal_id",
                    "category": "identity_divergent",
                    "live_id": live["id"],
                    "live_internal_id": lid,
                }
            )
            continue
        if not _content_not_unique_to_orphan(snap, live):
            retained.append(
                {
                    **snap,
                    "reason": "orphan_has_unique_cover_or_index",
                    "category": "recovery_valuable",
                    "live_id": live["id"],
                }
            )
            continue
        identity_evidence = "frozen_internal_id_exact_unique_live"
        content_bits = ["live_metadata.json"]
        if snap["has_cover"]:
            content_bits.append("cover_sha_equal")
        if snap["has_index"]:
            content_bits.append("index_sha_equal")
        if snap["meta_sha"] and snap["meta_sha"] == live["meta_sha"]:
            content_bits.append("metadata_sha_equal")
        why = (
            f"same Frozen internal_id={oid}; unique live backup {live['id']}; "
            "orphan cover/index are not unique recovery"
        )
        safe.append(
            {
                **snap,
                "live_id": live["id"],
                "live_internal_id": lid,
                "reason": "duplicate_of_live",
                "category": "safe_duplicate",
                "why_safe": why,
                "identity_evidence": identity_evidence,
                "content_evidence": ",".join(content_bits),
            }
        )
    return {
        "safe_delete": safe,
        "retain": retained,
        "orphan_before": len(safe) + len(retained),
        "safe_bytes": sum(int(row.get("bytes") or 0) for row in safe),
        "retain_bytes": sum(int(row.get("bytes") or 0) for row in retained),
        "recovery_valuable": sum(
            1 for row in retained if row.get("category") == "recovery_valuable"
        ),
        "uncertain": sum(1 for row in retained if row.get("category") == "uncertain"),
        "identity_divergent": sum(
            1 for row in retained if row.get("category") == "identity_divergent"
        ),
    }


def delete_orphan_backups(
    backup_root: Path,
    orphan_ids: Iterable[str],
    live_ids: Iterable[str],
    live_pairs: dict[str, str] | None = None,
) -> dict[str, int]:
    """Delete named orphan buckets. Refuses any id that is a live mod_id.

    If *live_pairs* maps orphan id → live id, also refuse when that live
    counterpart is missing ``metadata.json``.
    """
    live_set = {str(i).strip() for i in live_ids if str(i).strip().isdigit()}
    pairs = {str(k).strip(): str(v).strip() for k, v in (live_pairs or {}).items()}
    deleted = 0
    bytes_removed = 0
    files_removed = 0
    errors = 0
    skipped_live = 0
    skipped_incomplete = 0
    for raw in orphan_ids:
        mid = str(raw or "").strip()
        if not mid.isdigit():
            continue
        if mid in live_set:
            skipped_live += 1
            continue
        live_id = pairs.get(mid, "")
        if live_id:
            live_meta = backup_root / live_id / BACKUP_METADATA_NAME
            if live_id not in live_set or not live_meta.is_file():
                skipped_incomplete += 1
                continue
        bucket = backup_root / mid
        if not bucket.is_dir():
            continue
        fcount, fbytes = _dir_file_bytes(bucket)
        try:
            shutil.rmtree(bucket)
        except OSError as exc:
            logger.warning("failed to delete orphan backup %s: %s", bucket, exc)
            errors += 1
            continue
        deleted += 1
        files_removed += fcount
        bytes_removed += fbytes
    return {
        "deleted": deleted,
        "files": files_removed,
        "bytes": bytes_removed,
        "errors": errors,
        "skipped_live": skipped_live,
        "skipped_incomplete": skipped_incomplete,
    }


def delete_empty_quarantine_dirs(quarantine_root: Path) -> dict[str, Any]:
    """Delete timestamp dirs that contain zero files. Non-empty trees are kept."""
    deleted: list[str] = []
    retained: list[str] = []
    if not quarantine_root.is_dir():
        return {"deleted": deleted, "retained": retained}
    for child in list(quarantine_root.iterdir()):
        if not child.is_dir():
            retained.append(child.name)
            continue
        files, _nbytes = _dir_file_bytes(child)
        if files > 0:
            retained.append(child.name)
            continue
        try:
            shutil.rmtree(child)
        except OSError as exc:
            logger.warning("failed to delete empty quarantine %s: %s", child, exc)
            retained.append(child.name)
            continue
        deleted.append(child.name)
    return {"deleted": deleted, "retained": retained}


def _is_test_pak_tree(path: Path) -> bool:
    return (path / "ModName" / "test.pak").is_file() or (
        path / "ModName" / "Optional" / "hat.pak"
    ).is_file()


def classify_import_cache_leftovers(import_cache: Path) -> dict[str, Any]:
    """Name leftover import_cache children. Never selects `_modio_live_verify`."""
    safe: list[dict[str, Any]] = []
    retain: list[dict[str, Any]] = []
    if not import_cache.is_dir():
        return {"safe_delete": safe, "retain": retain}
    rar_groups: dict[tuple[str, int], list[Path]] = {}
    children = [p for p in import_cache.iterdir() if p.is_dir() or p.is_file()]
    for child in children:
        name = child.name
        files, nbytes = _dir_file_bytes(child) if child.is_dir() else (
            (1, int(child.stat().st_size)) if child.is_file() else (0, 0)
        )
        if name == "_modio_live_verify":
            retain.append({"name": name, "reason": "not_confirmed_leftover", "bytes": nbytes})
            continue
        if name.startswith("_trace_") or name.startswith("deploy_"):
            safe.append({"name": name, "reason": "trace_or_deploy_leftover", "bytes": nbytes})
            continue
        if child.is_dir() and _is_test_pak_tree(child):
            safe.append({"name": name, "reason": "test_pak_tree", "bytes": nbytes})
            continue
        if child.is_dir() and nbytes <= 16:
            safe.append({"name": name, "reason": "tiny_uuid_stub", "bytes": nbytes})
            continue
        rar_files = [
            p
            for p in child.rglob("*")
            if p.is_file() and p.suffix.lower() == ".rar"
        ] if child.is_dir() else []
        if len(rar_files) == 1:
            rar = rar_files[0]
            try:
                key = (rar.name, int(rar.stat().st_size))
            except OSError:
                key = (rar.name, -1)
            rar_groups.setdefault(key, []).append(child)
            continue
        retain.append({"name": name, "reason": "uncertain_staging", "bytes": nbytes})

    for key, group in rar_groups.items():
        if len(group) >= 2:
            for child in group:
                files, nbytes = _dir_file_bytes(child)
                safe.append(
                    {
                        "name": child.name,
                        "reason": f"duplicate_rar:{key[0]}:{key[1]}",
                        "bytes": nbytes,
                    }
                )
        else:
            child = group[0]
            files, nbytes = _dir_file_bytes(child)
            retain.append(
                {
                    "name": child.name,
                    "reason": "unique_rar_not_confirmed_duplicate",
                    "bytes": nbytes,
                }
            )
    return {"safe_delete": safe, "retain": retain}


def delete_import_cache_leftovers(
    import_cache: Path,
    names: Iterable[str],
) -> dict[str, int]:
    deleted = 0
    bytes_removed = 0
    errors = 0
    skipped = 0
    protected = {"_modio_live_verify"}
    for raw in names:
        name = str(raw or "").strip()
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            skipped += 1
            continue
        if name in protected:
            skipped += 1
            continue
        target = import_cache / name
        try:
            target.resolve().relative_to(import_cache.resolve())
        except (OSError, ValueError):
            skipped += 1
            continue
        if not target.exists():
            continue
        _files, nbytes = _dir_file_bytes(target) if target.is_dir() else (
            (1, int(target.stat().st_size)) if target.is_file() else (0, 0)
        )
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            logger.warning("failed to delete import_cache leftover %s: %s", target, exc)
            errors += 1
            continue
        deleted += 1
        bytes_removed += nbytes
    return {
        "deleted": deleted,
        "bytes": bytes_removed,
        "errors": errors,
        "skipped": skipped,
    }
