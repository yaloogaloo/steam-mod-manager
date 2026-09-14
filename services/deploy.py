"""Deploy managed Mods into a game's configured paths (strategy-based)."""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
    GameDeployConfig,
    get_db,
)
from core.paths import default_mod_library
from services.backup_manager import (
    BackupIntegrityError,
    BackupManager,
    BackupRestoreError,
)
from services.deployment_lifecycle import (
    DeploymentLifecycleState,
    PHASE_COPY_DONE,
    PHASE_MANIFEST_DONE,
    persist_lifecycle_transaction,
    resolve_from_transaction,
)
from services.conflict import ConflictDetector
from services.deploy_errors import DeploySourceError, DeployValidationError
from services.deploy_paths import (
    DeployPathError,
    attach_canonical_targets,
    planned_absolute_targets,
    remap_manifest_targets,
    resolve_deploy_identity,
    resolve_deploy_managed_path,
)
from services.deploy_security import (
    ManifestSecurityError,
    collect_allowed_target_roots,
    collect_protected_roots,
    validate_manifest_for_save,
    validate_manifest_mod_id,
    validate_manifest_targets,
    validate_planned_sources,
)
from services.deploy_rules import (
    DEPLOY_TYPE_ANNO_1800,
    DEPLOY_TYPE_CUSTOM_PATH,
    DEPLOY_TYPE_FOLDER_COPY,
    DEPLOY_TYPE_PALWORLD_PAK,
    DEPLOY_TYPE_SLAY_THE_SPIRE,
    DEPLOY_TYPE_STARDEW_VALLEY,
    DEPLOY_TYPE_STELLARIS,
    DEPLOY_TYPE_WARHAMMER3,
    PALWORLD_APP_ID,
    STARDEW_VALLEY_APP_ID,
    DeployContext,
    delete_manifest,
    get_strategy,
    load_manifest,
    resolve_deploy_type,
    resolve_strategy,
    save_manifest,
    supported_deploy_types,
)
from services.deploy_rules.custom import CustomPathStrategy
from services.deploy_rules.manifest import prune_protection
from services.deploy_status import (
    DEPLOY_BLOCKED_CONTENT_MISSING,
    DEPLOY_BLOCKED_FOLDER_MISSING,
    DEPLOY_ERR_COPY,
    DEPLOY_ERR_CUSTOM_PATH_MISSING,
    DEPLOY_ERR_ENTITY_DISK_MISSING,
    DEPLOY_ERR_GAME_INSTALL_MISSING,
    DEPLOY_ERR_GAME_MOD_PATH_MISSING,
    DEPLOY_ERR_IDENTITY_RESOLVE,
    DEPLOY_ERR_MOD_PATH_MISSING,
    DEPLOY_ERR_PERMISSION,
    DEPLOY_ERR_TARGET_FOREIGN,
    DEPLOY_ERR_UNDEPLOY_MISMATCH,
    classify_folder_copy_target,
    content_status_for_mod,
    deploy_block_reason_for_content_status,
    enrich_manifest_fingerprint,
    resolve_deployment_status,
)
from services.deploy_verifier import (
    has_legal_deploy_content,
    plain_content_allow_list,
    verify_deploy_result,
    verify_deploy_source,
    verify_file_plan,
)
from services.file_ops import (
    ModFileManager,
    clear_missing_content_if_present,
    read_is_missing_content,
)
from services.metadata_backup import is_mod_folder_absent

logger = logging.getLogger(__name__)

_conflict_scan_thread: threading.Thread | None = None
_conflict_scan_shutdown = False

MISSING_CONTENT_DEPLOY_ERROR = DEPLOY_BLOCKED_CONTENT_MISSING


def _utc_deploy_time() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _file_plan_result_fields(plan: Any | None) -> dict[str, Any]:
    """Always-populated FilePlan diagnostics for success and failure dicts."""
    if plan is None:
        return {
            "source": "",
            "target": "",
            "planned_files": 0,
            "backed_up_files": 0,
            "applied_files": 0,
            "verified_files": 0,
            "failed_files": 0,
        }
    out = plan.diagnostics_dict()
    out["copied_files"] = int(plan.diagnostics.applied_files or 0)
    return out


def _count_backed_up(prep: Any | None) -> int:
    if prep is None:
        return 0
    by_target = getattr(prep, "by_target", None) or {}
    return sum(1 for info in by_target.values() if info is not None)


def _infer_app_id_from_library_context(
    source: Path,
    *,
    db: DatabaseManager,
    game_name: str = "",
) -> int:
    """
    Resolve ``app_id`` when Mod metadata left it at 0 (common for mod.io stubs).

    Matches ``game_name`` / library parent folder against configured games.
    Never invents an AppID — returns 0 when no game row matches.
    """
    candidates: list[str] = []
    name = str(game_name or "").strip()
    if name:
        candidates.append(name)
    try:
        parent = Path(source).parent.name.strip()
        if parent and parent not in candidates:
            candidates.append(parent)
    except OSError:
        pass
    if not candidates:
        return 0
    try:
        for game in db.list_games():
            labels = {
                str(getattr(game, "name", "") or "").strip(),
                str(getattr(game, "folder_name", "") or "").strip(),
                str(getattr(game, "display_name", "") or "").strip(),
            }
            labels.discard("")
            for cand in candidates:
                if any(label.casefold() == cand.casefold() for label in labels):
                    aid = int(getattr(game, "app_id", 0) or 0)
                    if aid > 0:
                        return aid
    except Exception:  # noqa: BLE001
        logger.debug(
            "infer app_id from library context failed source=%s",
            source,
            exc_info=True,
        )
    return 0


def _entry_is_archive_source(entry: Any) -> bool:
    """True when FileEntry represents an archive source unit (not a plain file)."""
    from services.importers.archive import is_archive_path
    from services.importers.source_files import META_ARCHIVE_NAME

    meta = getattr(entry, "metadata", None)
    if isinstance(meta, dict) and str(meta.get(META_ARCHIVE_NAME) or "").strip():
        return True
    rel = (getattr(entry, "path", None) or getattr(entry, "filename", None) or "")
    rel = str(rel).replace("\\", "/").strip()
    return bool(rel) and is_archive_path(rel)


def _resolve_archive_file(managed: Path, entry: Any) -> Path | None:
    from services.importers.source_files import META_ARCHIVE_NAME

    meta = getattr(entry, "metadata", None)
    names: list[str] = []
    for raw in (
        getattr(entry, "path", None),
        getattr(entry, "filename", None),
        meta.get(META_ARCHIVE_NAME) if isinstance(meta, dict) else None,
    ):
        text = str(raw or "").replace("\\", "/").strip().lstrip("./")
        if text and text not in names:
            names.append(text)
    root = Path(managed)
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
        by_name = root / Path(name).name
        if by_name.is_file():
            return by_name
    return None


def _merge_tree(src: Path, dest: Path) -> None:
    """Merge *src* into *dest*, overwriting existing files."""
    from services.deploy_fs import safe_iter_files

    dest.mkdir(parents=True, exist_ok=True)
    for path in safe_iter_files(src):
        rel = path.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _payload_from_extracts(
    stage: Path,
    extracted_roots: list[Path],
    extra_files: list[tuple[Path, Path]],
) -> Path:
    """Use a single extract tree in place when nothing else needs merging.

    Avoids copying the full extracted payload into ``stage/content`` before
    Apply copies planned files to the game directory (duplicate I/O).
    """
    if len(extracted_roots) == 1 and not extra_files:
        return extracted_roots[0]
    content = stage / "content"
    content.mkdir(parents=True, exist_ok=True)
    for root in extracted_roots:
        _merge_tree(root, content)
    for src, rel in extra_files:
        dest = content / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return content


def _sniff_managed_archives(managed: Path) -> list[Path]:
    """Top-level archive files in a managed Mod folder (never ``.info``)."""
    from services.importers.archive import is_archive_path
    from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

    skip = {INFO_DIR_NAME, LEGACY_INFO_DIR_NAME}
    archives: list[Path] = []
    for path in sorted(managed.iterdir()):
        if path.name in skip or path.name.startswith("."):
            continue
        if path.is_file() and is_archive_path(path):
            archives.append(path)
    return archives


