"""Deploy managed Mods into a game's configured paths (strategy-based)."""

from __future__ import annotations

import hashlib
import logging
import shutil
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
from services.conflict import ConflictDetector
from services.deploy_errors import DeploySourceError, DeployValidationError
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
)
from services.file_ops import (
    ModFileManager,
    clear_missing_content_if_present,
    read_is_missing_content,
)
from services.metadata_backup import is_mod_folder_absent

logger = logging.getLogger(__name__)

MISSING_CONTENT_DEPLOY_ERROR = DEPLOY_BLOCKED_CONTENT_MISSING


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
    dest.mkdir(parents=True, exist_ok=True)
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


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
    from services.importers.archive import is_archive_path
    from services.file_ops import INFO_DIR_NAME, LEGACY_INFO_DIR_NAME

    skip_dirs = {INFO_DIR_NAME, LEGACY_INFO_DIR_NAME, "历史版本"}
    files: list[Path] = []
    for path in managed.rglob("*"):
        if not path.is_file():
            continue
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
    mod_id: int | str,
    managed: Path,
    *,
    db: DatabaseManager | None = None,
) -> list[Path]:
    """Resolve archive files that should be extracted for deploy (not copied as-is)."""
    database = db if db is not None else get_db()
    bundle = database.get_mod_files(mod_id)
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
    mod_id: int | str,
    *,
    db: DatabaseManager | None = None,
) -> bool:
    """
    True when archive extract must keep wrapper folders (Stardew SMAPI).

    Generic ``find_mod_root`` strips a single outer folder, which would break
    Stardew case-2 / multi-mod layout detection via ``manifest.json``.
    """
    try:
        mid = str(mod_id).strip()
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
    include_hash: bool = True,
) -> list[dict[str, Any]]:
    """
    Build runtime DeployResult ``files`` entries from the final manifest.

    Does not change on-disk manifest format — this list is only returned to
    callers / logged. Each item: ``source``, ``target``, ``size``, and
    optional ``hash`` (sha256 of the target file when readable).
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
    from services.importers.archive import (
        cleanup_import_cache,
        extract_archive,
        find_mod_root,
        import_cache_root,
    )

    stage = import_cache_root() / f"deploy_{uuid.uuid4().hex}"
    content = stage / "content"
    content.mkdir(parents=True, exist_ok=False)
    try:
        for archive in archive_paths:
            extract_dir = extract_archive(
                archive, dest_dir=stage / f"ex_{uuid.uuid4().hex[:8]}"
            )
            root = _choose_archive_extract_root(
                extract_dir,
                preserve_extract_layout=preserve_extract_layout,
                custom_deploy_path=custom_deploy_path,
                find_mod_root_fn=find_mod_root,
            )
            _merge_tree(root, content)
        if include_other_plain:
            for path in _iter_plain_managed_files(managed, skip_archives=True):
                rel = path.relative_to(managed)
                dest = content / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
        if not any(content.rglob("*")):
            cleanup_import_cache(stage)
            raise ValueError("压缩包解压后没有可部署的文件")
        return content, stage
    except Exception:
        cleanup_import_cache(stage)
        raise


def prepare_deploy_content(
    mod_id: int | str,
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
        extract_archive,
        find_mod_root,
        import_cache_root,
    )

    database = db if db is not None else get_db()
    managed = Path(managed_source)
    preserve_layout = _preserve_extract_layout_for_mod(mod_id, db=database)

    custom_path = str(custom_deploy_path or "").strip()
    if not custom_path:
        try:
            display = database.get_mod_display_info(mod_id)
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
        allowed = resolve_deploy_sources(mod_id, managed, db=database)
        if not allowed:
            allowed = plain_content_allow_list(managed)
        if not allowed:
            raise DeploySourceError(reason)
        logger.warning(
            "Archive unavailable; using validated managed loose content "
            "mod_id=%s files=%s reason=%s",
            mod_id,
            len(allowed),
            reason,
        )
        return managed, allowed, None

    bundle = database.get_mod_files(mod_id)
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
        return managed, resolve_deploy_sources(mod_id, managed, db=database), None

    stage = import_cache_root() / f"deploy_{uuid.uuid4().hex}"
    content = stage / "content"
    content.mkdir(parents=True, exist_ok=False)
    try:
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
            extract_dir = extract_archive(
                archive, dest_dir=stage / f"ex_{uuid.uuid4().hex[:8]}"
            )
            root = _choose_archive_extract_root(
                extract_dir,
                preserve_extract_layout=preserve_layout,
                custom_deploy_path=custom_path,
                find_mod_root_fn=find_mod_root,
            )
            _merge_tree(root, content)

        for entry in plain_entries:
            rel = (entry.path or entry.filename or "").replace("\\", "/").strip().lstrip("./")
            if not rel:
                continue
            src = managed / rel
            if not src.is_file():
                src = managed / Path(rel).name
            if not src.is_file():
                continue
            dest = content / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

        if not any(content.rglob("*")):
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
    mod_id: int | str,
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
    bundle = database.get_mod_files(mod_id)
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
    low = text.lower()
    if text in (
        DEPLOY_BLOCKED_FOLDER_MISSING,
        DEPLOY_ERR_MOD_PATH_MISSING,
        DEPLOY_ERR_TARGET_FOREIGN,
        DEPLOY_ERR_PERMISSION,
        DEPLOY_ERR_COPY,
        DEPLOY_ERR_UNDEPLOY_MISMATCH,
        MISSING_CONTENT_DEPLOY_ERROR,
    ):
        return text
    if "目标目录已存在其他" in text:
        return DEPLOY_ERR_TARGET_FOREIGN
    if "请先配置游戏部署目录" in text or (
        "mod" in low and "directory" in low and "not" in low
    ):
        if "不存在" in text or "does not exist" in low:
            return DEPLOY_ERR_MOD_PATH_MISSING
        return text
    if "Target mod directory does not exist" in text:
        return DEPLOY_ERR_MOD_PATH_MISSING
    if "游戏安装目录不存在" in text:
        return DEPLOY_ERR_MOD_PATH_MISSING
    if "permission denied" in low or ("拒绝" in text and "访问" in text):
        return DEPLOY_ERR_PERMISSION
    if text.startswith("复制失败") or "复制失败" in text:
        return DEPLOY_ERR_COPY
    # Keep archive / extract reasons verbatim for the Detail status banner.
    return text


class ModDeployer:
    """
    Facade: resolve Mod + game config, pick a :class:`DeployStrategy`,
    write ``deploy_manifest.json``, and update SQLite deploy columns.
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
        mod_id: int | str,
        *,
        require_target_exists: bool = False,
        prepare_archives: bool = True,
        for_undeploy: bool = False,
    ) -> tuple[DeployContext | None, dict[str, Any] | None, Path | None]:
        mid = str(mod_id).strip()
        if not mid.isdigit():
            return None, {
                "success": False,
                "error": f"无效的 Mod ID：{mod_id}",
            }, None

        db = self._database()
        db_meta = db.get_mod(mid)
        source = self.files.find_by_published_id(mid)

        if source is None or not source.is_dir():
            if for_undeploy:
                lkp = ""
                try:
                    row = db.get_mod_backup_row(mid) or {}
                    lkp = str(row.get("last_known_path") or "").strip()
                except Exception:  # noqa: BLE001
                    lkp = ""
                if lkp:
                    candidate = Path(lkp)
                    if candidate.is_dir():
                        source = candidate
            if source is None or not source.is_dir():
                return None, {
                    "success": False,
                    "error": (
                        f"源 Mod 目录不存在（库：{self.library_root}，mod_id={mid}）"
                    ),
                    "mod_id": mid,
                }, None

        source = source.resolve()
        fs_meta = self.files.load_metadata(source)
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
                        "persist inferred app_id failed mod_id=%s app_id=%s",
                        mid,
                        app_id,
                        exc_info=True,
                    )
                logger.info(
                    "[DEPLOY] mod_id=%s inferred app_id=%s from library context",
                    mid,
                    app_id,
                )

        if not app_id:
            return None, {
                "success": False,
                "error": f"无法解析 Mod 所属游戏 AppID（mod_id={mid}）",
                "mod_id": mid,
            }, None

        cfg = db.get_game_deploy_config(app_id)
        display = db.get_mod_display_info(mid)
        custom_deploy_path = (
            str(display.custom_deploy_path or "").strip() if display else ""
        )
        workspace_id = (
            str(display.workspace_id or "").strip() if display else ""
        ) or mid

        if custom_deploy_path:
            # Custom absolute path overrides all game-level deploy rules.
            if cfg is None:
                cfg = GameDeployConfig(app_id=app_id)
            if require_target_exists:
                custom_root = Path(custom_deploy_path).expanduser()
                parent = custom_root if custom_root.exists() else custom_root.parent
                if not parent.exists():
                    return None, {
                        "success": False,
                        "error": "Target mod directory does not exist",
                        "mod_id": mid,
                    }, None
            deploy_type = DEPLOY_TYPE_CUSTOM_PATH
        else:
            if cfg is None:
                return None, {
                    "success": False,
                    "error": "请先配置游戏部署目录",
                    "mod_id": mid,
                }, None

            deploy_type = resolve_deploy_type(app_id, cfg.deploy_type)
            if deploy_type == DEPLOY_TYPE_FOLDER_COPY and not str(
                cfg.mod_path or ""
            ).strip():
                return None, {
                    "success": False,
                    "error": "请先配置游戏部署目录",
                    "mod_id": mid,
                }, None
            # Palworld: install_path and/or mod_path — strategy picks pak vs folder_copy.
            # Do not require install_path alone (folder-only Mods still need mod_path).
            if deploy_type == DEPLOY_TYPE_PALWORLD_PAK:
                has_install = bool(str(cfg.install_path or "").strip())
                has_mod = bool(str(cfg.mod_path or "").strip())
                if not has_install and not has_mod:
                    return None, {
                        "success": False,
                        "error": "请先配置游戏安装目录或部署目录",
                        "mod_id": mid,
                    }, None
            # Anno 1800: prefer install_path → mods/; mod_path is a fallback root.
            if deploy_type == DEPLOY_TYPE_ANNO_1800:
                has_install = bool(str(cfg.install_path or "").strip())
                has_mod = bool(str(cfg.mod_path or "").strip())
                if not has_install and not has_mod:
                    return None, {
                        "success": False,
                        "error": "请先配置游戏安装目录或部署目录",
                        "mod_id": mid,
                    }, None
            # Slay the Spire: jars land under <install>/mods (strategy mkdir).
            if deploy_type == DEPLOY_TYPE_SLAY_THE_SPIRE:
                if not str(cfg.install_path or "").strip():
                    return None, {
                        "success": False,
                        "error": "请先配置游戏安装目录",
                        "mod_id": mid,
                    }, None
            # Stardew Valley: SMAPI mods land under configured Mods (mod_path).
            if deploy_type == DEPLOY_TYPE_STARDEW_VALLEY:
                if not str(cfg.mod_path or "").strip():
                    return None, {
                        "success": False,
                        "error": "请先配置游戏部署目录",
                        "mod_id": mid,
                    }, None
            if (
                require_target_exists
                and deploy_type == DEPLOY_TYPE_FOLDER_COPY
            ):
                mod_path = Path(str(cfg.mod_path).strip()).expanduser()
                if not mod_path.exists():
                    return None, {
                        "success": False,
                        "error": "Mod 安装目录不存在，请检查游戏设置",
                        "mod_id": mid,
                    }, None
            if (
                require_target_exists
                and deploy_type == DEPLOY_TYPE_PALWORLD_PAK
            ):
                install_raw = str(cfg.install_path or "").strip()
                mod_raw = str(cfg.mod_path or "").strip()
                install_ok = (
                    bool(install_raw)
                    and Path(install_raw).expanduser().is_dir()
                )
                mod_ok = bool(mod_raw) and Path(mod_raw).expanduser().exists()
                if not install_ok and not mod_ok:
                    return None, {
                        "success": False,
                        "error": "Target mod directory does not exist",
                        "mod_id": mid,
                    }, None
            # Anno: strategy mkdir(mods/) — only require install root when set.
            if require_target_exists and deploy_type == DEPLOY_TYPE_ANNO_1800:
                install_raw = str(cfg.install_path or "").strip()
                mod_raw = str(cfg.mod_path or "").strip()
                if install_raw:
                    if not Path(install_raw).expanduser().is_dir():
                        return None, {
                            "success": False,
                            "error": "Target mod directory does not exist",
                            "mod_id": mid,
                        }, None
                elif mod_raw and not Path(mod_raw).expanduser().exists():
                    return None, {
                        "success": False,
                        "error": "Target mod directory does not exist",
                        "mod_id": mid,
                    }, None
            if require_target_exists and deploy_type == DEPLOY_TYPE_SLAY_THE_SPIRE:
                install_raw = str(cfg.install_path or "").strip()
                if not install_raw or not Path(install_raw).expanduser().is_dir():
                    return None, {
                        "success": False,
                        "error": "Target mod directory does not exist",
                        "mod_id": mid,
                    }, None

        cleanup: Path | None = None
        content_root = source
        allowed: frozenset[str] | None
        if prepare_archives:
            try:
                content_root, allowed, cleanup = prepare_deploy_content(
                    mid, source, db=db
                )
                allowed = _normalize_deploy_allow_list(allowed)
            except (
                FileNotFoundError,
                ValueError,
                RuntimeError,
                OSError,
                DeploySourceError,
            ) as exc:
                return None, {
                    "success": False,
                    "error": str(exc),
                    "mod_id": mid,
                }, None
        else:
            allowed = _normalize_deploy_allow_list(
                resolve_deploy_sources(mid, source, db=db)
            )

        return (
            DeployContext(
                mod_id=mid,
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
                "[DEPLOY] mod_id=%s failed to record failed status: %s (%s)",
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

        Returns ``True`` when rollback succeeded (or there was nothing to roll
        back): manifest/transaction cleaned, DB ``not_deployed``.

        Returns ``False`` when rollback failed: keeps manifest, backups, and
        ``deploy_transaction.json`` for recovery; DB ``failed``.
        """
        msg = _normalize_deploy_error(error)
        rollback_ok = True
        if prep is not None:
            try:
                backup_mgr.rollback(prep)  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001
                rollback_ok = False
                logger.error(
                    "[DEPLOY] rollback failed, keeping manifest and backup "
                    "for recovery mod_id=%s error=%s",
                    mid,
                    exc,
                )

        if not rollback_ok:
            self._mark_failed(mid, app_id=app_id, error=msg)
            return False

        try:
            delete_manifest(manifest_root)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[DEPLOY] mod_id=%s delete_manifest after failure failed", mid
            )
        try:
            backup_mgr.clear_transaction()
        except Exception:  # noqa: BLE001
            logger.exception(
                "[DEPLOY] mod_id=%s clear_transaction after rollback failed", mid
            )
        try:
            self._database().update_mod_deploy_status(
                mid,
                deploy_status=DEPLOY_STATUS_NOT_DEPLOYED,
                deploy_path="",
                deploy_time="",
                deploy_error=msg,
                app_id=app_id or None,
            )
        except Exception:  # noqa: BLE001
            self._mark_failed(mid, app_id=app_id, error=msg)
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

        for folder in folders:
            mgr = BackupManager(folder)
            txn = mgr.load_transaction()
            if not txn:
                continue
            result = mgr.recover_interrupted_transaction(auto_rollback=True)
            mid = str(txn.get("mod_id") or folder.name).strip()
            entry = {
                "mod_id": mid,
                "managed_path": str(folder),
                **result,
            }
            reports.append(entry)
            action = str(result.get("action") or "")
            if action == "rolled_back" and mid.isdigit():
                try:
                    self._database().update_mod_deploy_status(
                        mid,
                        deploy_status=DEPLOY_STATUS_NOT_DEPLOYED,
                        deploy_path="",
                        deploy_time="",
                        deploy_error="interrupted deploy rolled back from transaction",
                        app_id=None,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[DEPLOY] mod_id=%s failed to clear status after txn rollback",
                        mid,
                    )
            elif action in {"needs_attention", "marked_failed"}:
                logger.warning(
                    "[DEPLOY] stale transaction needs attention mod_id=%s path=%s: %s",
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
        mod_id: int | str,
        *,
        _deploy_stack: frozenset[str] | None = None,
        _skip_target_ownership_check: bool = False,
    ) -> dict[str, Any]:
        """Deploy one Mod by Workshop / published file id."""
        mid = str(mod_id).strip()
        log_prefix = f"[DEPLOY] mod_id={mid}"

        import sys

        import services.importers.archive as archive_mod
        from services.importers.archive import (
            find_7z_executable,
            resolve_bundled_unrar_tool,
        )

        try:
            import rarfile

            rarfile_version = getattr(rarfile, "__version__", "unknown")
        except ImportError:
            rarfile_version = "not installed"

        archive_path = getattr(archive_mod, "__file__", "unknown")
        try:
            from datetime import datetime, timezone

            archive_mtime = datetime.fromtimestamp(
                Path(archive_path).stat().st_mtime,
                tz=timezone.utc,
            ).isoformat()
        except OSError:
            archive_mtime = "unknown"

        bundled = resolve_bundled_unrar_tool()
        seven = find_7z_executable()
        gui_runtime_lines = [
            f"[GUI_RUNTIME] python_executable={sys.executable}",
            f"[GUI_RUNTIME] python_version={sys.version}",
            f"[GUI_RUNTIME] archive_module={archive_path}",
            f"[GUI_RUNTIME] archive_module_mtime={archive_mtime}",
            f"[GUI_RUNTIME] rarfile_version={rarfile_version}",
            f"[GUI_RUNTIME] bundled_unrar={bundled}",
            f"[GUI_RUNTIME] find_7z={seven}",
        ]
        for line in gui_runtime_lines:
            logger.warning(line)
        try:
            from core.paths import data_dir

            trace_file = data_dir() / "gui_runtime_trace.log"
            with trace_file.open("a", encoding="utf-8") as fh:
                fh.write(f"mod_id={mid}\n")
                for line in gui_runtime_lines:
                    fh.write(line + "\n")
                fh.write("\n")
        except OSError:
            pass

        from services.runtime_identity import log_archive_runtime_identity

        log_archive_runtime_identity(logger, prefix="[DEPLOY_RUNTIME]")

        if mid.isdigit() and not self._database().is_mod_enabled(mid):
            error = "Mod disabled"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            return {
                "success": False,
                "error": error,
                "mod_id": mid,
            }

        # content_status gates (Phase 8) — separate from deployment_status
        blocked = deploy_block_reason_for_content_status(
            content_status_for_mod(mid, db=self._database())
        )
        if blocked:
            logger.warning("%s result=fail error=%s", log_prefix, blocked)
            return {
                "success": False,
                "error": blocked,
                "mod_id": mid,
                "folder_missing": blocked == DEPLOY_BLOCKED_FOLDER_MISSING,
            }

        source_for_gate = self.files.find_by_published_id(mid)
        if source_for_gate is None and is_mod_folder_absent(mid):
            logger.warning(
                "%s result=fail error=folder_missing", log_prefix
            )
            return {
                "success": False,
                "error": DEPLOY_BLOCKED_FOLDER_MISSING,
                "mod_id": mid,
                "folder_missing": True,
            }
        if source_for_gate is not None and is_mod_folder_absent(mid, source_for_gate):
            logger.warning(
                "%s result=fail error=folder_missing", log_prefix
            )
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
            from services.mod_source_integrity import validate_source

            validate_source(
                mid,
                managed_path=source_for_gate,
                db=self._database(),
            )
        except DeploySourceError as exc:
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
                        "%s skip disabled dependency dep_mod_id=%s",
                        log_prefix,
                        dep_mid,
                    )
                    continue
                logger.info(
                    "%s deploy dependency first dep_mod_id=%s",
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
                    logger.warning("%s result=fail error=%s", log_prefix, err)
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

        ctx, early, cleanup = self._resolve_context(
            mod_id, require_target_exists=True, prepare_archives=True
        )
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

    def _dependency_mod_ids_for_deploy(self, mod_id: str) -> list[str]:
        """Ordered dependency mod_ids (DB relations + metadata.json workspace ids)."""
        mid = str(mod_id or "").strip()
        if not mid or not mid.isdigit():
            return []
        db = self._database()
        ordered: list[str] = []
        seen: set[str] = set()

        def _push(candidate: str) -> None:
            cid = str(candidate or "").strip()
            if not cid or cid == mid or cid in seen:
                return
            seen.add(cid)
            ordered.append(cid)

        try:
            grouped = db.get_mod_relationships(mid)
            for item in grouped.get("dependencies") or []:
                _push(str(item.get("mod_id") or ""))
        except Exception:  # noqa: BLE001
            logger.debug(
                "dependency relation lookup failed mod_id=%s", mid, exc_info=True
            )

        source = self.files.find_by_published_id(mid)
        if source is not None:
            try:
                from services.file_ops import read_info_metadata_dict

                data = read_info_metadata_dict(source) or {}
                raw = data.get("dependencies") or []
                if isinstance(raw, (list, tuple)):
                    for entry in raw:
                        if isinstance(entry, dict):
                            wid = str(
                                entry.get("workspace_id")
                                or entry.get("mod_id")
                                or entry.get("id")
                                or ""
                            ).strip()
                        else:
                            wid = str(entry or "").strip()
                        if not wid:
                            continue
                        found = db.find_mod_id_by_workspace_id(wid)
                        if found:
                            _push(found)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "dependency metadata lookup failed mod_id=%s",
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
            self._mark_failed(ctx.mod_id, app_id=ctx.app_id, error=error)
            return {
                "success": False,
                "error": error,
                "mod_id": ctx.mod_id,
                "supported": list(supported_deploy_types()),
            }

        try:
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
            self._mark_failed(ctx.mod_id, app_id=ctx.app_id, error=error)
            return {
                "success": False,
                "error": error,
                "reason": "invalid_source",
                "mod_id": ctx.mod_id,
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

        # Conflict detection (warn only for overlapping file claims)
        conflicts_payload: dict[str, Any] | None = None
        planned = strategy.plan(ctx)
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
                self._mark_failed(ctx.mod_id, app_id=ctx.app_id, error=error)
                return {
                    "success": False,
                    "error": error,
                    "mod_id": ctx.mod_id,
                    "deploy_type": ctx.deploy_type,
                }
            conflicts_payload = self.check_conflict_preview(
                ctx.mod_id,
                [e.target for e in planned.files],
            )
            if conflicts_payload and conflicts_payload.get("conflict"):
                logger.warning(
                    "%s conflict=true files=%s",
                    log_prefix,
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
                    mod_id=ctx.mod_id,
                    managed=ctx.library_folder(),
                    mod_path=mod_path_raw,
                    library_root=self.library_root,
                )
                if kind == "foreign":
                    logger.warning(
                        "%s target_kind=foreign (backup-and-overwrite allowed)",
                        log_prefix,
                    )

        manifest_root = ctx.library_folder()
        backup_mgr = BackupManager(manifest_root)
        prep = None
        planned_targets: list[str] = []
        if planned.success and planned.files:
            planned_targets = [e.target for e in planned.files]

        try:
            prep = backup_mgr.prepare_overwrite(planned_targets)
        except (OSError, BackupIntegrityError, BackupRestoreError) as exc:
            error = f"部署前备份原文件失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            self._mark_failed(ctx.mod_id, app_id=ctx.app_id, error=error)
            return {
                "success": False,
                "error": error,
                "mod_id": ctx.mod_id,
                "deploy_type": ctx.deploy_type,
            }

        try:
            result = strategy.deploy(ctx)
        except Exception as exc:
            logger.exception("%s strategy.deploy raised", log_prefix)
            self._abort_failed_deploy(
                mid=ctx.mod_id,
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
                mid=ctx.mod_id,
                app_id=ctx.app_id,
                manifest_root=manifest_root,
                backup_mgr=backup_mgr,
                prep=prep,
                error=err,
            )
            out = {
                "success": False,
                "error": err,
                "mod_id": ctx.mod_id,
                "deploy_type": ctx.deploy_type,
            }
            if conflicts_payload:
                out["conflicts"] = conflicts_payload
            if relationship_warnings:
                out["relationship_warnings"] = relationship_warnings
            return out

        assert result.manifest is not None
        try:
            validated_count = validate_deploy_result(result)
        except DeployValidationError as exc:
            logger.warning(
                "%s source=%s result=failed reason=missing_targets copied=%s",
                log_prefix,
                ctx.source,
                result.copied_files,
            )
            for missing in exc.missing_targets:
                logger.warning("%s missing target: %s", log_prefix, missing)
            error = _normalize_deploy_error(str(exc))
            self._abort_failed_deploy(
                mid=ctx.mod_id,
                app_id=ctx.app_id,
                manifest_root=manifest_root,
                backup_mgr=backup_mgr,
                prep=prep,
                error=error,
            )
            out = {
                "success": False,
                "error": error,
                "reason": "missing_targets",
                "missing_targets": list(exc.missing_targets),
                "mod_id": ctx.mod_id,
                "deploy_type": ctx.deploy_type,
            }
            if conflicts_payload:
                out["conflicts"] = conflicts_payload
            if relationship_warnings:
                out["relationship_warnings"] = relationship_warnings
            return out

        if prep is not None:
            backup_mgr.apply_to_manifest(result.manifest, prep)
        try:
            backup_mgr.validate_manifest_backups(result.manifest)
            enrich_manifest_fingerprint(
                result.manifest,
                source=manifest_root,
                managed=manifest_root,
            )
            from services.mod_source_integrity import enrich_manifest_source_hashes

            enrich_manifest_source_hashes(result.manifest)
            validate_manifest_for_save(
                result.manifest,
                managed=manifest_root,
                ctx=ctx,
            )
            save_manifest(manifest_root, result.manifest)
            if prep is not None:
                backup_mgr.mark_deployed(prep, mod_id=ctx.mod_id)
        except OSError as exc:
            error = f"文件已复制，但写入部署清单失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            self._abort_failed_deploy(
                mid=ctx.mod_id,
                app_id=ctx.app_id,
                manifest_root=manifest_root,
                backup_mgr=backup_mgr,
                prep=prep,
                error=error,
            )
            return {"success": False, "error": error, "mod_id": ctx.mod_id}
        except (ManifestSecurityError, BackupIntegrityError) as exc:
            error = f"部署清单校验失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            self._abort_failed_deploy(
                mid=ctx.mod_id,
                app_id=ctx.app_id,
                manifest_root=manifest_root,
                backup_mgr=backup_mgr,
                prep=prep,
                error=error,
            )
            return {"success": False, "error": error, "mod_id": ctx.mod_id}
        except Exception as exc:  # noqa: BLE001 — integrity / path escape
            error = f"部署清单校验失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            self._abort_failed_deploy(
                mid=ctx.mod_id,
                app_id=ctx.app_id,
                manifest_root=manifest_root,
                backup_mgr=backup_mgr,
                prep=prep,
                error=error,
            )
            return {"success": False, "error": error, "mod_id": ctx.mod_id}

        try:
            self._database().update_mod_deploy_status(
                ctx.mod_id,
                deploy_status=DEPLOY_STATUS_DEPLOYED,
                deploy_path=result.target,
                deploy_time=result.deploy_time,
                deploy_error="",
                app_id=ctx.app_id,
            )
            db_warning: str | None = None
        except Exception as exc:  # noqa: BLE001
            db_warning = "database_update_failed"
            logger.warning(
                "[DEPLOY] database status update failed, filesystem deployment "
                "succeeded mod_id=%s error=%s",
                ctx.mod_id,
                exc,
            )

        # Post-deploy: refresh library-wide file-path conflicts into SQLite
        try:
            ConflictDetector(
                self.library_root, db=self._database()
            ).check_all_mods(persist=True)
        except Exception:  # noqa: BLE001
            logger.exception("%s post-deploy conflict scan failed", log_prefix)

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
            "mod_id": ctx.mod_id,
            "target": result.target,
            "copied_files": result.copied_files,
            "validated": validated_count,
            "files": file_details,
            "deploy_type": result.deploy_type,
            "deploy_time": result.deploy_time,
            "deployment_status": "deployed",
        }
        if db_warning:
            out["warning"] = db_warning
        if conflicts_payload:
            out["conflicts"] = conflicts_payload
        if relationship_warnings:
            out["relationship_warnings"] = relationship_warnings
        return out

    def deployment_status(self, mod_id: int | str) -> str:
        """Phase 8 runtime deployment_status (not content_status)."""
        return resolve_deployment_status(
            mod_id,
            library_root=self.library_root,
            db=self._database(),
        )

    def check_conflict_preview(
        self,
        mod_id: int | str,
        planned_targets: list[str | Path],
    ) -> dict[str, Any] | None:
        """
        Pre-deploy path-overlap preview. Never blocks deploy; payload uses
        ``status="conflict"`` for FILE_OVERWRITE (same as post-deploy scan).
        """
        mid = str(mod_id).strip()
        report = ConflictDetector(
            self.library_root, db=self._database()
        ).preview_targets(mid, list(planned_targets))
        if report.status == "none" or not report.conflicts:
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
        return {
            "conflict": True,
            "status": report.status,
            "conflicts": [c.as_dict() for c in report.conflicts],
            "files": files,
        }

    def undeploy_mod(self, mod_id: int | str) -> dict[str, Any]:
        """
        Remove files listed in ``deploy_manifest.json`` and clear DB status.

        Never deletes an entire target directory tree — only manifest targets.
        """
        mid = str(mod_id).strip()
        log_prefix = f"[UNDEPLOY] mod_id={mid}"

        ctx, early, _cleanup = self._resolve_context(
            mod_id, prepare_archives=False, for_undeploy=True
        )
        if early is not None:
            logger.warning("%s result=fail error=%s", log_prefix, early.get("error"))
            out = dict(early)
            out["error"] = _normalize_deploy_error(str(early.get("error") or ""))
            return out
        assert ctx is not None

        manifest_root = ctx.library_folder()
        manifest = load_manifest(manifest_root, expected_mod_id=mid)

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
            if raw_probe is not None and str(raw_probe.mod_id or "").strip() not in (
                "",
                mid,
            ):
                error = DEPLOY_ERR_UNDEPLOY_MISMATCH
                logger.warning(
                    "%s result=fail error=%s (manifest pollution)",
                    log_prefix,
                    error,
                )
                return {"success": False, "error": error, "mod_id": mid}
            info = self._database().get_mod_deploy_info(mid) if mid.isdigit() else None
            deploy_path = str(info.deploy_path or "").strip() if info else ""
            if deploy_path and Path(deploy_path).exists() and any(
                Path(deploy_path).rglob("*")
            ):
                error = DEPLOY_ERR_UNDEPLOY_MISMATCH
                logger.warning("%s result=fail error=%s", log_prefix, error)
                return {"success": False, "error": error, "mod_id": mid}
        else:
            try:
                validate_manifest_mod_id(manifest, mid)
                planned_targets: list[str] = []
                try:
                    planned = strategy.plan(ctx)
                    if planned.success and planned.files:
                        planned_targets = [e.target for e in planned.files]
                except Exception:  # noqa: BLE001 — plan is advisory for undeploy
                    logger.debug(
                        "%s strategy.plan for undeploy target check failed",
                        log_prefix,
                        exc_info=True,
                    )
                validate_manifest_targets(
                    manifest,
                    allowed_roots=collect_allowed_target_roots(ctx),
                    planned_targets=planned_targets or None,
                )
            except ManifestSecurityError as exc:
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

        backup_mgr = BackupManager(manifest_root)
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
                    "mod_id": ctx.mod_id,
                }

        with prune_protection(collect_protected_roots(ctx)):
            result = strategy.undeploy(ctx, manifest)
        if not result.success:
            logger.warning("%s result=fail error=%s", log_prefix, result.error)
            return {
                "success": False,
                "error": result.error,
                "mod_id": ctx.mod_id,
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
                    "mod_id": ctx.mod_id,
                    "restore_failures": list(exc.failures),
                }
            except Exception as exc:  # noqa: BLE001
                logger.exception("%s restore from backup failed", log_prefix)
                return {
                    "success": False,
                    "error": f"取消部署时恢复原文件失败：{exc}",
                    "mod_id": ctx.mod_id,
                }
            try:
                backup_mgr.cleanup_backups()
            except Exception:  # noqa: BLE001
                logger.exception("%s backup cleanup failed", log_prefix)

        delete_manifest(manifest_root)
        try:
            self._database().update_mod_deploy_status(
                ctx.mod_id,
                deploy_status=DEPLOY_STATUS_NOT_DEPLOYED,
                deploy_path="",
                deploy_time="",
                deploy_error="",
                app_id=ctx.app_id,
            )
        except Exception as exc:  # noqa: BLE001
            error = f"文件已移除，但更新部署状态失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            return {"success": False, "error": error, "mod_id": ctx.mod_id}

        try:
            ConflictDetector(
                self.library_root, db=self._database()
            ).check_all_mods(persist=True)
        except Exception:  # noqa: BLE001
            logger.exception("%s post-undeploy conflict scan failed", log_prefix)

        logger.info(
            "%s source=%s result=ok removed=%s",
            log_prefix,
            ctx.source,
            result.copied_files,
        )
        return {
            "success": True,
            "mod_id": ctx.mod_id,
            "removed_files": result.copied_files,
            "deploy_type": result.deploy_type or ctx.deploy_type,
        }

    def redeploy_mod(self, mod_id: int | str) -> dict[str, Any]:
        """
        Redeploy ≡ undeploy (old manifest) + deploy (new files + new manifest).

        Aborts if undeploy fails, so removed source files cannot linger.
        """
        mid = str(mod_id).strip()
        und = self.undeploy_mod(mod_id)
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
        return self.deploy_mod(mod_id, _skip_target_ownership_check=True)
