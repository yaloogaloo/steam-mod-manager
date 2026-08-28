"""
Deployment Domain — deploy source validation and deploy-time integrity.

Library refresh / metadata sync must use :mod:`services.local_file_index`
instead of this module.
"""

from __future__ import annotations

import hashlib
import logging
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.db_manager import DatabaseManager, get_db
from core.mod_platform import (
    ModFileEntry,
    ModFilesBundle,
    is_entry_selected_for_deploy,
)
from services.deploy_errors import DeploySourceError
from services.file_ops import (
    INFO_DIR_NAME,
    LEGACY_INFO_DIR_NAME,
    read_info_metadata_dict,
    _zip_has_payload,
)
from services.importers.archive import is_archive_path
from services.importers.image_scanner import IMAGE_SUFFIXES
from services.importers.local_scanner import ARCHIVE_SUFFIXES
from services.importers.source_files import META_ARCHIVE_NAME

logger = logging.getLogger(__name__)

from services.local_file_index import (
    META_CONTENT_HASH,
    LocalReconcileResult,
    ReplacementCandidate,
    reconcile_local_files,
)

HISTORY_VERSION_DIR = "历史版本"

FORBIDDEN_SOURCE_PARTS = frozenset(
    {
        INFO_DIR_NAME,
        LEGACY_INFO_DIR_NAME,
        HISTORY_VERSION_DIR,
        "backups",
        "import_cache",
        ".cache",
        "cache",
    }
)

_README_NAMES = frozenset(
    {
        "readme",
        "readme.txt",
        "readme.md",
        "read_me.txt",
        "license",
        "license.txt",
        "changelog",
        "changelog.txt",
    }
)

__all__ = [
    "META_CONTENT_HASH",
    "ReplacementCandidate",
    "ReconcileResult",
    "ResolvedDeploySource",
    "SourceValidationResult",
    "enrich_manifest_source_hashes",
    "has_deployable_source",
    "reconcile_source",
    "resolve_deploy_source",
    "validate_content_root",
    "validate_source",
]

ReconcileResult = LocalReconcileResult


@dataclass
class SourceValidationResult:
    ok: bool = True
    missing_files: list[str] = field(default_factory=list)
    source_changed: list[str] = field(default_factory=list)
    replacement_candidates: list[ReplacementCandidate] = field(default_factory=list)
    resolved: ResolvedDeploySource | None = None


@dataclass
class ResolvedDeploySource:
    managed_path: Path
    archive_paths: list[Path] = field(default_factory=list)
    loose_files: list[Path] = field(default_factory=list)
    entries: list[ModFileEntry] = field(default_factory=list)


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
    if rel_parts[0] in FORBIDDEN_SOURCE_PARTS:
        return True
    return any(part in FORBIDDEN_SOURCE_PARTS for part in rel_parts)


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


def _is_deployable_member_name(name: str) -> bool:
    base = Path(name.replace("\\", "/").split("/")[-1]).name
    lower = base.casefold()
    if lower in _README_NAMES:
        return False
    if Path(base).suffix.lower() in IMAGE_SUFFIXES:
        return False
    if lower.startswith("readme") or lower.startswith("changelog"):
        return False
    return True


def validate_archive_content(path: Path) -> None:
    """
    Ensure an archive contains deployable payload (not empty / readme-only).

    Raises :class:`DeploySourceError` with structured ``code``.
    """
    if not path.is_file():
        raise DeploySourceError(
            f"压缩包不存在：{path}",
            code="archive_missing",
        )
    suffix = path.suffix.lower()
    if suffix == ".zip":
        if not _zip_has_payload(path):
            raise DeploySourceError(
                f"空压缩包：{path.name}",
                code="empty_archive",
            )
        try:
            with zipfile.ZipFile(path, "r") as zf:
                members = [
                    n.replace("\\", "/")
                    for n in zf.namelist()
                    if n and not str(n).endswith("/")
                ]
        except (OSError, zipfile.BadZipFile) as exc:
            raise DeploySourceError(
                f"无效压缩包：{path.name}（{exc}）",
                code="invalid_archive",
            ) from exc
        deployable = [m for m in members if _is_deployable_member_name(m)]
        if not deployable:
            raise DeploySourceError(
                f"压缩包无可部署内容（仅 README/图片）：{path.name}",
                code="no_deployable_payload",
            )
        return
    # Other archive types: require non-zero size (deep inspect deferred).
    try:
        if path.stat().st_size <= 0:
            raise DeploySourceError(
                f"空压缩包：{path.name}",
                code="empty_archive",
            )
    except OSError as exc:
        raise DeploySourceError(
            f"无法读取压缩包：{path.name}（{exc}）",
            code="invalid_archive",
        ) from exc


