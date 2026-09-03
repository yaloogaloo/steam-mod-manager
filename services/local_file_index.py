"""
Local File Domain — disk scan, hash, and mod_files path reconcile.

Library refresh and library reconcile use this module only. It must never
apply deployment legality rules (archive payload inspection, deploy gates).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from core.db_manager import DatabaseManager, get_db
from core.mod_platform import ModFileEntry, ModFilesBundle
from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, read_info_metadata_dict
from services.importers.archive import is_archive_path
from services.importers.local_scanner import ARCHIVE_SUFFIXES
from services.importers.source_files import META_ARCHIVE_NAME

logger = logging.getLogger(__name__)

META_CONTENT_HASH = "content_hash"
HISTORY_VERSION_DIR = "历史版本"
BACKUPS_DIRNAME = "backups"
IMPORT_CACHE_DIRNAME = "import_cache"

SKIP_INDEX_PARTS = frozenset(
    {
        INFO_DIR_NAME,
        LEGACY_INFO_DIR_NAME,
        HISTORY_VERSION_DIR,
        BACKUPS_DIRNAME,
        IMPORT_CACHE_DIRNAME,
        ".cache",
        "cache",
    }
)

__all__ = [
    "META_CONTENT_HASH",
    "ReplacementCandidate",
    "LocalReconcileResult",
    "has_local_mod_payload",
    "reconcile_local_files",
    "validate_mod_files",
]


@dataclass
class ReplacementCandidate:
    entry_id: str
    db_path: str
    candidate_path: str
    reference_hash: str
    candidate_hash: str
    reason: str = "hash_mismatch"


@dataclass
class LocalReconcileResult:
    missing_files: list[str] = field(default_factory=list)
    auto_fixed: list[str] = field(default_factory=list)
    replacement_candidates: list[ReplacementCandidate] = field(default_factory=list)
    updated: bool = False


def _sha256_file(path: Path, *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _path_forbidden(rel_parts: tuple[str, ...]) -> bool:
    if not rel_parts:
        return True
    if rel_parts[0] in SKIP_INDEX_PARTS:
        return True
    return any(part in SKIP_INDEX_PARTS for part in rel_parts)


def _resolve_managed_path(
    mod_id: int | str,
    *,
    managed_path: Path | str | None = None,
    db: DatabaseManager | None = None,
) -> Path | None:
    if managed_path is not None:
        root = Path(managed_path).expanduser()
        if root.is_dir():
            return root
    database = db if db is not None else get_db()
    mid = str(mod_id).strip()
    if mid.isdigit():
        row = database.get_mod_backup_row(mid) or {}
        lkp = str(row.get("last_known_path") or "").strip()
        if lkp and Path(lkp).is_dir():
            return Path(lkp)
        info = database.get_mod_display_info(mid)
        if info:
            for raw in (getattr(info, "managed_path", None), getattr(info, "local_path", None)):
                text = str(raw or "").strip()
                if text and Path(text).is_dir():
                    return Path(text)
    return None


def _entry_path_names(entry: ModFileEntry) -> list[str]:
    names: list[str] = []
    meta = entry.metadata if isinstance(entry.metadata, dict) else {}
    for raw in (entry.path, entry.filename, meta.get(META_ARCHIVE_NAME)):
        text = str(raw or "").replace("\\", "/").strip().lstrip("./")
        if text and text not in names:
            names.append(text)
    return names


def _is_archive_entry(entry: ModFileEntry) -> bool:
    meta = entry.metadata if isinstance(entry.metadata, dict) else {}
    if str(meta.get(META_ARCHIVE_NAME) or "").strip():
        return True
    return any(is_archive_path(name) for name in _entry_path_names(entry))


def _archive_suffix(entry: ModFileEntry) -> str:
    for name in _entry_path_names(entry):
        suffix = Path(name).suffix.lower()
        if suffix in ARCHIVE_SUFFIXES:
            return suffix
    return ".zip"


def _resolve_entry_path(managed: Path, entry: ModFileEntry) -> Path | None:
    """Resolve indexed path when the file exists (no deploy legality checks)."""
    root = Path(managed)
    for name in _entry_path_names(entry):
        candidate = root / name
        if candidate.is_file() and not _path_forbidden(candidate.relative_to(root).parts):
            return candidate
        by_name = root / Path(name).name
        if by_name.is_file() and not _path_forbidden(by_name.relative_to(root).parts):
            return by_name
    return None


def _list_root_archives(managed: Path, *, suffix: str = "") -> list[Path]:
    root = Path(managed)
    if not root.is_dir():
        return []
    want = suffix.lower() if suffix else ""
    out: list[Path] = []
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not child.is_file():
                continue
            if child.name.startswith("."):
                continue
            if not is_archive_path(child):
                continue
            if want and child.suffix.lower() != want:
                continue
            out.append(child)
    except OSError:
        return []
    return out


def _iter_loose_files(managed: Path) -> Iterator[Path]:
    """Yield loose (non-archive) payload files under *managed*.

    Lazy: callers may stop after the first hit. Filter rules match the
    previous list-building implementation (skip ``SKIP_INDEX_PARTS`` and
    archive filenames).
    """
    root = Path(managed)
    if not root.is_dir():
        return
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                parts = path.relative_to(root).parts
            except ValueError:
                continue
            if _path_forbidden(parts):
                continue
            if is_archive_path(path.name):
                continue
            yield path
    except OSError:
        return


def _reference_hash(managed: Path, entry: ModFileEntry) -> str:
    meta = entry.metadata if isinstance(entry.metadata, dict) else {}
    stored = str(meta.get(META_CONTENT_HASH) or "").strip()
    if stored:
        return stored
    resolved = _resolve_entry_path(managed, entry)
    if resolved is not None and resolved.is_file():
        try:
            return _sha256_file(resolved)
        except OSError:
            pass
    basename = Path(str(entry.filename or entry.path or "")).name
    if basename:
        hist = managed / HISTORY_VERSION_DIR / basename
        if hist.is_file():
            try:
                return _sha256_file(hist)
            except OSError:
                pass
    return ""


def _touch_content_hash(entry: ModFileEntry, path: Path) -> None:
    if not isinstance(entry.metadata, dict):
        entry.metadata = {}
    try:
        entry.metadata[META_CONTENT_HASH] = _sha256_file(path)
    except OSError:
        pass


def _apply_path_update(entry: ModFileEntry, new_path: Path, managed: Path) -> None:
    rel = new_path.relative_to(managed).as_posix()
    entry.path = rel
    entry.filename = new_path.name
    if not isinstance(entry.metadata, dict):
        entry.metadata = {}
    entry.metadata[META_ARCHIVE_NAME] = new_path.name
    _touch_content_hash(entry, new_path)


def _sync_sidecar(mod_id: int | str, managed: Path, db: DatabaseManager) -> None:
    try:
        from services.info_sidecar import write_sidecar_for_mod

        write_sidecar_for_mod(
            managed,
            mod_id,
            db=db,
            sync_backup=True,
            sync_reason="local_file_reconcile",
        )
    except Exception:  # noqa: BLE001
        logger.debug("sidecar sync failed mod_id=%s", mod_id, exc_info=True)


def _report_missing_for_entry(entry: ModFileEntry) -> bool:
    """Local index missing-file reports skip explicitly unselected DB rows."""
    sel = getattr(entry, "selected_for_deploy", None)
    if sel is False:
        return False
    return bool(str(entry.path or entry.filename or "").strip())


def _reconcile_archive_entry(entry: ModFileEntry) -> bool:
    """Archive path/hash reconcile applies to indexed rows (not deploy legality)."""
    sel = getattr(entry, "selected_for_deploy", None)
    if sel is False:
        return False
    return _is_archive_entry(entry)


def reconcile_local_files(
    mod_id: int | str,
    *,
    managed_path: Path | str | None = None,
    db: DatabaseManager | None = None,
    persist: bool = True,
) -> LocalReconcileResult:
    """
    Align ``mod_files`` paths with disk via hash match (Local File Domain).

    Never inspects archive deployability — only existence, hash, and path.
    """
    database = db if db is not None else get_db()
    root = _resolve_managed_path(mod_id, managed_path=managed_path, db=database)
    result = LocalReconcileResult()
    if root is None:
        return result

    bundle = database.get_mod_files(mod_id)
    if not bundle.files:
        return result

    changed = False
    hash_fill = False
    for entry in bundle.files:
        if not _is_archive_entry(entry):
            resolved = _resolve_entry_path(root, entry)
            if resolved is not None:
                meta = entry.metadata if isinstance(entry.metadata, dict) else {}
                if not str(meta.get(META_CONTENT_HASH) or "").strip():
                    _touch_content_hash(entry, resolved)
                    hash_fill = True
            elif _report_missing_for_entry(entry):
                result.missing_files.append(str(entry.path or entry.filename))
            continue

        resolved = _resolve_entry_path(root, entry)
        db_path = str(entry.path or entry.filename or "").strip()
        if resolved is not None:
            meta = entry.metadata if isinstance(entry.metadata, dict) else {}
            if not str(meta.get(META_CONTENT_HASH) or "").strip():
                _touch_content_hash(entry, resolved)
                hash_fill = True
            continue

        if not _reconcile_archive_entry(entry):
            continue

        suffix = _archive_suffix(entry)
        candidates = _list_root_archives(root, suffix=suffix)
        ref_hash = _reference_hash(root, entry)
        matched: Path | None = None
        for candidate in candidates:
            try:
                cand_hash = _sha256_file(candidate)
            except OSError:
                continue
            if ref_hash and cand_hash == ref_hash:
                matched = candidate
                break

        if matched is not None:
            _apply_path_update(entry, matched, root)
            result.auto_fixed.append(matched.relative_to(root).as_posix())
            changed = True
            logger.info(
                "[local_file] mod_id=%s auto-fixed %s -> %s",
                mod_id,
                db_path,
                matched.name,
            )
            continue

        if db_path:
            result.missing_files.append(db_path)

        for candidate in candidates:
            try:
                cand_hash = _sha256_file(candidate)
            except OSError:
                cand_hash = ""
            result.replacement_candidates.append(
                ReplacementCandidate(
                    entry_id=str(entry.id),
                    db_path=db_path,
                    candidate_path=candidate.relative_to(root).as_posix(),
                    reference_hash=ref_hash,
                    candidate_hash=cand_hash,
                    reason="hash_mismatch" if ref_hash else "missing_db_path",
                )
            )

    if persist and (changed or hash_fill):
        database.set_mod_files(mod_id, bundle)
        if changed:
            _sync_sidecar(mod_id, root, database)
        result.updated = changed
    elif changed:
        result.updated = True

    return result


def has_local_mod_payload(
    managed_path: str | Path,
    *,
    mod_id: int | str | None = None,
    db: DatabaseManager | None = None,
) -> bool:
    """
    True when the managed folder has indexed/local payload on disk.

    Presence only — empty/invalid archives still count as payload for library
    ``content_status``. Deployment legality is enforced only at deploy time.
    """
    root = Path(managed_path).expanduser()
    if not root.is_dir():
        return False
    t0 = time.perf_counter()
    seen = 0
    hit = False
    try:
        for _path in _iter_loose_files(root):
            seen += 1
            hit = True
            break
    finally:
        try:
            from services.reconcile_observability import add_payload_walk

            add_payload_walk(
                files=seen,
                ms=(time.perf_counter() - t0) * 1000.0,
            )
        except Exception:  # noqa: BLE001
            pass
    if hit:
        return True
    if _list_root_archives(root):
        return True

    database = db if db is not None else get_db()
    mid = str(mod_id or "").strip()
    if not mid:
        data = read_info_metadata_dict(root) or {}
        mid = str(data.get("published_file_id") or "").strip()

    if mid and mid.isdigit():
        for entry in database.get_mod_files(mid).files:
            if _resolve_entry_path(root, entry) is not None:
                return True
    return False


def validate_mod_files(
    mod_id: int | str,
    *,
    managed_path: Path | str | None = None,
    db: DatabaseManager | None = None,
) -> list[str]:
    database = db if db is not None else get_db()
    root = _resolve_managed_path(mod_id, managed_path=managed_path, db=database)
    if root is None:
        bundle = database.get_mod_files(mod_id)
        return [
            str(e.path or e.filename or e.id)
            for e in bundle.files
            if str(e.path or e.filename or "").strip()
        ]
    missing: list[str] = []
    for entry in database.get_mod_files(mod_id).files:
        rel = str(entry.path or entry.filename or "").strip()
        if not rel:
            continue
        if _resolve_entry_path(root, entry) is None:
            missing.append(rel)
    return missing