def _iter_plain_managed_files(managed: Path, *, skip_archives: bool) -> list[Path]:
    from services.deploy_fs import safe_iter_files
    from services.importers.archive import is_archive_path
    from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

    skip_dirs = {INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"}
    files: list[Path] = []
    for path in safe_iter_files(managed):
        try:
            rel_parts = path.relative_to(managed).parts
        except ValueError:
            continue
        if any(part in skip_dirs or part == "历史版本" for part in rel_parts):
            continue
        if skip_archives and is_archive_path(path.name):
            continue
        files.append(path)
    return files


def collect_deploy_archives(
    internal_id: int | str,
    managed: Path,
    *,
    db: DatabaseManager | None = None,
) -> list[Path]:
    """Resolve archive files that should be extracted for deploy (not copied as-is)."""
    database = db if db is not None else get_db()
    bundle = database.get_mod_files(internal_id)
    if not bundle.files:
        return _sniff_managed_archives(managed)

    from core.mod_platform import (
        FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
        is_entry_selected_for_deploy,
        normalize_file_role,
    )

    selected = [
        e
        for e in bundle.files
        if is_entry_selected_for_deploy(e)
        and normalize_file_role(getattr(e, "file_role", None))
        != FILE_ROLE_GITHUB_SOURCE_ARCHIVE
    ]
    if not selected:
        return _sniff_managed_archives(managed)

    archives: list[Path] = []
    for entry in selected:
        if not _entry_is_archive_source(entry):
            continue
        resolved = _resolve_archive_file(managed, entry)
        if resolved is not None:
            archives.append(resolved)
    return archives


def _preserve_extract_layout_for_mod(
    internal_id: int | str,
    *,
    db: DatabaseManager | None = None,
) -> bool:
    """
    True when archive extract must keep wrapper folders (Stardew SMAPI).

    Generic ``find_mod_root`` strips a single outer folder, which would break
    Stardew case-2 / multi-mod layout detection via ``manifest.json``.
    """
    try:
        mid = str(internal_id).strip()
        database = db if db is not None else get_db()
        meta = database.get_mod(mid) if mid.isdigit() else None
        app_id = int(getattr(meta, "app_id", 0) or 0) if meta else 0
    except Exception:  # noqa: BLE001
        return False
    return app_id == STARDEW_VALLEY_APP_ID


def _top_level_dir_names(root: Path) -> set[str]:
    """Immediate child directory names under *root* (ignore dot entries)."""
    if not root.is_dir():
        return set()
    names: set[str] = set()
    try:
        for path in root.iterdir():
            if path.name.startswith("."):
                continue
            if path.is_dir():
                names.add(path.name)
    except OSError:
        return set()
    return names


def _extract_overlaps_custom_deploy_dirs(
    extract_dir: Path,
    custom_deploy_path: str | None,
) -> bool:
    """
    Narrow rule: custom deploy target already has a top-level folder whose
    name matches a top-level folder inside the extracted archive.

    Example: extract/bin + GameRoot/bin → treat ``bin`` as install structure,
    not a packaging wrapper (skip ``find_mod_root``).
    """
    raw = str(custom_deploy_path or "").strip()
    if not raw:
        return False
    target = Path(raw).expanduser()
    if not target.is_dir():
        return False
    extract_dirs = _top_level_dir_names(extract_dir)
    if not extract_dirs:
        return False
    target_dirs = _top_level_dir_names(target)
    if not target_dirs:
        return False
    # Case-insensitive: Windows game roots often mix casing.
    return bool({n.casefold() for n in extract_dirs} & {n.casefold() for n in target_dirs})


def _choose_archive_extract_root(
    extract_dir: Path,
    *,
    preserve_extract_layout: bool,
    custom_deploy_path: str = "",
    find_mod_root_fn: Any | None = None,
) -> Path:
    """
    Pick content root for one extracted archive.

    Order:
    1. Explicit preserve (e.g. Stardew)
    2. Narrow custom_deploy_path overlap with extract top-level dirs
    3. Legacy ``find_mod_root``
    """
    if preserve_extract_layout:
        return extract_dir
    if _extract_overlaps_custom_deploy_dirs(extract_dir, custom_deploy_path):
        logger.info(
            "[DEPLOY] preserve extract layout: custom path overlaps top-level dirs "
            "extract=%s custom=%s",
            extract_dir,
            custom_deploy_path,
        )
        return extract_dir
    if find_mod_root_fn is None:
        from services.importers.archive import find_mod_root as find_mod_root_fn
    return find_mod_root_fn(extract_dir) or extract_dir


def validate_deploy_result(result: Any) -> int:
    """
    Verify every ``result.manifest.files`` target exists on disk.

    Delegates to :func:`services.deploy_verifier.verify_deploy_result`
    (existence + size). Raises ``DeployValidationError`` on failure.
    """
    return verify_deploy_result(result, check_size=True, check_hash=False)


def _sha256_file(path: Path, *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def build_deploy_result_files(
    manifest: Any,
    *,
    include_hash: bool = False,
) -> list[dict[str, Any]]:
    """
    Build runtime DeployResult ``files`` entries from the final manifest.

    Does not change on-disk manifest format — this list is only returned to
    callers / logged. Each item: ``source``, ``target``, ``size``, and
    optional ``hash`` (sha256 of the target file when readable).

    ``include_hash`` defaults to False — hashing every target after a large
    Anno deploy (thousands of files) was a confirmed multi-minute stall and is
    not required for verify (existence-only) or Library projection.
    """
    out: list[dict[str, Any]] = []
    for entry in list(getattr(manifest, "files", None) or []):
        source = str(getattr(entry, "source", "") or "")
        target = str(getattr(entry, "target", "") or "")
        item: dict[str, Any] = {
            "source": source,
            "target": target,
            "size": 0,
        }
        path = Path(target) if target else None
        if path is not None and path.is_file():
            try:
                item["size"] = int(path.stat().st_size)
            except OSError:
                item["size"] = 0
            if include_hash:
                try:
                    item["hash"] = _sha256_file(path)
                except OSError:
                    pass
        elif path is not None and path.is_dir():
            # Directory entries are rare; size stays 0, no hash.
            pass
        out.append(item)
    return out


def _deploy_result_file_labels(
    files: list[dict[str, Any]],
    *,
    target_root: str = "",
) -> list[str]:
    """Prefer paths relative to deploy target root for success logs."""
    root = Path(target_root).resolve() if str(target_root or "").strip() else None
    labels: list[str] = []
    for item in files:
        raw = str(item.get("target") or "").strip()
        if not raw:
            continue
        path = Path(raw)
        if root is not None:
            try:
                labels.append(path.resolve().relative_to(root).as_posix())
                continue
            except (ValueError, OSError):
                pass
        labels.append(path.as_posix())
    return labels


def _build_extracted_deploy_content(
    managed: Path,
    archive_paths: list[Path],
    *,
    include_other_plain: bool,
    preserve_extract_layout: bool = False,
    custom_deploy_path: str = "",
) -> tuple[Path, Path]:
    """Extract archives (+ optional loose files) into a temp deploy payload."""
    from services.deploy_apply import extract_archive_via_core
    from services.importers.archive import (
        cleanup_import_cache,
        find_mod_root,
        import_cache_root,
    )

    stage = import_cache_root() / f"deploy_{uuid.uuid4().hex}"
    stage.mkdir(parents=True, exist_ok=False)
    try:
        extracted_roots: list[Path] = []
        for archive in archive_paths:
            extract_dest = stage / f"ex_{uuid.uuid4().hex[:8]}"
            result, ArchiveExtractStatus = extract_archive_via_core(
                archive, extract_dest
            )
            if not result.success:
                if result.status == ArchiveExtractStatus.TIMEOUT:
                    raise TimeoutError(result.error or "压缩包解压超时")
                raise RuntimeError(result.error or "压缩包解压失败")
            extract_dir = Path(result.output_root)
            root = _choose_archive_extract_root(
                extract_dir,
                preserve_extract_layout=preserve_extract_layout,
                custom_deploy_path=custom_deploy_path,
                find_mod_root_fn=find_mod_root,
            )
            extracted_roots.append(root)
        extra_files: list[tuple[Path, Path]] = []
        if include_other_plain:
            for path in _iter_plain_managed_files(managed, skip_archives=True):
                extra_files.append((path, path.relative_to(managed)))
        content = _payload_from_extracts(
            stage, extracted_roots, extra_files
        )
        from services.deploy_fs import safe_iter_files

        if not any(safe_iter_files(content)):
            cleanup_import_cache(stage)
            raise ValueError("压缩包解压后没有可部署的文件")
        return content, stage
    except Exception:
        cleanup_import_cache(stage)
        raise


def prepare_deploy_content(
    internal_id: int | str,
    managed_source: Path,
    *,
    db: DatabaseManager | None = None,
    custom_deploy_path: str | None = None,
) -> tuple[Path, frozenset[str] | None, Path | None]:
    """
    Resolve deploy content root for *mod_id*.

    Archive FileEntries are deploy *units* but not deploy *files*: selected
    archives are extracted and their contents become the deploy payload.
    Plain FileEntries keep path allow-list behaviour.

    Returns ``(content_root, allowed_rel_paths, cleanup_dir)``.
    ``cleanup_dir`` is a temp extract folder to remove after deploy (or None).

    When a listed archive is missing on disk, falls back to the managed folder
    **only** if it already contains legal loose (non-archive) Mod content;
    otherwise raises ``DeploySourceError``.
    """
    from core.mod_platform import (
        FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
        is_entry_selected_for_deploy,
        normalize_file_role,
    )
    from services.importers.archive import (
        cleanup_import_cache,
        find_mod_root,
        import_cache_root,
    )

    database = db if db is not None else get_db()
    managed = Path(managed_source)
    preserve_layout = _preserve_extract_layout_for_mod(internal_id, db=database)

    custom_path = str(custom_deploy_path or "").strip()
    if not custom_path:
        try:
            display = database.get_mod_display_info(internal_id)
            custom_path = (
                str(display.custom_deploy_path or "").strip() if display else ""
            )
        except Exception:  # noqa: BLE001
            custom_path = ""

    def _managed_fallback_or_raise(reason: str) -> tuple[Path, frozenset[str], None]:
        """
        Archive missing / empty extract: allow managed only with loose content.

        Always returns a non-archive allow-list so leftover ``.zip`` files are
        never copied as deploy payload.
        """
        if not has_legal_deploy_content(managed):
            raise DeploySourceError(reason)
        allowed = resolve_deploy_sources(internal_id, managed, db=database)
        if not allowed:
            allowed = plain_content_allow_list(managed)
        if not allowed:
            raise DeploySourceError(reason)
        logger.warning(
            "Archive unavailable; using validated managed loose content "
            "internal_id=%s files=%s reason=%s",
            internal_id,
            len(allowed),
            reason,
        )
        return managed, allowed, None

    bundle = database.get_mod_files(internal_id)
    if not bundle.files:
        sniffed = _sniff_managed_archives(managed)
        if sniffed:
            content, stage = _build_extracted_deploy_content(
                managed,
                sniffed,
                include_other_plain=True,
                preserve_extract_layout=preserve_layout,
                custom_deploy_path=custom_path,
            )
            return content, None, stage
        if not has_legal_deploy_content(managed):
            raise DeploySourceError(
                "部署源没有可部署的内容：managed 目录为空或仅有压缩包"
            )
        return managed, None, None

    selected = [
        e
        for e in bundle.files
        if is_entry_selected_for_deploy(e)
        and normalize_file_role(getattr(e, "file_role", None))
        != FILE_ROLE_GITHUB_SOURCE_ARCHIVE
    ]
    if not selected:
        sniffed = _sniff_managed_archives(managed)
        if sniffed:
            content, stage = _build_extracted_deploy_content(
                managed,
                sniffed,
                include_other_plain=True,
                preserve_extract_layout=preserve_layout,
                custom_deploy_path=custom_path,
            )
            return content, None, stage
        if not has_legal_deploy_content(managed):
            raise DeploySourceError(
                "部署源没有可部署的内容：无选中文件且 managed 非法"
            )
        return managed, None, None

    archive_entries = [e for e in selected if _entry_is_archive_source(e)]
    plain_entries = [e for e in selected if not _entry_is_archive_source(e)]

    if not archive_entries:
        if not has_legal_deploy_content(managed):
            # Plain entries selected but nothing on disk / archives-only.
            raise DeploySourceError(
                "部署源没有可部署的内容：选中文件均不可用"
            )
        return managed, resolve_deploy_sources(internal_id, managed, db=database), None

    stage = import_cache_root() / f"deploy_{uuid.uuid4().hex}"
    stage.mkdir(parents=True, exist_ok=False)
    try:
        extracted_roots: list[Path] = []
        for entry in archive_entries:
            archive = _resolve_archive_file(managed, entry)
            if archive is None:
                label = str(
                    getattr(entry, "filename", None)
                    or getattr(entry, "path", None)
                    or entry
                )
                logger.warning(
                    "Archive source missing; validating managed dir before fallback: %s",
                    label,
                )
                cleanup_import_cache(stage)
                return _managed_fallback_or_raise(
                    f"压缩包源缺失且 managed 目录无合法 Mod 内容：{label}"
                )
            extract_dest = stage / f"ex_{uuid.uuid4().hex[:8]}"
            from services.deploy_apply import extract_archive_via_core

            extracted, ArchiveExtractStatus = extract_archive_via_core(
                archive, extract_dest
            )
            if not extracted.success:
                cleanup_import_cache(stage)
                if extracted.status == ArchiveExtractStatus.TIMEOUT:
                    raise TimeoutError(extracted.error or "压缩包解压超时")
                raise RuntimeError(extracted.error or "压缩包解压失败")
            extract_dir = Path(extracted.output_root)
            root = _choose_archive_extract_root(
                extract_dir,
                preserve_extract_layout=preserve_layout,
                custom_deploy_path=custom_path,
                find_mod_root_fn=find_mod_root,
            )
            extracted_roots.append(root)

        extra_files: list[tuple[Path, Path]] = []
        for entry in plain_entries:
            rel = (entry.path or entry.filename or "").replace("\\", "/").strip().lstrip("./")
            if not rel:
                continue
            src = managed / rel
            if not src.is_file():
                src = managed / Path(rel).name
            if not src.is_file():
                continue
            extra_files.append((src, Path(rel)))

        content = _payload_from_extracts(stage, extracted_roots, extra_files)
        from services.deploy_fs import safe_iter_files

        if not any(safe_iter_files(content)):
            cleanup_import_cache(stage)
            return _managed_fallback_or_raise(
                "压缩包解压后没有可部署的文件，且 managed 目录无合法 Mod 内容"
            )

        # Extracted payload: deploy whole content tree (never the .zip itself).
        return content, None, stage
    except DeploySourceError:
        cleanup_import_cache(stage)
        raise
    except Exception:
        cleanup_import_cache(stage)
        raise


def resolve_deploy_sources(
    internal_id: int | str,
    source: Path,
    *,
    db: DatabaseManager | None = None,
) -> frozenset[str] | None:
    """
    Resolve which relative files may be deployed for *mod_id*.

    - ``mod_files`` empty → ``None`` (legacy: deploy whole Mod / strategy scan)
    - otherwise → relative paths of selected entries only
      (prefer ``selected_for_deploy``, fall back to ``enabled``)

    Archive source entries are excluded here — use :func:`prepare_deploy_content`.
    """
    from core.mod_platform import (
        FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
        is_entry_selected_for_deploy,
        normalize_file_role,
    )

    database = db if db is not None else get_db()
    bundle = database.get_mod_files(internal_id)
    if not bundle.files:
        return None
    root = Path(source)
    allowed: set[str] = set()
    for entry in bundle.files:
        if normalize_file_role(getattr(entry, "file_role", None)) == (
            FILE_ROLE_GITHUB_SOURCE_ARCHIVE
        ):
            continue
        if not is_entry_selected_for_deploy(entry):
            continue
        if _entry_is_archive_source(entry):
            continue
        rel = (entry.path or entry.filename or "").replace("\\", "/").strip().lstrip("./")
        if not rel:
            continue
        allowed.add(rel)
        name = Path(rel).name
        if name:
            allowed.add(name)
        candidate = root / rel
        if candidate.is_file():
            try:
                allowed.add(candidate.resolve().relative_to(root.resolve()).as_posix())
            except ValueError:
                pass
    return frozenset(allowed) if allowed else None


def _normalize_deploy_allow_list(
    allowed: frozenset[str] | None,
) -> frozenset[str] | None:
    """Empty allow-list → whole managed directory deploy."""
    if allowed is not None and len(allowed) == 0:
        return None
    return allowed


def _normalize_deploy_error(error: str) -> str:
    """Map common failures to stable, user-facing messages (keep concrete detail)."""
    text = (error or "").strip()
    if not text:
        return "未知错误"
    # Path-lifecycle messages must stay specific — never collapse to game-settings.
    from services.deploy_path_lifecycle import (
        FORBIDDEN_VAGUE_MOD_PATH_COPY,
        is_path_lifecycle_error,
    )

    if is_path_lifecycle_error(text):
        return text
    low = text.lower()
    if text in (
        DEPLOY_BLOCKED_FOLDER_MISSING,
        DEPLOY_ERR_ENTITY_DISK_MISSING,
        DEPLOY_ERR_TARGET_FOREIGN,
        DEPLOY_ERR_PERMISSION,
        DEPLOY_ERR_COPY,
        DEPLOY_ERR_UNDEPLOY_MISMATCH,
        MISSING_CONTENT_DEPLOY_ERROR,
    ):
        return text
    # Never reintroduce the vague game-settings copy for path lifecycle.
    if text == FORBIDDEN_VAGUE_MOD_PATH_COPY or text == DEPLOY_ERR_MOD_PATH_MISSING:
        return text
    if text.startswith(
        (
            DEPLOY_ERR_CUSTOM_PATH_MISSING,
            DEPLOY_ERR_GAME_INSTALL_MISSING,
            DEPLOY_ERR_GAME_MOD_PATH_MISSING,
            DEPLOY_ERR_IDENTITY_RESOLVE,
        )
    ):
        return text
    if "目标目录已存在其他" in text:
        return DEPLOY_ERR_TARGET_FOREIGN
    if "请先配置游戏部署目录" in text:
        return text
    if "请先配置游戏安装目录" in text:
        return text
    if "游戏安装目录不存在" in text:
        return text
    if "游戏Mod部署目录不存在" in text or "游戏 Mod 部署目录不存在" in text:
        return text
    if text.startswith("Mod自定义部署路径不存在"):
        return text
    if "Target mod directory does not exist" in text:
        # Legacy English — preserve detail; do not collapse to vague settings copy.
        return text
    if "permission denied" in low or ("拒绝" in text and "访问" in text):
        return DEPLOY_ERR_PERMISSION
    if text.startswith("复制失败") or "复制失败" in text:
        return DEPLOY_ERR_COPY
    # Keep archive / extract reasons verbatim for the Detail status banner.
    return text


def _finalize_deploy_dict(
    data: dict[str, Any],
    *,
    log_prefix: str = "",
) -> dict[str, Any]:
    """Normalize legacy deploy dicts and emit ``[DEPLOY_RESULT]``."""
    from services.deploy_result import DeployResult, normalize_deploy_dict

    out = normalize_deploy_dict(data)
    result = DeployResult.from_dict(out)
    prefix = log_prefix or "[DEPLOY]"
    if result.success:
        logger.info(
            "%s [DEPLOY_RESULT] internal_id=%s status=SUCCESS strategy=%s copied_files=%s",
            prefix,
            result.internal_id,
            result.strategy,
            result.copied_files,
        )
    else:
        logger.warning(
            "%s [DEPLOY_RESULT] internal_id=%s status=%s error_code=%s error=%s",
            prefix,
            result.internal_id,
            result.status.value,
            result.error_code or "deploy_failed",
            result.error or "unknown",
        )
    return out


def _schedule_post_deploy_conflict_scan(
    library_root: Path,
    *,
    db: DatabaseManager | None = None,
    log_prefix: str = "",
) -> None:
    """Removed: post-deploy scans must not touch user conflict annotation.

    ARCHITECTURE RULE: Conflict is user annotation. Deploy must not schedule
    ConflictDetector persistence. Kept as a no-op so call sites / tests that
    still reference the symbol do not invent a replacement writer.
    """
    del library_root, db, log_prefix


def request_deploy_conflict_scan_shutdown() -> None:
    global _conflict_scan_shutdown
    _conflict_scan_shutdown = True


def join_deploy_conflict_scan(timeout: float) -> bool:
    thread = _conflict_scan_thread
    if thread is None or not thread.is_alive():
        return True
    thread.join(timeout)
    return not thread.is_alive()


def reset_deploy_conflict_scan_state() -> None:
    global _conflict_scan_thread, _conflict_scan_shutdown
    _conflict_scan_shutdown = False
    _conflict_scan_thread = None


class ModDeployer:
    """
    ARCHITECTURE CONTRACT — Core owns the Deploy lifecycle.

    Resolve → strategy.plan() (path mapping only) → DeployFilePlan
    → Backup → Apply → Verify → Manifest → Result.

    The Core MUST NOT call Strategy.deploy() for execution.
    Strategy supplies mappings; all stages share one DeployFilePlan.
    ``strategy.plan()`` builds mappings / FilePlan input — it does not deploy.
    """

    def __init__(
        self,
        library_root: str | Path | None = None,
        *,
        db: DatabaseManager | None = None,
    ) -> None:
        root = Path(library_root) if library_root else default_mod_library()
        self.library_root = root.expanduser().resolve()
        self._db = db
        self.files = ModFileManager(self.library_root)

    def _database(self) -> DatabaseManager:
        return self._db if self._db is not None else get_db()

    def _resolve_context(
        self,
        internal_id: int | str,
        *,
        require_target_exists: bool = False,
        prepare_archives: bool = True,
        for_undeploy: bool = False,
    ) -> tuple[DeployContext | None, dict[str, Any] | None, Path | None]:
        from services.deploy_path_lifecycle import (
            GAME_CONFIG_PATH_MISSING,
            custom_deploy_path_failure,
            game_config_path_failure,
            resolve_entity_internal_id,
            source_mod_path_failure,
            validate_custom_deploy_target,
            validate_game_install_dir,
            validate_game_mod_path,
        )

        db = self._database()
        mid, identity_err = resolve_entity_internal_id(internal_id, db=db)
        if identity_err:
            return None, {
                "success": False,
                "error": identity_err,
            }, None

        db_meta = db.get_mod(mid)
        source = resolve_deploy_managed_path(
            mid,
            db=db,
            library_root=self.library_root,
            file_manager=self.files,
        )

        skip_library_payload = False
        hint = ""
        try:
            brow = db.get_mod_backup_row(mid) or {}
            hint = str(brow.get("last_known_path") or "").strip()
        except Exception:  # noqa: BLE001
            hint = str(getattr(db_meta, "last_known_path", "") or "").strip()
        if source is None or not source.is_dir():
            from services.mod_presence import deployment_capability

            cap = deployment_capability(mid, db=db)
            if not cap.allowed:
                fail = source_mod_path_failure(
                    library_root=self.library_root,
                    internal_id=mid,
                    path=hint or None,
                )
                return None, fail.as_error_dict(mod_id=mid), None
            skip_library_payload = True
            source = Path(hint) if hint else Path(f"__missing__/{mid}")
            prepare_archives = False

        if source.is_dir():
            source = source.resolve()
        fs_meta = self.files.load_metadata(source) if source.is_dir() else None
        app_id = 0
        if db_meta and db_meta.app_id:
            app_id = int(db_meta.app_id)
        elif fs_meta and fs_meta.app_id:
            app_id = int(fs_meta.app_id)

        if not app_id:
            game_name = ""
            if fs_meta is not None:
                game_name = str(getattr(fs_meta, "game_name", "") or "").strip()
            inferred = _infer_app_id_from_library_context(
                source, db=db, game_name=game_name
            )
            if inferred > 0:
                app_id = inferred
                try:
                    db.update_mod_identity_fields(mid, app_id=app_id)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "persist inferred app_id failed internal_id=%s app_id=%s",
                        mid,
                        app_id,
                        exc_info=True,
                    )
                logger.info(
                    "[DEPLOY] internal_id=%s inferred app_id=%s from library context",
                    mid,
                    app_id,
                )

        if not app_id:
            return None, {
                "success": False,
                "error": f"无法解析 Mod 所属游戏 AppID（internal_id={mid}）",
                "mod_id": mid,
            }, None

        cfg = db.get_game_deploy_config(app_id)
        display = db.get_mod_display_info(mid)
        custom_deploy_path = (
            str(display.custom_deploy_path or "").strip() if display else ""
        )
        workspace_id = (
            str(display.workspace_id or "").strip() if display else ""
        )
        if display is not None:
            from services.mod_identity_authority import safe_workspace_id_for_deploy

            workspace_id = safe_workspace_id_for_deploy(
                platform=str(display.platform or ""),
                workspace_id=workspace_id,
                mod_id=mid,
                source_url=str(display.source_url or ""),
                external_id=str(display.external_id or ""),
            )

        def _game_path_err(
            field: str, raw_path: str, *, code_hint: str = GAME_CONFIG_PATH_MISSING
        ) -> dict[str, Any]:
            del code_hint
            fail = game_config_path_failure(
                field=field, path=raw_path, app_id=app_id, internal_id=mid
            )
            return fail.as_error_dict(mod_id=mid)

        if custom_deploy_path:
            # Custom absolute path overrides all game-level deploy rules.
            if cfg is None:
                cfg = GameDeployConfig(app_id=app_id)
            if require_target_exists:
                custom_err = validate_custom_deploy_target(
                    custom_deploy_path, app_id=app_id
                )
                if custom_err:
                    fail = custom_deploy_path_failure(
                        path=custom_deploy_path,
                        app_id=app_id,
                        internal_id=mid,
                    )
                    return None, fail.as_error_dict(mod_id=mid), None
            deploy_type = DEPLOY_TYPE_CUSTOM_PATH
        else:
            if cfg is None:
                return None, {
                    "success": False,
                    "error": "请先配置游戏部署目录",
                    "mod_id": mid,
                    "error_kind": GAME_CONFIG_PATH_MISSING,
                    "error_code": GAME_CONFIG_PATH_MISSING,
                    "app_id": app_id,
                    "path_field": "mod_path",
                    "configured_path": "",
                }, None

            deploy_type = resolve_deploy_type(app_id, cfg.deploy_type)
            if deploy_type == DEPLOY_TYPE_FOLDER_COPY and not str(
                cfg.mod_path or ""
            ).strip():
                return None, {
                    "success": False,
                    "error": (
                        f"请先配置游戏部署目录 "
                        f"(field=mod_path, app_id={app_id}, "
                        f"code={GAME_CONFIG_PATH_MISSING})"
                    ),
                    "mod_id": mid,
                    "error_kind": GAME_CONFIG_PATH_MISSING,
                    "error_code": GAME_CONFIG_PATH_MISSING,
                    "app_id": app_id,
                    "path_field": "mod_path",
                    "configured_path": "",
                }, None
            # Palworld: install_path and/or mod_path — strategy picks pak vs folder_copy.
            if deploy_type == DEPLOY_TYPE_PALWORLD_PAK:
                has_install = bool(str(cfg.install_path or "").strip())
                has_mod = bool(str(cfg.mod_path or "").strip())
                if not has_install and not has_mod:
                    return None, {
                        "success": False,
                        "error": (
                            f"请先配置游戏安装目录或部署目录 "
                            f"(app_id={app_id}, code={GAME_CONFIG_PATH_MISSING})"
                        ),
                        "mod_id": mid,
                        "error_kind": GAME_CONFIG_PATH_MISSING,
                        "error_code": GAME_CONFIG_PATH_MISSING,
                        "app_id": app_id,
                        "path_field": "install_path",
                        "configured_path": "",
                    }, None
            # Anno 1800: prefer install_path → mods/; mod_path is a fallback root.
            if deploy_type == DEPLOY_TYPE_ANNO_1800:
                has_install = bool(str(cfg.install_path or "").strip())
                has_mod = bool(str(cfg.mod_path or "").strip())
                if not has_install and not has_mod:
                    return None, {
                        "success": False,
                        "error": (
                            f"请先配置游戏安装目录或部署目录 "
                            f"(app_id={app_id}, code={GAME_CONFIG_PATH_MISSING})"
                        ),
                        "mod_id": mid,
                        "error_kind": GAME_CONFIG_PATH_MISSING,
                        "error_code": GAME_CONFIG_PATH_MISSING,
                        "app_id": app_id,
                        "path_field": "install_path",
                        "configured_path": "",
                    }, None
            if deploy_type == DEPLOY_TYPE_SLAY_THE_SPIRE:
                if not str(cfg.install_path or "").strip():
                    return None, {
                        "success": False,
                        "error": (
                            f"请先配置游戏安装目录 "
                            f"(field=install_path, app_id={app_id}, "
                            f"code={GAME_CONFIG_PATH_MISSING})"
                        ),
                        "mod_id": mid,
                        "error_kind": GAME_CONFIG_PATH_MISSING,
                        "error_code": GAME_CONFIG_PATH_MISSING,
                        "app_id": app_id,
                        "path_field": "install_path",
                        "configured_path": "",
                    }, None
            if deploy_type == DEPLOY_TYPE_STARDEW_VALLEY:
                if not str(cfg.mod_path or "").strip():
                    return None, {
                        "success": False,
                        "error": (
                            f"请先配置游戏部署目录 "
                            f"(field=mod_path, app_id={app_id}, "
                            f"code={GAME_CONFIG_PATH_MISSING})"
                        ),
                        "mod_id": mid,
                        "error_kind": GAME_CONFIG_PATH_MISSING,
                        "error_code": GAME_CONFIG_PATH_MISSING,
                        "app_id": app_id,
                        "path_field": "mod_path",
                        "configured_path": "",
                    }, None
            if (
                require_target_exists
                and deploy_type == DEPLOY_TYPE_FOLDER_COPY
            ):
                mod_raw = str(cfg.mod_path or "").strip()
                mod_err = validate_game_mod_path(mod_raw, app_id=app_id)
                if mod_err:
                    return None, _game_path_err("mod_path", mod_raw), None
            if (
                require_target_exists
                and deploy_type == DEPLOY_TYPE_PALWORLD_PAK
            ):
                install_raw = str(cfg.install_path or "").strip()
                mod_raw = str(cfg.mod_path or "").strip()
                install_err = (
                    validate_game_install_dir(install_raw, app_id=app_id)
                    if install_raw
                    else None
                )
                mod_err = (
                    validate_game_mod_path(mod_raw, app_id=app_id)
                    if mod_raw
                    else None
                )
                if (install_raw or mod_raw) and install_err and (
                    not mod_raw or mod_err
                ):
                    if install_raw and install_err:
                        return None, _game_path_err("install_path", install_raw), None
                    return None, _game_path_err("mod_path", mod_raw), None
            # Anno: strategy mkdir(mods/) — only require install root when set.
            if require_target_exists and deploy_type == DEPLOY_TYPE_ANNO_1800:
                install_raw = str(cfg.install_path or "").strip()
                mod_raw = str(cfg.mod_path or "").strip()
                if install_raw:
                    install_err = validate_game_install_dir(
                        install_raw, app_id=app_id
                    )
                    if install_err:
                        return None, _game_path_err("install_path", install_raw), None
                elif mod_raw:
                    mod_err = validate_game_mod_path(mod_raw, app_id=app_id)
                    if mod_err:
                        return None, _game_path_err("mod_path", mod_raw), None
            if require_target_exists and deploy_type == DEPLOY_TYPE_SLAY_THE_SPIRE:
                install_raw = str(cfg.install_path or "").strip()
                install_err = validate_game_install_dir(
                    install_raw, app_id=app_id
                )
                if install_err or not install_raw:
                    if install_raw:
                        return None, _game_path_err("install_path", install_raw), None
                    return None, {
                        "success": False,
                        "error": (
                            f"请先配置游戏安装目录 "
                            f"(field=install_path, app_id={app_id}, "
                            f"code={GAME_CONFIG_PATH_MISSING})"
                        ),
                        "mod_id": mid,
                        "error_kind": GAME_CONFIG_PATH_MISSING,
                        "error_code": GAME_CONFIG_PATH_MISSING,
                        "app_id": app_id,
                        "path_field": "install_path",
                        "configured_path": "",
                    }, None
            if require_target_exists and deploy_type == DEPLOY_TYPE_STARDEW_VALLEY:
                mod_raw = str(cfg.mod_path or "").strip()
                mod_err = validate_game_mod_path(mod_raw, app_id=app_id)
                if mod_err:
                    return None, _game_path_err("mod_path", mod_raw), None

        cleanup: Path | None = None
        content_root = source
        allowed: frozenset[str] | None
        # Anno archive payload: strategy extracts zip roots into mods/.
        # Do not pre-extract into import_cache (double extract + MAX_PATH).
        defer_archive_extract = False
        if prepare_archives and deploy_type == DEPLOY_TYPE_ANNO_1800:
            defer_archive_extract = bool(
                collect_deploy_archives(mid, source, db=db)
            )
        # WH3 activation never extracts archives: packs stay in the library.
        if prepare_archives and deploy_type == DEPLOY_TYPE_WARHAMMER3:
            defer_archive_extract = True
        if prepare_archives and not defer_archive_extract:
            from services.deploy_stage_log import deploy_stage

            try:
                with deploy_stage("extract", internal_id=str(internal_id)):
                    content_root, allowed, cleanup = prepare_deploy_content(
                        mid, source, db=db
                    )
                allowed = _normalize_deploy_allow_list(allowed)
            except (
                FileNotFoundError,
                ValueError,
                RuntimeError,
                OSError,
                TimeoutError,
                DeploySourceError,
            ) as exc:
                from services.deploy_archive_errors import archive_error_code

                err_text = str(exc)
                code = archive_error_code(err_text)
                if isinstance(exc, TimeoutError):
                    code = "ARCHIVE_TIMEOUT"
                out: dict[str, Any] = {
                    "success": False,
                    "error": err_text,
                    "error_code": code,
                    "mod_id": mid,
                }
                if code == "ARCHIVE_TIMEOUT":
                    out["status"] = "TIMEOUT"
                return None, out, None
        else:
            if skip_library_payload or not source.is_dir():
                allowed = None
            else:
                allowed = _normalize_deploy_allow_list(
                    resolve_deploy_sources(mid, source, db=db)
                )

        return (
            DeployContext(
                internal_id=mid,
                source=content_root,
                managed_path=source,
                app_id=app_id,
                config=cfg,
                deploy_type=deploy_type,
                allowed_rel_paths=allowed,
                custom_deploy_path=custom_deploy_path,
                workspace_id=workspace_id,
            ),
            None,
            cleanup,
        )

    def _mark_failed(
        self, mid: str, *, app_id: int = 0, error: str = ""
    ) -> None:
        """
        Record deployment_status=failed only.

        Never mutates ``content_status`` / ``folder_present`` /
        ``is_missing_content`` — Deploy failure ≠ content missing.
        """
        msg = _normalize_deploy_error(error)
        try:
            self._database().update_mod_deploy_status(
                mid,
                deploy_status=DEPLOY_STATUS_FAILED,
                deploy_path="",
                deploy_time="",
                deploy_error=msg,
                app_id=app_id or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[DEPLOY] internal_id=%s failed to record failed status: %s (%s)",
                mid,
                exc,
                msg,
            )

    def _abort_failed_deploy(
        self,
        *,
        mid: str,
        app_id: int,
        manifest_root: Path,
        backup_mgr: BackupManager,
        prep: object | None,
        error: str,
    ) -> bool:
        """
        After a failed deploy attempt: try rollback, then align DB.

        Rollback only touches targets recorded in *prep* (this attempt's planned
        overwrites). Pre-existing game files that were not backed up are not removed.

        Rollback success restores game files — it does **not** mean the deploy
        operation succeeded. DB always records ``deploy_status=failed`` with
        ``deploy_error`` preserved.

        Returns ``True`` when rollback succeeded (or there was nothing to roll
        back): manifest/transaction cleaned, DB ``failed``.

        Returns ``False`` when rollback failed: keeps manifest, backups, and
        ``deploy_transaction.json`` for recovery; DB ``failed``.
        """
        from services.deploy_txn import (
            PHASE_ROLLBACK,
            log_txn_phase,
            unregister_active_deploy_transaction,
        )

        msg = _normalize_deploy_error(error)
        rollback_ok = True
        if prep is not None:
            try:
                backup_mgr.rollback(prep)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001
                rollback_ok = False
                logger.error(
                    "[DEPLOY] rollback failed, keeping manifest and backup "
                    "for recovery internal_id=%s error=%s",
                    mid,
                    exc,
                )

        log_txn_phase(
            PHASE_ROLLBACK,
            internal_id=str(mid),
            managed=manifest_root,
            extra=f"rollback_ok={1 if rollback_ok else 0}",
        )
        unregister_active_deploy_transaction(manifest_root)

        if not rollback_ok:
            self._mark_failed(mid, app_id=app_id, error=msg)
            return False

        try:
            delete_manifest(manifest_root)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[DEPLOY] internal_id=%s delete_manifest after failure failed", mid
            )
        try:
            backup_mgr.clear_transaction()
        except Exception:  # noqa: BLE001
            logger.exception(
                "[DEPLOY] internal_id=%s clear_transaction after rollback failed", mid
            )
        # Rollback restored files; deploy outcome is still FAILED (never NOT_DEPLOYED).
        self._mark_failed(mid, app_id=app_id, error=msg)
        logger.warning(
            "[DEPLOY] internal_id=%s abort_failed_deploy rollback_ok=1 "
            "deploy_status=failed error=%s",
            mid,
            msg,
        )
        return True

    def recover_stale_deploy_transactions(self) -> list[dict[str, Any]]:
        """
        Scan managed Mods for leftover ``deploy_transaction.json`` and recover.

        Intended for library startup / reconcile. Does not change ConflictDetector.
        """
        from services.file_ops import ModFileManager

        reports: list[dict[str, Any]] = []
        try:
            folders = ModFileManager(self.library_root).list_managed_mods()
        except Exception:  # noqa: BLE001
            logger.exception("[DEPLOY] stale transaction scan failed to list mods")
            return reports

        from services.deploy_txn import (
            compose_recover_deploy_error,
            is_active_deploy_transaction,
        )

        for folder in folders:
            mgr = BackupManager(folder)
            txn = mgr.load_transaction()
            if not txn:
                continue
            # Concurrent deploy holds this managed path — do not recover it.
            if is_active_deploy_transaction(folder):
                logger.info(
                    "[DEPLOY] skip stale txn recovery; active deploy managed=%s",
                    folder,
                )
                reports.append(
                    {
                        "mod_id": str(txn.get("mod_id") or "").strip(),
                        "managed_path": str(folder),
                        "action": "skipped_active",
                        "status": str(txn.get("status") or ""),
                        "message": "active deploy transaction — reconcile skipped",
                    }
                )
                continue
            result = mgr.recover_interrupted_transaction(auto_rollback=True)
            mid = str(txn.get("mod_id") or "").strip()
            if not mid.isdigit():
                try:
                    from services.file_ops import read_info_metadata_dict
                    from services.mod_identity import read_internal_id

                    raw = read_info_metadata_dict(folder) or {}
                    proof = read_internal_id(raw)
                    if proof:
                        found = self._database().find_mod_by_internal_id(proof)
                        if found is not None:
                            mid = str(found)
                        elif proof.isdigit():
                            mid = proof
                except Exception:  # noqa: BLE001
                    mid = ""
            # Never invent mod_id from the managed directory name.
            entry = {
                "mod_id": mid,
                "managed_path": str(folder),
                **result,
            }
            reports.append(entry)
            action = str(result.get("action") or "")
            if action == "rolled_back" and mid.isdigit():
                try:
                    prev = self._database().get_mod_deploy_info(mid)
                    prev_error = prev.deploy_error if prev is not None else ""
                    prev_status = (
                        str(prev.deploy_status or "").strip().lower()
                        if prev is not None
                        else ""
                    )
                    # Preserve FAILED + original error; only append rollback note.
                    next_status = (
                        DEPLOY_STATUS_FAILED
                        if prev_status == DEPLOY_STATUS_FAILED
                        else DEPLOY_STATUS_NOT_DEPLOYED
                    )
                    self._database().update_mod_deploy_status(
                        mid,
                        deploy_status=next_status,
                        deploy_path="",
                        deploy_time="",
                        deploy_error=compose_recover_deploy_error(prev_error),
                        app_id=None,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[DEPLOY] internal_id=%s failed to clear status after txn rollback",
                        mid,
                    )
            elif action in {"needs_attention", "marked_failed"}:
                logger.warning(
                    "[DEPLOY] stale transaction needs attention internal_id=%s path=%s: %s",
                    mid,
                    folder,
                    result.get("message"),
                )
        if reports:
            logger.info(
                "[DEPLOY] stale transaction recovery reports=%s", len(reports)
            )
        return reports

    def deploy_mod(
        self,
        internal_id: int | str,
        *,
        _deploy_stack: frozenset[str] | None = None,
        _skip_target_ownership_check: bool = False,
    ) -> dict[str, Any]:
        """Deploy one Mod by Workshop / published file id."""
        from services.deploy_lock import deploy_operation_lock
        from services.deploy_result import normalize_deploy_dict, terminal_failed

        mid = resolve_deploy_identity(internal_id, db=self._database())
        log_prefix = f"[DEPLOY] internal_id={mid}"
        from services.identity_service import lifecycle_scope
        from services.deploy_stage_log import (
            deploy_timing_session,
            log_deploy_result,
            write_deploy_timing,
        )

        try:
            with lifecycle_scope("deploy"), deploy_operation_lock(mid):
                with deploy_timing_session(internal_id=mid) as sess:
                    out = self._deploy_mod_body(
                        mid,
                        _deploy_stack=_deploy_stack,
                        _skip_target_ownership_check=_skip_target_ownership_check,
                    )
                    status = "ok" if out.get("success") else "failed"
                    sess.source = str(out.get("source") or sess.source or "")
                    sess.target = str(out.get("target") or sess.target or "")
                    sess.files = int(out.get("copied_files") or sess.files or 0)
                    log_deploy_result(
                        sess,
                        status=status,
                        error=str(out.get("error") or ""),
                        files=sess.files,
                        source=sess.source,
                        target=sess.target,
                    )
                    write_deploy_timing(out.get("managed_path") or sess.source, sess)
                    finalized = _finalize_deploy_dict(out, log_prefix=log_prefix)
                    finalized["deploy_timing"] = sess.to_dict()
                    return finalized
        except RuntimeError as exc:
            msg = str(exc)
            if "已有部署任务" in msg:
                return normalize_deploy_dict(
                    terminal_failed(
                        msg,
                        internal_id=mid,
                        error_code="deploy_in_progress",
                    )
                )
            raise

    def _deploy_mod_body(
        self,
        internal_id: int | str,
        *,
        _deploy_stack: frozenset[str] | None = None,
        _skip_target_ownership_check: bool = False,
    ) -> dict[str, Any]:
        """Internal deploy implementation (caller holds deploy lock)."""
        mid = resolve_deploy_identity(internal_id, db=self._database())
        log_prefix = f"[DEPLOY] internal_id={mid}"

        from services.runtime_identity import log_archive_runtime_identity

        log_archive_runtime_identity(logger, prefix="[DEPLOY_RUNTIME]")

        from services.stellaris_activation import is_stellaris_activation_app
        from services.wh3_activation import is_wh3_activation_app

        app_id_hint = 0
        if mid.isdigit():
            try:
                row = self._database().get_mod_backup_row(mid)
                if row is not None:
                    app_id_hint = int(row.get("app_id") or 0)
            except Exception:  # noqa: BLE001
                app_id_hint = 0
        wh3_activation = is_wh3_activation_app(app_id_hint)
        stellaris_activation = is_stellaris_activation_app(app_id_hint)

        if (
            mid.isdigit()
            and not self._database().is_mod_enabled(mid)
            and not wh3_activation
            and not stellaris_activation
        ):
            error = "Mod disabled"
            logger.warning(
                "%s result=fail stage=early_gate reason=mod_disabled error=%s",
                log_prefix,
                error,
            )
            self._mark_failed(mid, error=error)
            return {
                "success": False,
                "error": error,
                "mod_id": mid,
            }

        from services.mod_presence import deployment_capability

        cap = deployment_capability(mid, db=self._database())
        workshop_miss_ok = bool(cap.allowed and cap.source_kind == "workshop")

        # content_status gates (Phase 8) — separate from deployment_status
        blocked = deploy_block_reason_for_content_status(
            content_status_for_mod(mid, db=self._database())
        )
        if blocked and not workshop_miss_ok:
            logger.warning(
                "%s result=fail stage=early_gate reason=content_status "
                "error=%s",
                log_prefix,
                blocked,
            )
            if mid.isdigit():
                self._mark_failed(mid, error=blocked)
            return {
                "success": False,
                "error": blocked,
                "mod_id": mid,
                "folder_missing": blocked == DEPLOY_BLOCKED_FOLDER_MISSING,
                "is_missing_content": blocked == DEPLOY_BLOCKED_CONTENT_MISSING,
            }

        source_for_gate = resolve_deploy_managed_path(
            mid,
            db=self._database(),
            library_root=self.library_root,
            file_manager=self.files,
        )
        if source_for_gate is None and not workshop_miss_ok:
            # Entity exists in DB but no proven disk folder via .info/entity_key.
            # Never confuse this with game ``mod_path`` configuration errors.
            from services.deploy_path_lifecycle import source_mod_path_failure

            logger.warning(
                "%s result=fail stage=early_gate reason=SOURCE_MOD_PATH_MISSING",
                log_prefix,
            )
            fail = source_mod_path_failure(
                library_root=self.library_root,
                internal_id=mid,
            )
            out = fail.as_error_dict(mod_id=mid)
            out["folder_missing"] = True
            if mid.isdigit():
                self._mark_failed(mid, error=str(out.get("error") or ""))
            return out
        if (
            source_for_gate is not None
            and is_mod_folder_absent(mid, source_for_gate)
            and not workshop_miss_ok
        ):
            logger.warning(
                "%s result=fail stage=early_gate reason=folder_missing error=%s",
                log_prefix,
                DEPLOY_BLOCKED_FOLDER_MISSING,
            )
            if mid.isdigit():
                self._mark_failed(mid, error=DEPLOY_BLOCKED_FOLDER_MISSING)
            return {
                "success": False,
                "error": DEPLOY_BLOCKED_FOLDER_MISSING,
                "mod_id": mid,
                "folder_missing": True,
            }
        # Content-missing gate uses live filesystem payload. Heal stale sticky
        # ``is_missing_content`` markers first — Deploy failure must never be
        # confused with (or cause) content_status=content_missing.
        if source_for_gate is not None:
            try:
                clear_missing_content_if_present(source_for_gate)
            except OSError:
                logger.debug(
                    "%s clear stale missing-content marker failed",
                    log_prefix,
                    exc_info=True,
                )

        try:
            if not wh3_activation and not workshop_miss_ok:
                from services.mod_source_integrity import validate_source

                validate_source(
                    mid,
                    managed_path=source_for_gate,
                    db=self._database(),
                )
        except DeploySourceError as exc:
            if exc.code == "no_deployable_source":
                error = MISSING_CONTENT_DEPLOY_ERROR
            else:
                error = _normalize_deploy_error(str(exc))
            logger.warning(
                "%s result=fail reason=source_integrity error=%s code=%s",
                log_prefix,
                error,
                getattr(exc, "code", ""),
            )
            if mid.isdigit():
                self._mark_failed(mid, error=error)
            out: dict[str, Any] = {
                "success": False,
                "error": error,
                "reason": "source_integrity",
                "mod_id": mid,
            }
            if exc.code:
                out["source_error_code"] = exc.code
            if exc.missing_files:
                out["missing_files"] = list(exc.missing_files)
            if exc.replacement_candidates:
                out["replacement_candidates"] = list(exc.replacement_candidates)
            is_missing = exc.code in {
                "no_deployable_source",
                "missing_files",
                "managed_missing",
            }
            if is_missing:
                out["is_missing_content"] = True
            return out

        # Absolute order: deploy declared dependencies first, then this Mod.
        stack = set(_deploy_stack or ())
        if mid not in stack:
            stack.add(mid)
            db = self._database()
            for dep_mid in self._dependency_mod_ids_for_deploy(mid):
                if not dep_mid or dep_mid == mid or dep_mid in stack:
                    continue
                if dep_mid.isdigit() and not db.is_mod_enabled(dep_mid):
                    logger.warning(
                        "%s skip disabled dependency dep_internal_id=%s",
                        log_prefix,
                        dep_mid,
                    )
                    continue
                if dep_mid.isdigit():
                    try:
                        dep_info = db.get_mod_deploy_info(dep_mid)
                    except Exception:  # noqa: BLE001
                        dep_info = None
                    dep_status = (
                        str(dep_info.deploy_status or "").strip().lower()
                        if dep_info is not None
                        else ""
                    )
                    if dep_status == DEPLOY_STATUS_DEPLOYED:
                        logger.info(
                            "%s skip already-deployed dependency "
                            "dep_internal_id=%s",
                            log_prefix,
                            dep_mid,
                        )
                        continue
                logger.info(
                    "%s deploy dependency first dep_internal_id=%s",
                    log_prefix,
                    dep_mid,
                )
                dep_out = self.deploy_mod(
                    dep_mid, _deploy_stack=frozenset(stack)
                )
                if not dep_out.get("success"):
                    err = (
                        f"依赖 Mod {dep_mid} 部署失败："
                        f"{dep_out.get('error') or 'unknown'}"
                    )
                    logger.warning(
                        "%s result=fail stage=early_gate reason=dependency "
                        "dep_internal_id=%s error=%s",
                        log_prefix,
                        dep_mid,
                        err,
                    )
                    if mid.isdigit():
                        self._mark_failed(mid, error=err)
                    return {
                        "success": False,
                        "error": err,
                        "mod_id": mid,
                        "dependency_mod_id": dep_mid,
                    }

        # Relationship warnings (hint only — never blocks, never auto-enables)
        relationship_warnings: list[dict[str, Any]] = []
        if mid.isdigit():
            try:
                relationship_warnings = (
                    self._database().check_relationship_deploy_warnings(mid)
                )
            except Exception:  # noqa: BLE001
                logger.exception("%s relationship warning check failed", log_prefix)
            for w in relationship_warnings:
                logger.warning("%s relation_warn=%s", log_prefix, w.get("message"))

        from services.deploy_stage_log import current_deploy_timing, deploy_stage

        with deploy_stage("resolve", internal_id=mid):
            ctx, early, cleanup = self._resolve_context(
                internal_id, require_target_exists=True, prepare_archives=True
            )
        sess = current_deploy_timing()
        if sess is not None:
            sess.diagnostics["extracted"] = cleanup is not None
        try:
            return self._deploy_with_context(
                mid=mid,
                log_prefix=log_prefix,
                ctx=ctx,
                early=early,
                relationship_warnings=relationship_warnings,
                skip_target_ownership_check=_skip_target_ownership_check,
            )
        finally:
            if cleanup is not None:
                from services.importers.archive import cleanup_import_cache

                cleanup_import_cache(cleanup)

    def _dependency_mod_ids_for_deploy(self, internal_id: str) -> list[str]:
        """
        Ordered dependency Internal IDs (``mods.mod_id`` PK).

        Sources:
        - ``mod_relationships`` (already stores PK)
        - ``metadata.json`` ``dependencies`` (workspace_id tokens)

        Metadata tokens are resolved via ``(platform, app_id, workspace_id)``
        only — never ``get_mod(token)`` as a cross-game PK lookup.
        """
        mid = str(internal_id or "").strip()
        if not mid or not mid.isdigit():
            return []
        db = self._database()
        ordered: list[str] = []
        seen: set[str] = set()

        def _push(candidate: str) -> None:
            cid = str(candidate or "").strip()
            if not cid or not cid.isdigit() or cid == mid or cid in seen:
                return
            seen.add(cid)
            ordered.append(cid)

        try:
            grouped = db.get_mod_relationships(mid)
            for item in grouped.get("dependencies") or []:
                _push(str(item.get("mod_id") or ""))
        except Exception:  # noqa: BLE001
            logger.debug(
                "dependency relation lookup failed internal_id=%s", mid, exc_info=True
            )

        # Scope for metadata workspace_id → PK (same game + platform only).
        owner_app_id = 0
        owner_platform = ""
        try:
            row = db.get_mod_backup_row(mid) or {}
            try:
                owner_app_id = int(row.get("app_id") or 0)
            except (TypeError, ValueError):
                owner_app_id = 0
            owner_platform = str(row.get("platform") or "").strip()
        except Exception:  # noqa: BLE001
            logger.debug(
                "dependency owner scope lookup failed internal_id=%s",
                mid,
                exc_info=True,
            )
        if not owner_platform:
            try:
                from core.mod_platform import normalize_platform

                info = db.get_mod_display_info(mid)
                if info is not None:
                    owner_platform = str(info.platform or "").strip()
                    if not owner_app_id:
                        try:
                            owner_app_id = int(info.app_id or 0)
                        except (TypeError, ValueError):
                            owner_app_id = 0
                owner_platform = normalize_platform(owner_platform)
            except Exception:  # noqa: BLE001
                owner_platform = ""

        source = resolve_deploy_managed_path(
            mid,
            db=db,
            library_root=self.library_root,
            file_manager=self.files,
        )
        if source is not None and owner_app_id > 0 and owner_platform:
            try:
                from core.mod_platform import normalize_platform
                from services.file_ops import read_info_metadata_dict

                data = read_info_metadata_dict(source) or {}
                # Prefer metadata platform when present (same as owner scope).
                meta_plat = normalize_platform(
                    str(data.get("platform") or owner_platform or "")
                )
                if not meta_plat:
                    meta_plat = normalize_platform(owner_platform)
                raw = data.get("dependencies") or []
                if isinstance(raw, (list, tuple)):
                    for entry in raw:
                        if isinstance(entry, dict):
                            token = str(
                                entry.get("workspace_id")
                                or entry.get("mod_id")
                                or entry.get("id")
                                or ""
                            ).strip()
                        else:
                            token = str(entry or "").strip()
                        if not token or not token.isdigit():
                            continue
                        # 1) workspace_id within (platform, app_id) → PK
                        pk = db.resolve_mod_id_by_scoped_workspace(
                            platform=meta_plat,
                            app_id=owner_app_id,
                            workspace_id=token,
                        )
                        if pk:
                            _push(pk)
                            continue
                        # 2) Legacy metadata may already store same-game PK.
                        #    Accept only when app_id matches — never cross-game.
                        try:
                            legacy = db.get_mod(token)
                        except Exception:  # noqa: BLE001
                            legacy = None
                        if legacy is None:
                            logger.warning(
                                "[DEPLOY] internal_id=%s unresolved dependency "
                                "token=%s platform=%s app_id=%s",
                                mid,
                                token,
                                meta_plat,
                                owner_app_id,
                            )
                            continue
                        try:
                            legacy_app = int(legacy.app_id or 0)
                        except (TypeError, ValueError):
                            legacy_app = 0
                        if legacy_app == owner_app_id:
                            _push(token)
                        else:
                            logger.warning(
                                "[DEPLOY] internal_id=%s skip cross-game "
                                "dependency token=%s token_app_id=%s "
                                "owner_app_id=%s",
                                mid,
                                token,
                                legacy_app,
                                owner_app_id,
                            )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "dependency metadata lookup failed internal_id=%s",
                    mid,
                    exc_info=True,
                )
        return ordered

    def _deploy_with_context(
        self,
        *,
        mid: str,
        log_prefix: str,
        ctx: DeployContext | None,
        early: dict[str, Any] | None,
        relationship_warnings: list[dict[str, Any]],
        skip_target_ownership_check: bool = False,
    ) -> dict[str, Any]:
        """
        Core Deploy pipeline for one Mod.

        strategy.plan() → FilePlan → Backup → apply_file_plan → verify_file_plan
        → Manifest. Execution must not go through Strategy.deploy.
        """
        if early is not None:
            logger.warning("%s result=fail error=%s", log_prefix, early.get("error"))
            if mid.isdigit():
                self._mark_failed(mid, error=str(early.get("error") or ""))
            out = dict(early)
            out["error"] = _normalize_deploy_error(str(early.get("error") or ""))
            return out
        assert ctx is not None

        # Absolute red line: custom deploy path skips ALL game strategies.
        if str(ctx.custom_deploy_path or "").strip():
            strategy: Any = CustomPathStrategy()
        else:
            strategy = resolve_strategy(ctx)
        if strategy is None:
            error = f"暂不支持的部署类型：{ctx.deploy_type}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            self._mark_failed(ctx.internal_id, app_id=ctx.app_id, error=error)
            return {
                "success": False,
                "error": error,
                "mod_id": ctx.internal_id,
                "supported": list(supported_deploy_types()),
            }

        from services.deploy_stage_log import current_deploy_timing, deploy_stage

        sess = current_deploy_timing()
        if sess is not None:
            sess.source = str(ctx.source or ctx.library_folder() or "")
            sess.archive_type = str(ctx.deploy_type or "")

        try:
            with deploy_stage("verify_source", internal_id=mid):
                anno_archives = []
                if ctx.deploy_type == DEPLOY_TYPE_ANNO_1800:
                    anno_archives = collect_deploy_archives(
                        ctx.internal_id, ctx.library_folder(), db=self._database()
                    )
                if anno_archives:
                    missing_zips = [p for p in anno_archives if not Path(p).is_file()]
                    if missing_zips:
                        raise DeploySourceError(
                            "压缩包源缺失："
                            + ", ".join(str(p) for p in missing_zips[:3])
                        )
                elif ctx.deploy_type in (DEPLOY_TYPE_WARHAMMER3, DEPLOY_TYPE_STELLARIS):
                    # Workshop-source activation does not copy the library folder.
                    pass
                else:
                    verify_deploy_source(
                        ctx.content_root(),
                        managed_path=ctx.library_folder(),
                        allowed_rel_paths=ctx.allowed_rel_paths,
                    )
        except DeploySourceError as exc:
            error = _normalize_deploy_error(str(exc))
            logger.warning(
                "%s result=failed reason=invalid_source error=%s",
                log_prefix,
                error,
            )
            self._mark_failed(ctx.internal_id, app_id=ctx.app_id, error=error)
            return {
                "success": False,
                "error": error,
                "reason": "invalid_source",
                "mod_id": ctx.internal_id,
                "deploy_type": ctx.deploy_type,
            }

        game_label = (
            "Palworld"
            if int(ctx.app_id) == PALWORLD_APP_ID
            else (ctx.config.name or f"App_{ctx.app_id}")
        )
        logger.info(
            "[DEPLOY] game=%s app_id=%s strategy=%s",
            game_label,
            ctx.app_id,
            type(strategy).__name__,
        )
        sess = current_deploy_timing()
        if sess is not None:
            sess.strategy = type(strategy).__name__

        # Conflict detection (warn only for overlapping file claims)
        conflicts_payload: dict[str, Any] | None = None
        with deploy_stage("plan", internal_id=mid, extra=f"strategy={type(strategy).__name__}"):
            planned = strategy.plan(ctx)
        sess = current_deploy_timing()
        if sess is not None and planned.success and planned.files:
            pack_n = 0
            pack_bytes = 0
            for entry in planned.files:
                src = Path(str(getattr(entry, "source", "") or ""))
                try:
                    if src.is_file() and src.suffix.lower() == ".pack":
                        pack_n += 1
                        pack_bytes += int(src.stat().st_size)
                except OSError:
                    continue
            sess.diagnostics["planned_files"] = len(planned.files)
            sess.diagnostics["pack_count"] = pack_n
            sess.diagnostics["pack_bytes"] = pack_bytes
        if (
            planned.success
            and not str(ctx.custom_deploy_path or "").strip()
            and ctx.deploy_type == DEPLOY_TYPE_WARHAMMER3
        ):
            from services.wh3_activation import is_wh3_activation_app

            if is_wh3_activation_app(ctx.app_id):
                return self._finish_wh3_activation_deploy(
                    ctx,
                    planned,
                    relationship_warnings,
                    log_prefix,
                )
        if (
            planned.success
            and not str(ctx.custom_deploy_path or "").strip()
            and ctx.deploy_type == DEPLOY_TYPE_STELLARIS
        ):
            from services.stellaris_activation import is_stellaris_activation_app

            if is_stellaris_activation_app(ctx.app_id):
                return self._finish_stellaris_activation_deploy(
                    ctx,
                    relationship_warnings,
                    log_prefix,
                )
        if planned.success and planned.files:
            try:
                workspace_roots = [
                    Path(ctx.library_folder()).resolve(),
                    Path(ctx.content_root()).resolve(),
                ]
                validate_planned_sources(
                    planned.files, workspace_roots=workspace_roots
                )
            except ManifestSecurityError as exc:
                error = f"部署计划未通过安全校验：{exc}"
                logger.warning("%s result=fail error=%s", log_prefix, error)
                self._mark_failed(ctx.internal_id, app_id=ctx.app_id, error=error)
                return {
                    "success": False,
                    "error": error,
                    "mod_id": ctx.internal_id,
                    "deploy_type": ctx.deploy_type,
                }
            conflicts_payload = None
            with deploy_stage(
                "conflict_scan",
                internal_id=mid,
                extra=f"files={len(planned.files)}",
            ):
                conflicts_payload = self.check_conflict_preview(
                    ctx.internal_id,
                    [e.target for e in planned.files],
                )
            if conflicts_payload and (
                conflicts_payload.get("overwrite") or conflicts_payload.get("conflict")
            ):
                logger.warning(
                    "%s overwrite=%s conflict=%s files=%s",
                    log_prefix,
                    bool(conflicts_payload.get("overwrite")),
                    bool(conflicts_payload.get("conflict")),
                    len(conflicts_payload.get("files") or []),
                )

        # Folder ownership: foreign trees are no longer hard-blocked.
        # Overwrite is allowed with BackupManager; ConflictDetector warns on
        # multi-mod target claims (warn-only, never blocks here).
        if (
            not skip_target_ownership_check
            and not str(ctx.custom_deploy_path or "").strip()
            and ctx.deploy_type == DEPLOY_TYPE_FOLDER_COPY
            and type(strategy).__name__ == "FolderCopyStrategy"
            and planned.success
        ):
            mod_path_raw = str(ctx.config.mod_path or "").strip()
            if mod_path_raw:
                kind = classify_folder_copy_target(
                    internal_id=ctx.internal_id,
                    managed=ctx.library_folder(),
                    mod_path=mod_path_raw,
                    library_root=self.library_root,
                )
                if kind == "foreign":
                    logger.warning(
                        "%s target_kind=foreign (backup-and-overwrite allowed)",
                        log_prefix,
                    )

        if not planned.success:
            error = _normalize_deploy_error(planned.error or "部署计划失败")
            logger.warning("%s result=fail stage=plan error=%s", log_prefix, error)
            self._mark_failed(ctx.internal_id, app_id=ctx.app_id, error=error)
            out = {
                "success": False,
                "error": error,
                "mod_id": ctx.internal_id,
                "deploy_type": ctx.deploy_type,
                "stage": "plan",
                "source": str(ctx.source or ctx.library_folder() or ""),
                "target": str(planned.target or ""),
                "planned_files": len(planned.files or []),
                "backed_up_files": 0,
                "applied_files": 0,
                "verified_files": 0,
                "failed_files": 0,
            }
            if relationship_warnings:
                out["relationship_warnings"] = relationship_warnings
            return out

        manifest_root = ctx.library_folder()
        backup_mgr = BackupManager(
            manifest_root, internal_id=str(ctx.internal_id or "").strip()
        )
        prep = None

        from services.deploy_apply import apply_file_plan
        from services.deploy_file_plan import (
            file_plan_core_applicable,
            file_plan_from_strategy_result,
            strategy_result_from_file_plan,
        )

        archive_hint: list[Path] = []
        if ctx.deploy_type == DEPLOY_TYPE_ANNO_1800:
            archive_hint = collect_deploy_archives(
                ctx.internal_id, ctx.library_folder(), db=self._database()
            )

        file_plan = None
        if planned.success:
            file_plan = file_plan_from_strategy_result(
                planned, ctx, archives=archive_hint
            )
            if sess is not None and file_plan.target_root:
                sess.target = file_plan.target_root
            if not file_plan.files:
                error = planned.error or "没有可部署的文件（FilePlan 为空）"
                logger.warning("%s result=fail error=%s", log_prefix, error)
                self._mark_failed(ctx.internal_id, app_id=ctx.app_id, error=error)
                out = {
                    "success": False,
                    "error": _normalize_deploy_error(error),
                    "mod_id": ctx.internal_id,
                    "deploy_type": ctx.deploy_type,
                    "stage": "plan",
                }
                out.update(_file_plan_result_fields(file_plan))
                if conflicts_payload:
                    out["conflicts"] = conflicts_payload
                if relationship_warnings:
                    out["relationship_warnings"] = relationship_warnings
                return out

        planned_targets: list[str] = []
        if file_plan is not None:
            planned_targets = file_plan.target_absolutes()
        elif planned.success and planned.files:
            planned_targets = [e.target for e in planned.files]

        try:
            with deploy_stage(
                "backup", internal_id=mid, extra=f"targets={len(planned_targets)}"
            ):
                prep = backup_mgr.prepare_overwrite(
                    planned_targets, mod_id=ctx.internal_id
                )
            sess = current_deploy_timing()
            if sess is not None:
                backed = 0
                if prep is not None:
                    backed = sum(1 for b in prep.by_target.values() if b is not None)
                sess.diagnostics["backup_happened"] = backed > 0
                sess.diagnostics["backup_files"] = backed
            if file_plan is not None:
                file_plan.diagnostics.backed_up_files = _count_backed_up(prep)
        except (OSError, BackupIntegrityError, BackupRestoreError) as exc:
            error = f"部署前备份原文件失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            self._mark_failed(ctx.internal_id, app_id=ctx.app_id, error=error)
            out = {
                "success": False,
                "error": error,
                "mod_id": ctx.internal_id,
                "deploy_type": ctx.deploy_type,
                "stage": "backup",
            }
            out.update(_file_plan_result_fields(file_plan))
            return out

        use_core_apply = bool(
            file_plan is not None and file_plan_core_applicable(file_plan)
        )
        if not use_core_apply:
            error = (
                "FilePlan 无法由 Core Apply 执行"
                + (
                    f"（planned={file_plan.diagnostics.planned_files}）"
                    if file_plan is not None
                    else ""
                )
            )
            logger.warning("%s result=fail stage=plan error=%s", log_prefix, error)
            self._mark_failed(ctx.internal_id, app_id=ctx.app_id, error=error)
            out = {
                "success": False,
                "error": _normalize_deploy_error(error),
                "mod_id": ctx.internal_id,
                "deploy_type": ctx.deploy_type,
                "stage": "plan",
            }
            out.update(_file_plan_result_fields(file_plan))
            if conflicts_payload:
                out["conflicts"] = conflicts_payload
            if relationship_warnings:
                out["relationship_warnings"] = relationship_warnings
            return out

        assert file_plan is not None
        result: Any
        try:
            has_extract = any(
                e.op == "extract_member" for e in file_plan.files if e.required
            )
            with deploy_stage(
                "copy",
                internal_id=mid,
                extra=f"fileplan=1 strategy={type(strategy).__name__}",
            ):
                if has_extract:
                    with deploy_stage("extract", internal_id=mid):
                        apply_out = apply_file_plan(file_plan)
                else:
                    apply_out = apply_file_plan(file_plan)
            sess = current_deploy_timing()
            if sess is not None:
                sess.files = int(getattr(apply_out, "copied_files", 0) or 0)
                sess.bytes = int(getattr(apply_out, "total_bytes", 0) or 0)
                sess.diagnostics["copied_bytes"] = sess.bytes
                sess.diagnostics["copied_files"] = sess.files
                from services.deploy_apply import current_apply_source_hashes

                sess.diagnostics["hashed_during_copy"] = len(
                    current_apply_source_hashes()
                )
            logger.info(
                "%s [DEPLOY_APPLY_DIAG] planned=%s copied=%s bytes=%s groups=%s",
                log_prefix,
                getattr(apply_out, "source_file_count", 0),
                getattr(apply_out, "copied_files", 0),
                getattr(apply_out, "total_bytes", 0),
                getattr(apply_out, "group_timings_ms", []),
            )
            if not apply_out.success:
                err = _normalize_deploy_error(apply_out.error)
                logger.warning(
                    "%s source=%s result=fail stage=apply error=%s "
                    "planned=%s applied=%s",
                    log_prefix,
                    ctx.source,
                    err,
                    file_plan.diagnostics.planned_files,
                    apply_out.applied,
                )
                self._abort_failed_deploy(
                    mid=ctx.internal_id,
                    app_id=ctx.app_id,
                    manifest_root=manifest_root,
                    backup_mgr=backup_mgr,
                    prep=prep,
                    error=err,
                )
                out = {
                    "success": False,
                    "error": err,
                    "mod_id": ctx.internal_id,
                    "deploy_type": ctx.deploy_type,
                    "stage": "apply",
                    "failed_files_detail": list(apply_out.failed_details),
                }
                out.update(_file_plan_result_fields(file_plan))
                if conflicts_payload:
                    out["conflicts"] = conflicts_payload
                if relationship_warnings:
                    out["relationship_warnings"] = relationship_warnings
                return out

            from services.deploy_txn import log_txn_phase

            log_txn_phase(
                PHASE_COPY_DONE,
                internal_id=ctx.internal_id,
                managed=manifest_root,
            )
            if prep is not None:
                try:
                    cur = resolve_from_transaction(backup_mgr.load_transaction())
                    persist_lifecycle_transaction(
                        backup_mgr,
                        DeploymentLifecycleState.COPYING,
                        current=None
                        if cur is DeploymentLifecycleState.CREATED
                        else cur,
                        targets=list(prep.by_target.keys()),
                        backups=[
                            {
                                "target": t,
                                "path": b.path,
                                "hash": b.hash,
                                "created_at": b.created_at,
                            }
                            for t, b in prep.by_target.items()
                            if b is not None
                        ],
                        mod_id=ctx.internal_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "deploy txn COPY_DONE phase write failed", exc_info=True
                    )

            with deploy_stage("validate", internal_id=mid):
                verify_out = verify_file_plan(file_plan)
            if not verify_out.success:
                err = _normalize_deploy_error(verify_out.error)
                logger.warning(
                    "%s source=%s result=fail stage=verify error=%s "
                    "planned=%s verified=%s",
                    log_prefix,
                    ctx.source,
                    err,
                    file_plan.diagnostics.planned_files,
                    verify_out.verified,
                )
                self._abort_failed_deploy(
                    mid=ctx.internal_id,
                    app_id=ctx.app_id,
                    manifest_root=manifest_root,
                    backup_mgr=backup_mgr,
                    prep=prep,
                    error=err,
                )
                out = {
                    "success": False,
                    "error": err,
                    "mod_id": ctx.internal_id,
                    "deploy_type": ctx.deploy_type,
                    "stage": "verify",
                    "missing_targets": list(verify_out.missing_targets),
                    "reason": "missing_targets",
                }
                out.update(_file_plan_result_fields(file_plan))
                if conflicts_payload:
                    out["conflicts"] = conflicts_payload
                if relationship_warnings:
                    out["relationship_warnings"] = relationship_warnings
                return out

            when = _utc_deploy_time()
            result = strategy_result_from_file_plan(
                file_plan, deploy_time=when, success=True
            )
        except Exception as exc:
            logger.exception("%s apply/deploy raised", log_prefix)
            self._abort_failed_deploy(
                mid=ctx.internal_id,
                app_id=ctx.app_id,
                manifest_root=manifest_root,
                backup_mgr=backup_mgr,
                prep=prep,
                error=str(exc),
            )
            raise

        if not result.success:
            err = _normalize_deploy_error(result.error)
            logger.warning(
                "%s source=%s result=fail error=%s",
                log_prefix,
                ctx.source,
                err,
            )
            self._abort_failed_deploy(
                mid=ctx.internal_id,
                app_id=ctx.app_id,
                manifest_root=manifest_root,
                backup_mgr=backup_mgr,
                prep=prep,
                error=err,
            )
            out = {
                "success": False,
                "error": err,
                "mod_id": ctx.internal_id,
                "deploy_type": ctx.deploy_type,
                "stage": "apply",
            }
            out.update(_file_plan_result_fields(file_plan))
            if conflicts_payload:
                out["conflicts"] = conflicts_payload
            if relationship_warnings:
                out["relationship_warnings"] = relationship_warnings
            return out

        assert result.manifest is not None
        validated_count = int(
            file_plan.diagnostics.verified_files or result.copied_files
        )
        if prep is not None:
            backup_mgr.apply_to_manifest(result.manifest, prep)

        # DEPLOYED is written only after transaction commit (persist/mark_deployed).
        # Writing it before hash/manifest left backup_done txn + DB success, so
        # startup recover could roll back and overwrite the real error.
        db_warning: str | None = None

        try:
            with deploy_stage("hash", internal_id=mid):
                backup_mgr.validate_manifest_backups(result.manifest)
                enrich_manifest_fingerprint(
                    result.manifest,
                    source=manifest_root,
                    managed=manifest_root,
                )
                from services.mod_source_integrity import enrich_manifest_source_hashes

                enrich_manifest_source_hashes(result.manifest)
            with deploy_stage("manifest", internal_id=mid):
                try:
                    attach_canonical_targets(result.manifest, ctx)
                except DeployPathError as exc:
                    raise ManifestSecurityError(str(exc)) from exc
                planned_abs = (
                    planned_absolute_targets(planned.files)
                    if planned.success and planned.files
                    else None
                )
                validate_manifest_for_save(
                    result.manifest,
                    managed=manifest_root,
                    ctx=ctx,
                    planned_targets=planned_abs,
                )
                save_manifest(manifest_root, result.manifest)
                from services.deploy_txn import log_txn_phase

                log_txn_phase(
                    PHASE_MANIFEST_DONE,
                    internal_id=ctx.internal_id,
                    managed=manifest_root,
                )
                if prep is not None:
                    try:
                        cur = resolve_from_transaction(backup_mgr.load_transaction())
                        persist_lifecycle_transaction(
                            backup_mgr,
                            DeploymentLifecycleState.VERIFYING,
                            current=None
                            if cur is DeploymentLifecycleState.CREATED
                            else cur,
                            targets=list(prep.by_target.keys()),
                            backups=[
                                {
                                    "target": t,
                                    "path": b.path,
                                    "hash": b.hash,
                                    "created_at": b.created_at,
                                }
                                for t, b in prep.by_target.items()
                                if b is not None
                            ],
                            mod_id=ctx.internal_id,
                        )
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "deploy txn MANIFEST_DONE phase write failed",
                            exc_info=True,
                        )
            with deploy_stage("persist", internal_id=mid):
                if prep is not None:
                    backup_mgr.mark_deployed(prep, mod_id=ctx.internal_id)
                # Transaction committed ⇒ deployment success (DB DEPLOYED).
                try:
                    self._database().update_mod_deploy_status(
                        ctx.internal_id,
                        deploy_status=DEPLOY_STATUS_DEPLOYED,
                        deploy_path=result.target,
                        deploy_time=result.deploy_time,
                        deploy_error="",
                        app_id=ctx.app_id,
                    )
                    try:
                        from services.mod_projection_events import notify_mod_changed

                        notify_mod_changed(ctx.internal_id)
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "notify_mod_changed after deploy failed internal_id=%s",
                            ctx.internal_id,
                            exc_info=True,
                        )
                    try:
                        from services.mod_fs_observer import touch_observation_stamp

                        touch_observation_stamp(
                            ctx.internal_id,
                            managed_path=getattr(ctx, "managed_path", None)
                            or manifest_root,
                        )
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "fs observation stamp after deploy failed internal_id=%s",
                            ctx.internal_id,
                            exc_info=True,
                        )
                except Exception as exc:  # noqa: BLE001
                    db_warning = "database_update_failed"
                    logger.warning(
                        "[DEPLOY] database status update failed after txn commit "
                        "internal_id=%s error=%s",
                        ctx.internal_id,
                        exc,
                    )
                from services.deploy_txn import unregister_active_deploy_transaction

                unregister_active_deploy_transaction(manifest_root)
        except OSError as exc:
            error = f"文件已复制，但写入部署清单失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            self._abort_failed_deploy(
                mid=ctx.internal_id,
                app_id=ctx.app_id,
                manifest_root=manifest_root,
                backup_mgr=backup_mgr,
                prep=prep,
                error=error,
            )
            out = {"success": False, "error": error, "mod_id": ctx.internal_id, "stage": "manifest"}
            out.update(_file_plan_result_fields(file_plan))
            return out
        except (ManifestSecurityError, DeployPathError, BackupIntegrityError) as exc:
            error = f"部署清单校验失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            self._abort_failed_deploy(
                mid=ctx.internal_id,
                app_id=ctx.app_id,
                manifest_root=manifest_root,
                backup_mgr=backup_mgr,
                prep=prep,
                error=error,
            )
            out = {"success": False, "error": error, "mod_id": ctx.internal_id, "stage": "manifest"}
            out.update(_file_plan_result_fields(file_plan))
            return out
        except Exception as exc:  # noqa: BLE001 — integrity / path escape
            error = f"部署清单校验失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            self._abort_failed_deploy(
                mid=ctx.internal_id,
                app_id=ctx.app_id,
                manifest_root=manifest_root,
                backup_mgr=backup_mgr,
                prep=prep,
                error=error,
            )
            out = {"success": False, "error": error, "mod_id": ctx.internal_id, "stage": "manifest"}
            out.update(_file_plan_result_fields(file_plan))
            return out

        # ARCHITECTURE RULE: Deploy must not write user conflict annotation.
        # Post-deploy ConflictDetector persist was deleted (no-op stub remains).
        _schedule_post_deploy_conflict_scan(
            self.library_root,
            db=self._database(),
            log_prefix=log_prefix,
        )

        file_details = build_deploy_result_files(result.manifest)
        file_labels = _deploy_result_file_labels(
            file_details, target_root=str(result.target or "")
        )
        files_log = "\n".join(f"- {name}" for name in file_labels) if file_labels else ""
        if files_log:
            logger.info(
                "%s source=%s target=%s type=%s result=ok copied=%s validated=%s\n"
                "files:\n%s",
                log_prefix,
                ctx.source,
                result.target,
                result.deploy_type,
                result.copied_files,
                validated_count,
                files_log,
            )
        else:
            logger.info(
                "%s source=%s target=%s type=%s result=ok copied=%s validated=%s",
                log_prefix,
                ctx.source,
                result.target,
                result.deploy_type,
                result.copied_files,
                validated_count,
            )
        out = {
            "success": True,
            "mod_id": ctx.internal_id,
            "source": str(ctx.source or ctx.library_folder() or ""),
            "target": result.target,
            "managed_path": str(ctx.library_folder() or ""),
            "copied_files": result.copied_files,
            "validated": validated_count,
            "files": file_details,
            "deploy_type": result.deploy_type,
            "deploy_time": result.deploy_time,
            "deployment_status": "deployed",
        }
        out.update(_file_plan_result_fields(file_plan))
        if file_plan is not None and file_plan.target_root:
            out["target"] = file_plan.target_root
        if db_warning:
            out["warning"] = db_warning
        if conflicts_payload:
            out["conflicts"] = conflicts_payload
        if relationship_warnings:
            out["relationship_warnings"] = relationship_warnings
        return out

    def _finish_wh3_activation_deploy(
        self,
        ctx: DeployContext,
        planned: Any,
        relationship_warnings: list[dict[str, Any]],
        log_prefix: str,
    ) -> dict[str, Any]:
        """WH3 deploy: record status + used_mods.txt. Never copy packs to data."""
        from services.wh3_activation import (
            Wh3ModRef,
            complete_wh3_deploy_activation,
            load_wh3_game_paths,
            prepare_wh3_workshop_packs,
            _row_to_ref,
        )
        from services.deploy_rules.warhammer3 import Warhammer3Strategy

        library = ctx.library_folder()
        paths = load_wh3_game_paths(self._database())
        ref = None
        try:
            rows = self._database().list_mod_list_items(mod_id=ctx.internal_id)
            if rows:
                ref = _row_to_ref(rows[0])
        except Exception:  # noqa: BLE001
            ref = None
        if ref is None:
            ref = Wh3ModRef(
                internal_id=str(ctx.internal_id),
                workspace_id=str(ctx.workspace_id or ""),
                enabled=True,
                last_known_path=str(library),
                deploy_status="",
                title="",
            )
        pack_lines, prepare_error = prepare_wh3_workshop_packs(
            ref,
            workshop_path=paths.workshop_path,
            data_folder=paths.data_folder,
            extract=True,
            db=self._database(),
        )
        if prepare_error or not pack_lines:
            error = prepare_error or "战锤 III Mod 部署失败：未找到 .pack 文件"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            self._mark_failed(ctx.internal_id, app_id=ctx.app_id, error=error)
            return {
                "success": False,
                "error": error,
                "mod_id": ctx.internal_id,
                "deploy_type": ctx.deploy_type,
                "copied_files": 0,
            }

        workshop_target = pack_lines[0].directory
        old = load_manifest(library, expected_internal_id=ctx.internal_id)
        if old is not None and getattr(old, "files", None):
            try:
                Warhammer3Strategy().undeploy(ctx, old)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "%s leftover WH3 data-copy cleanup failed",
                    log_prefix,
                    exc_info=True,
                )
        try:
            delete_manifest(library)
        except Exception:  # noqa: BLE001
            logger.debug(
                "%s delete old WH3 copy manifest failed", log_prefix, exc_info=True
            )

        when = _utc_deploy_time()
        db_warning: str | None = None
        try:
            self._database().update_mod_deploy_status(
                ctx.internal_id,
                deploy_status=DEPLOY_STATUS_DEPLOYED,
                deploy_path=str(workshop_target),
                deploy_time=when,
                deploy_error="",
                app_id=ctx.app_id,
            )
            try:
                from services.mod_projection_events import notify_mod_changed

                notify_mod_changed(ctx.internal_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "notify_mod_changed after WH3 deploy failed internal_id=%s",
                    ctx.internal_id,
                    exc_info=True,
                )
            try:
                from services.mod_fs_observer import touch_observation_stamp

                touch_observation_stamp(
                    ctx.internal_id,
                    managed_path=getattr(ctx, "managed_path", None) or library,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "fs observation stamp after WH3 deploy failed internal_id=%s",
                    ctx.internal_id,
                    exc_info=True,
                )
        except Exception as exc:  # noqa: BLE001
            db_warning = "database_update_failed"
            logger.warning(
                "[DEPLOY] WH3 database status update failed internal_id=%s error=%s",
                ctx.internal_id,
                exc,
            )
        try:
            complete_wh3_deploy_activation(
                ctx.internal_id,
                db=self._database(),
                library_root=self.library_root,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "%s WH3 used_mods / load-order persist failed",
                log_prefix,
                exc_info=True,
            )

        pack_names = [line.pack_name for line in pack_lines]
        logger.info(
            "%s source=%s target=%s type=%s result=ok copied=0 packs=%s",
            log_prefix,
            library,
            workshop_target,
            ctx.deploy_type,
            pack_names,
        )
        out: dict[str, Any] = {
            "success": True,
            "mod_id": ctx.internal_id,
            "source": str(library),
            "target": str(workshop_target),
            "managed_path": str(library),
            "copied_files": 0,
            "validated": len(pack_lines),
            "files": pack_names,
            "deploy_type": ctx.deploy_type,
            "deploy_time": when,
            "deployment_status": "deployed",
            "planned_files": len(planned.files or []),
            "backed_up_files": 0,
            "applied_files": 0,
            "verified_files": 0,
            "failed_files": 0,
        }
        if db_warning:
            out["warning"] = db_warning
        if relationship_warnings:
            out["relationship_warnings"] = relationship_warnings
        return out

    def _undeploy_wh3_activation(
        self,
        ctx: DeployContext,
        log_prefix: str,
    ) -> dict[str, Any]:
        """Clear WH3 deploy status without deleting library packs."""
        from services.wh3_activation import complete_wh3_undeploy_activation
        from services.deploy_rules.warhammer3 import Warhammer3Strategy

        manifest_root = ctx.library_folder()
        manifest = load_manifest(manifest_root, expected_internal_id=ctx.internal_id)
        strategy = Warhammer3Strategy()
        result = strategy.undeploy(ctx, manifest)
        if not result.success:
            logger.warning("%s result=fail error=%s", log_prefix, result.error)
            return {
                "success": False,
                "error": result.error,
                "mod_id": ctx.internal_id,
            }
        delete_manifest(manifest_root)
        try:
            self._database().update_mod_deploy_status(
                ctx.internal_id,
                deploy_status=DEPLOY_STATUS_NOT_DEPLOYED,
                deploy_path="",
                deploy_time="",
                deploy_error="",
                app_id=ctx.app_id,
            )
            try:
                from services.mod_projection_events import notify_mod_changed

                notify_mod_changed(ctx.internal_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "notify_mod_changed after WH3 undeploy failed internal_id=%s",
                    ctx.internal_id,
                    exc_info=True,
                )
        except Exception as exc:  # noqa: BLE001
            error = f"更新部署状态失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            return {"success": False, "error": error, "mod_id": ctx.internal_id}
        try:
            complete_wh3_undeploy_activation(
                ctx.internal_id,
                db=self._database(),
                library_root=self.library_root,
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "%s WH3 used_mods persist after undeploy failed",
                log_prefix,
                exc_info=True,
            )
        logger.info(
            "%s source=%s result=ok removed=%s",
            log_prefix,
            ctx.source,
            result.copied_files,
        )
        return {
            "success": True,
            "mod_id": ctx.internal_id,
            "removed_files": result.copied_files,
            "deploy_type": result.deploy_type or ctx.deploy_type,
        }

    def _finish_stellaris_activation_deploy(
        self,
        ctx: DeployContext,
        relationship_warnings: list[dict[str, Any]],
        log_prefix: str,
    ) -> dict[str, Any]:
        """Stellaris deploy: record status + sync launcher enable/order. Never copy."""
        from services.stellaris_activation import (
            persist_load_order,
            load_saved_order,
            sync_stellaris_launcher,
        )

        library = ctx.library_folder()
        when = _utc_deploy_time()
        db_warning: str | None = None
        try:
            self._database().update_mod_deploy_status(
                ctx.internal_id,
                deploy_status=DEPLOY_STATUS_DEPLOYED,
                deploy_path=str(library),
                deploy_time=when,
                deploy_error="",
                app_id=ctx.app_id,
            )
            try:
                self._database().enable_mod(ctx.internal_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Stellaris enable after deploy failed internal_id=%s",
                    ctx.internal_id,
                    exc_info=True,
                )
            try:
                from services.mod_projection_events import notify_mod_changed

                notify_mod_changed(ctx.internal_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "notify_mod_changed after Stellaris deploy failed internal_id=%s",
                    ctx.internal_id,
                    exc_info=True,
                )
        except Exception as exc:  # noqa: BLE001
            db_warning = "database_update_failed"
            logger.warning(
                "[DEPLOY] Stellaris database status update failed internal_id=%s error=%s",
                ctx.internal_id,
                exc,
            )
        try:
            persist_load_order(load_saved_order(), db=self._database())
            sync_stellaris_launcher(db=self._database())
        except Exception:  # noqa: BLE001
            logger.warning(
                "%s Stellaris launcher sync after deploy failed",
                log_prefix,
                exc_info=True,
            )
        logger.info(
            "%s source=%s target=%s type=%s result=ok copied=0",
            log_prefix,
            library,
            library,
            ctx.deploy_type,
        )
        out: dict[str, Any] = {
            "success": True,
            "mod_id": ctx.internal_id,
            "source": str(library),
            "target": str(library),
            "managed_path": str(library),
            "copied_files": 0,
            "validated": 0,
            "files": [],
            "deploy_type": ctx.deploy_type,
            "deploy_time": when,
            "deployment_status": "deployed",
            "planned_files": 0,
            "backed_up_files": 0,
            "applied_files": 0,
            "verified_files": 0,
            "failed_files": 0,
        }
        if db_warning:
            out["warning"] = db_warning
        if relationship_warnings:
            out["relationship_warnings"] = relationship_warnings
        return out

    def _undeploy_stellaris_activation(
        self,
        ctx: DeployContext,
        log_prefix: str,
    ) -> dict[str, Any]:
        """Clear Stellaris deploy status without deleting Workshop or library files."""
        from services.stellaris_activation import persist_load_order, load_saved_order, sync_stellaris_launcher

        try:
            self._database().update_mod_deploy_status(
                ctx.internal_id,
                deploy_status=DEPLOY_STATUS_NOT_DEPLOYED,
                deploy_path="",
                deploy_time="",
                deploy_error="",
                app_id=ctx.app_id,
            )
            try:
                from services.mod_projection_events import notify_mod_changed

                notify_mod_changed(ctx.internal_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "notify_mod_changed after Stellaris undeploy failed internal_id=%s",
                    ctx.internal_id,
                    exc_info=True,
                )
        except Exception as exc:  # noqa: BLE001
            error = f"更新部署状态失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            return {"success": False, "error": error, "mod_id": ctx.internal_id}
        try:
            persist_load_order(load_saved_order(), db=self._database())
            sync_stellaris_launcher(db=self._database())
        except Exception:  # noqa: BLE001
            logger.warning(
                "%s Stellaris launcher sync after undeploy failed",
                log_prefix,
                exc_info=True,
            )
        logger.info("%s source=%s result=ok removed=0", log_prefix, ctx.source)
        return {
            "success": True,
            "mod_id": ctx.internal_id,
            "removed_files": 0,
            "deploy_type": ctx.deploy_type,
        }

    def deployment_status(self, internal_id: int | str) -> str:
        """Phase 8 runtime deployment_status (not content_status)."""
        return resolve_deployment_status(
            internal_id,
            library_root=self.library_root,
            db=self._database(),
        )

    def check_conflict_preview(
        self,
        internal_id: int | str,
        planned_targets: list[str | Path],
    ) -> dict[str, Any] | None:
        """
        Pre-deploy path-overlap preview. Never blocks deploy.

        FILE_OVERWRITE is a diagnostic (``overwrite=True``). ``conflict=True``
        only when a user-declared relationship is present. Path overlap never
        sets ``status="conflict"``.
        """
        mid = str(internal_id).strip()
        report = ConflictDetector(
            self.library_root, db=self._database()
        ).preview_targets(mid, list(planned_targets))
        if not report.conflicts:
            return None
        files = []
        for entry in report.conflicts:
            others = [m for m in entry.mods if m != mid]
            files.append(
                {
                    "target": entry.file,
                    "existing_mod": others[0] if others else "",
                }
            )
        overwrite = any(
            c.conflict_type == "FILE_OVERWRITE" for c in report.conflicts
        )
        relationship = any(
            c.conflict_type == "RELATIONSHIP" for c in report.conflicts
        )
        return {
            "conflict": relationship,
            "overwrite": overwrite,
            "status": report.status,
            "conflicts": [c.as_dict() for c in report.conflicts],
            "files": files,
        }

    def undeploy_mod(self, internal_id: int | str) -> dict[str, Any]:
        """Remove files listed in deploy_manifest and clear DB status."""
        from services.deploy_lock import deploy_operation_lock
        from services.deploy_result import normalize_deploy_dict, terminal_failed

        mid = resolve_deploy_identity(internal_id, db=self._database())
        log_prefix = f"[UNDEPLOY] internal_id={mid}"
        try:
            with deploy_operation_lock(mid):
                out = self._undeploy_mod_body(mid)
                return _finalize_deploy_dict(out, log_prefix=log_prefix)
        except RuntimeError as exc:
            msg = str(exc)
            if "已有部署任务" in msg:
                return normalize_deploy_dict(
                    terminal_failed(
                        msg,
                        internal_id=mid,
                        error_code="deploy_in_progress",
                    )
                )
            raise

    def _undeploy_mod_body(self, internal_id: int | str) -> dict[str, Any]:
        """
        Remove files listed in ``deploy_manifest.json`` and clear DB status.

        Never deletes an entire target directory tree — only manifest targets.
        """
        mid = resolve_deploy_identity(internal_id, db=self._database())
        log_prefix = f"[UNDEPLOY] internal_id={mid}"

        ctx, early, _cleanup = self._resolve_context(
            mid, prepare_archives=False, for_undeploy=True
        )
        if early is not None:
            logger.warning("%s result=fail error=%s", log_prefix, early.get("error"))
            out = dict(early)
            out["error"] = _normalize_deploy_error(str(early.get("error") or ""))
            return out
        assert ctx is not None

        if (
            not str(ctx.custom_deploy_path or "").strip()
            and ctx.deploy_type == DEPLOY_TYPE_WARHAMMER3
        ):
            from services.wh3_activation import is_wh3_activation_app

            if is_wh3_activation_app(ctx.app_id):
                return self._undeploy_wh3_activation(ctx, log_prefix)
        if (
            not str(ctx.custom_deploy_path or "").strip()
            and ctx.deploy_type == DEPLOY_TYPE_STELLARIS
        ):
            from services.stellaris_activation import is_stellaris_activation_app

            if is_stellaris_activation_app(ctx.app_id):
                return self._undeploy_stellaris_activation(ctx, log_prefix)

        manifest_root = ctx.library_folder()
        manifest = load_manifest(manifest_root, expected_internal_id=mid)

        if str(ctx.custom_deploy_path or "").strip():
            strategy = CustomPathStrategy()
        elif manifest is not None and str(manifest.deploy_type or "").strip():
            strategy = get_strategy(manifest.deploy_type, app_id=ctx.app_id)
            if strategy is None:
                strategy = get_strategy(DEPLOY_TYPE_FOLDER_COPY, app_id=0)
        else:
            strategy = resolve_strategy(ctx)
            if strategy is None:
                strategy = get_strategy(DEPLOY_TYPE_FOLDER_COPY, app_id=0)
        assert strategy is not None

        # Phase 8: refuse undeploy when ownership cannot be confirmed
        if manifest is None:
            # Distinguish pollution (file present, wrong mod_id) from missing
            raw_probe = load_manifest(manifest_root)
            claimed = ""
            if raw_probe is not None:
                claimed = str(
                    raw_probe.internal_id or raw_probe.mod_id or ""
                ).strip()
            if raw_probe is not None and claimed not in ("", mid):
                error = DEPLOY_ERR_UNDEPLOY_MISMATCH
                logger.warning(
                    "%s result=fail error=%s (manifest pollution)",
                    log_prefix,
                    error,
                )
                return {"success": False, "error": error, "mod_id": mid}
            info = self._database().get_mod_deploy_info(mid) if mid.isdigit() else None
            deploy_path = str(info.deploy_path or "").strip() if info else ""
            if deploy_path:
                from services.deploy_fs import safe_has_any_file

                target = Path(deploy_path)
                if target.exists() and safe_has_any_file(target):
                    error = DEPLOY_ERR_UNDEPLOY_MISMATCH
                    logger.warning("%s result=fail error=%s", log_prefix, error)
                    return {"success": False, "error": error, "mod_id": mid}
        else:
            try:
                validate_manifest_mod_id(manifest, mid)
                remap_manifest_targets(manifest, ctx)
                validate_manifest_targets(
                    manifest,
                    allowed_roots=collect_allowed_target_roots(ctx),
                )
            except (ManifestSecurityError, DeployPathError) as exc:
                error = f"取消部署中止：清单未通过安全校验 — {exc}"
                logger.warning("%s result=fail error=%s", log_prefix, error)
                return {"success": False, "error": error, "mod_id": mid}

        # Manifest from a prior custom deploy still undeploys via CustomPathStrategy.
        if (
            manifest is not None
            and str(manifest.deploy_type or "") == DEPLOY_TYPE_CUSTOM_PATH
            and not str(ctx.custom_deploy_path or "").strip()
        ):
            strategy = CustomPathStrategy()

        backup_mgr = BackupManager(
            manifest_root, internal_id=str(ctx.internal_id or "").strip()
        )
        # Preflight: refuse undeploy when a required backup is missing/corrupt
        # so we never silently delete targets that cannot be restored.
        if manifest is not None:
            try:
                for entry in manifest.files:
                    if entry.backup is None:
                        continue
                    backup_mgr.verify_backup_hash(entry.backup)
            except BackupIntegrityError as exc:
                logger.error("%s backup preflight failed: %s", log_prefix, exc)
                return {
                    "success": False,
                    "error": f"取消部署中止：备份校验失败（未删除已部署文件）— {exc}",
                    "mod_id": ctx.internal_id,
                }

        with prune_protection(collect_protected_roots(ctx)):
            result = strategy.undeploy(ctx, manifest)
        if not result.success:
            logger.warning("%s result=fail error=%s", log_prefix, result.error)
            return {
                "success": False,
                "error": result.error,
                "mod_id": ctx.internal_id,
            }

        # After strategy removes Mod files, restore any pre-overwrite originals.
        if manifest is not None:
            try:
                restored = backup_mgr.restore_from_manifest(manifest)
                if restored:
                    logger.info(
                        "%s restored_originals=%s", log_prefix, restored
                    )
            except BackupRestoreError as exc:
                logger.error("%s restore failed: %s", log_prefix, exc)
                return {
                    "success": False,
                    "error": f"取消部署时恢复原文件失败：{exc}",
                    "mod_id": ctx.internal_id,
                    "restore_failures": list(exc.failures),
                }
            except Exception as exc:  # noqa: BLE001
                logger.exception("%s restore from backup failed", log_prefix)
                return {
                    "success": False,
                    "error": f"取消部署时恢复原文件失败：{exc}",
                    "mod_id": ctx.internal_id,
                }
            try:
                backup_mgr.cleanup_backups()
            except Exception:  # noqa: BLE001
                logger.exception("%s backup cleanup failed", log_prefix)

        delete_manifest(manifest_root)
        try:
            self._database().update_mod_deploy_status(
                ctx.internal_id,
                deploy_status=DEPLOY_STATUS_NOT_DEPLOYED,
                deploy_path="",
                deploy_time="",
                deploy_error="",
                app_id=ctx.app_id,
            )
            try:
                from services.mod_projection_events import notify_mod_changed

                notify_mod_changed(ctx.internal_id)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "notify_mod_changed after undeploy failed internal_id=%s",
                    ctx.internal_id,
                    exc_info=True,
                )
            try:
                from services.mod_fs_observer import touch_observation_stamp

                touch_observation_stamp(
                    ctx.internal_id,
                    managed_path=manifest_root,
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "fs observation stamp after undeploy failed internal_id=%s",
                    ctx.internal_id,
                    exc_info=True,
                )
        except Exception as exc:  # noqa: BLE001
            error = f"文件已移除，但更新部署状态失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            return {"success": False, "error": error, "mod_id": ctx.internal_id}

        _schedule_post_deploy_conflict_scan(
            self.library_root,
            db=self._database(),
            log_prefix=log_prefix,
        )

        logger.info(
            "%s source=%s result=ok removed=%s",
            log_prefix,
            ctx.source,
            result.copied_files,
        )
        return {
            "success": True,
            "mod_id": ctx.internal_id,
            "removed_files": result.copied_files,
            "deploy_type": result.deploy_type or ctx.deploy_type,
        }

    def redeploy_mod(self, internal_id: int | str) -> dict[str, Any]:
        """
        Redeploy ≡ undeploy (old manifest) + deploy (new files + new manifest).

        Aborts if undeploy fails, so removed source files cannot linger.
        """
        mid = resolve_deploy_identity(internal_id, db=self._database())
        und = self.undeploy_mod(mid)
        if not und.get("success"):
            err = str(und.get("error") or "取消部署失败")
            # Persist failed redeploy reason when we still have a source
            if mid.isdigit() and "源 Mod 目录不存在" not in err:
                self._mark_failed(mid, error=f"重新部署中止：{err}")
            return {
                "success": False,
                "error": f"重新部署中止：取消部署未完成 — {err}",
                "mod_id": mid,
                "undeploy": und,
            }
        return self.deploy_mod(mid, _skip_target_ownership_check=True)