def _archive_is_deployable(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        validate_archive_content(path)
    except DeploySourceError:
        return False
    return True


def _resolve_entry_path(managed: Path, entry: ModFileEntry) -> Path | None:
    root = Path(managed)
    for name in _entry_path_names(entry):
        candidate = root / name
        if _is_archive_entry(entry):
            if _archive_is_deployable(candidate):
                return candidate
            continue
        if candidate.is_file() and not _path_forbidden(
            candidate.relative_to(root).parts
        ):
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
            if _archive_is_deployable(child):
                out.append(child)
    except OSError:
        return []
    return out


def _iter_loose_payload_files(managed: Path) -> list[Path]:
    root = Path(managed)
    if not root.is_dir():
        return []
    files: list[Path] = []
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
            files.append(path)
    except OSError:
        return []
    return files


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
            sync_reason="source_integrity",
        )
    except Exception:  # noqa: BLE001
        logger.debug("sidecar sync failed mod_id=%s", mod_id, exc_info=True)


def reconcile_source(
    mod_id: int | str,
    *,
    managed_path: Path | str | None = None,
    db: DatabaseManager | None = None,
    persist: bool = True,
) -> ReconcileResult:
    """Deploy-domain alias for :func:`local_file_index.reconcile_local_files`."""
    return reconcile_local_files(
        mod_id,
        managed_path=managed_path,
        db=db,
        persist=persist,
    )


def resolve_deploy_source(
    mod_id: int | str,
    *,
    managed_path: Path | str | None = None,
    db: DatabaseManager | None = None,
) -> ResolvedDeploySource:
    """
    Resolve deploy sources for *mod_id* without auto-upgrading versions.

    Priority: user-selected entries only; among multiple selected archives pick
    newest ``mtime`` (never largest file size).
    """
    database = db if db is not None else get_db()
    root = _resolve_managed_path(mod_id, managed_path=managed_path, db=database)
    if root is None:
        raise DeploySourceError("managed Mod 目录不存在", code="managed_missing")

    bundle = database.get_mod_files(mod_id)
    loose = _iter_loose_payload_files(root)
    archive_entries = [e for e in bundle.files if _is_archive_entry(e)]
    selected_archives = [e for e in archive_entries if is_entry_selected_for_deploy(e)]

    resolved_archives: list[tuple[ModFileEntry, Path]] = []
    for entry in selected_archives:
        path = _resolve_entry_path(root, entry)
        if path is not None:
            resolved_archives.append((entry, path))

    if not resolved_archives and not selected_archives:
        root_archives = _list_root_archives(root)
        return ResolvedDeploySource(
            managed_path=root,
            archive_paths=root_archives,
            loose_files=loose,
            entries=[],
        )

    resolved_archives.sort(
        key=lambda pair: pair[1].stat().st_mtime if pair[1].is_file() else 0,
        reverse=True,
    )

    if not archive_entries and not bundle.files:
        root_archives = _list_root_archives(root)
        return ResolvedDeploySource(
            managed_path=root,
            archive_paths=root_archives,
            loose_files=loose,
            entries=[],
        )

    return ResolvedDeploySource(
        managed_path=root,
        archive_paths=[p for _, p in resolved_archives],
        loose_files=loose,
        entries=[e for e, _ in resolved_archives],
    )


def has_deployable_source(
    managed_path: str | Path,
    *,
    mod_id: int | str | None = None,
    db: DatabaseManager | None = None,
) -> bool:
    """True when managed folder has DB-resolvable deploy payload."""
    root = Path(managed_path).expanduser()
    if not root.is_dir():
        return False
    if _iter_loose_payload_files(root):
        return True

    database = db if db is not None else get_db()
    mid = str(mod_id or "").strip()
    if not mid:
        data = read_info_metadata_dict(root) or {}
        mid = str(data.get("published_file_id") or "").strip()

    try:
        resolved = resolve_deploy_source(mid, managed_path=root, db=database)
    except DeploySourceError:
        return False
    if resolved.loose_files:
        return True
    if resolved.archive_paths:
        return True
    if not database.get_mod_files(mid).files:
        return bool(_list_root_archives(root))
    return False


def validate_source(
    mod_id: int | str,
    *,
    managed_path: Path | str | None = None,
    db: DatabaseManager | None = None,
    auto_reconcile: bool = True,
) -> SourceValidationResult:
    """
    Validate managed Mod source integrity before deploy.

    Raises :class:`DeploySourceError` when replacement is required or payload
    is missing/invalid.
    """
    database = db if db is not None else get_db()
    root = _resolve_managed_path(mod_id, managed_path=managed_path, db=database)
    if root is None:
        raise DeploySourceError(
            "managed Mod 目录不存在",
            code="managed_missing",
        )

    bundle = database.get_mod_files(mod_id)
    source_changed: list[str] = []
    for entry in bundle.files:
        if not is_entry_selected_for_deploy(entry):
            continue
        path = _resolve_entry_path(root, entry)
        rel = str(entry.path or entry.filename or "").strip()
        if path is None or not rel:
            continue
        meta = entry.metadata if isinstance(entry.metadata, dict) else {}
        stored = str(meta.get(META_CONTENT_HASH) or "").strip()
        if not stored:
            continue
        try:
            live = _sha256_file(path)
        except OSError:
            continue
        if live and stored != live:
            source_changed.append(rel)

    recon = (
        reconcile_source(mod_id, managed_path=root, db=database, persist=auto_reconcile)
        if auto_reconcile
        else ReconcileResult()
    )

    result = SourceValidationResult(
        missing_files=list(recon.missing_files),
        replacement_candidates=list(recon.replacement_candidates),
        source_changed=list(source_changed),
    )

    if recon.replacement_candidates:
        raise DeploySourceError(
            "Mod 源文件版本不一致，需要刷新或手动选择版本",
            code="replacement_required",
            replacement_candidates=[
                {
                    "entry_id": c.entry_id,
                    "db_path": c.db_path,
                    "candidate_path": c.candidate_path,
                    "reference_hash": c.reference_hash,
                    "candidate_hash": c.candidate_hash,
                    "reason": c.reason,
                }
                for c in recon.replacement_candidates
            ],
        )

    if not has_deployable_source(root, mod_id=mod_id, db=database):
        raise DeploySourceError(
            "Mod 没有可部署的内容",
            code="no_deployable_source",
            missing_files=result.missing_files,
        )

    resolved = resolve_deploy_source(mod_id, managed_path=root, db=database)
    result.resolved = resolved

    bundle = database.get_mod_files(mod_id)
    for entry in bundle.files:
        if not is_entry_selected_for_deploy(entry):
            continue
        path = _resolve_entry_path(root, entry)
        rel = str(entry.path or entry.filename or "").strip()
        if path is None:
            if rel and rel not in result.missing_files:
                result.missing_files.append(rel)
            continue
        if _is_archive_entry(entry):
            validate_archive_content(path)

    loose_payload = bool(_iter_loose_payload_files(root))
    if result.missing_files and loose_payload:
        result.missing_files = [
            m
            for m in result.missing_files
            if Path(m).suffix.lower() not in ARCHIVE_SUFFIXES
        ]

    if result.missing_files:
        raise DeploySourceError(
            "Mod 源文件缺失",
            code="missing_files",
            missing_files=result.missing_files,
        )

    if auto_reconcile and source_changed:
        for entry in bundle.files:
            if not is_entry_selected_for_deploy(entry):
                continue
            rel = str(entry.path or entry.filename or "").strip()
            if rel not in source_changed:
                continue
            path = _resolve_entry_path(root, entry)
            if path is not None:
                _touch_content_hash(entry, path)
        database.set_mod_files(mod_id, bundle)
        _sync_sidecar(mod_id, root, database)

    return result


def validate_content_root(
    content_root: Path | str,
    *,
    managed_path: Path | str | None = None,
    allowed_rel_paths: frozenset[str] | None = None,
) -> None:
    """
    Post-extract deploy gate: *content_root* must contain loose deploy payload.

    Raises :class:`DeploySourceError` when only archives/metadata remain.
    """
    del allowed_rel_paths
    root = Path(content_root)
    if not root.exists():
        raise DeploySourceError(f"部署源不存在：{root}", code="content_missing")
    if not root.is_dir():
        raise DeploySourceError(f"部署源不是目录：{root}", code="content_invalid")

    if _iter_loose_payload_files(root):
        return

    managed = Path(managed_path) if managed_path is not None else None
    hint = ""
    if managed is not None:
        try:
            same = managed.exists() and managed.resolve() == root.resolve()
        except OSError:
            same = False
        if same:
            archives = [p.name for p in _list_root_archives(root)]
            if archives:
                hint = f"（仅有压缩包：{', '.join(archives[:3])}）"
        else:
            hint = f"（managed={managed}）"
    raise DeploySourceError(
        "部署源没有可部署的内容：请确认压缩包已解压，"
        f"或 managed 目录已包含合法 Mod 文件{hint}",
        code="no_deployable_payload",
    )


def enrich_manifest_source_hashes(manifest: Any) -> None:
    """Attach optional ``source_hash`` to manifest file entries (backward compatible)."""
    for entry in list(getattr(manifest, "files", None) or []):
        raw = str(getattr(entry, "source", "") or "").strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_file():
            continue
        try:
            digest = _sha256_file(path)
        except OSError:
            continue
        if hasattr(entry, "source_hash"):
            entry.source_hash = digest


# Backward-compatible aliases
reconcile_archive_source = reconcile_source


def validate_mod_files(
    mod_id: int | str,
    *,
    managed_path: Path | str | None = None,
    db: DatabaseManager | None = None,
) -> list[str]:
    from services.local_file_index import validate_mod_files as _validate_local

    return _validate_local(mod_id, managed_path=managed_path, db=db)
