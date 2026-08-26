"""Deploy managed Mods into a game's configured paths (strategy-based)."""

from __future__ import annotations

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
from services.conflict import ConflictDetector
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
    save_manifest,
    supported_deploy_types,
)
from services.deploy_rules.custom import CustomPathStrategy
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


def _build_extracted_deploy_content(
    managed: Path,
    archive_paths: list[Path],
    *,
    include_other_plain: bool,
    preserve_extract_layout: bool = False,
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
            if preserve_extract_layout:
                root = extract_dir
            else:
                root = find_mod_root(extract_dir) or extract_dir
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
) -> tuple[Path, frozenset[str] | None, Path | None]:
    """
    Resolve deploy content root for *mod_id*.

    Archive FileEntries are deploy *units* but not deploy *files*: selected
    archives are extracted and their contents become the deploy payload.
    Plain FileEntries keep path allow-list behaviour.

    Returns ``(content_root, allowed_rel_paths, cleanup_dir)``.
    ``cleanup_dir`` is a temp extract folder to remove after deploy (or None).
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
    bundle = database.get_mod_files(mod_id)
    if not bundle.files:
        sniffed = _sniff_managed_archives(managed)
        if sniffed:
            content, stage = _build_extracted_deploy_content(
                managed,
                sniffed,
                include_other_plain=True,
                preserve_extract_layout=preserve_layout,
            )
            return content, None, stage
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
            )
            return content, None, stage
        return managed, None, None

    archive_entries = [e for e in selected if _entry_is_archive_source(e)]
    plain_entries = [e for e in selected if not _entry_is_archive_source(e)]

    if not archive_entries:
        return managed, resolve_deploy_sources(mod_id, managed, db=database), None

    stage = import_cache_root() / f"deploy_{uuid.uuid4().hex}"
    content = stage / "content"
    content.mkdir(parents=True, exist_ok=False)
    try:
        for entry in archive_entries:
            archive = _resolve_archive_file(managed, entry)
            if archive is None:
                # Directory-imported / extracted Mods may still list an archive
                # FileEntry without a physical .zip/.7z/.rar on disk. Fall back
                # to the managed folder so .pak / folder_copy scan can proceed.
                logger.warning(
                    "Archive source missing; falling back to managed dir: %s",
                    getattr(entry, "filename", "") or entry,
                )
                cleanup_import_cache(stage)
                return managed, None, None
            extract_dir = extract_archive(archive, dest_dir=stage / f"ex_{uuid.uuid4().hex[:8]}")
            if preserve_layout:
                root = extract_dir
            else:
                root = find_mod_root(extract_dir) or extract_dir
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
            return managed, None, None

        # Extracted payload: deploy whole content tree (never the .zip itself).
        return content, None, stage
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
            except (FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
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
            if read_is_missing_content(source_for_gate):
                logger.warning(
                    "%s result=fail error=%s",
                    log_prefix,
                    MISSING_CONTENT_DEPLOY_ERROR,
                )
                return {
                    "success": False,
                    "error": MISSING_CONTENT_DEPLOY_ERROR,
                    "mod_id": mid,
                    "is_missing_content": True,
                }

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
            strategy = get_strategy(ctx.deploy_type, app_id=ctx.app_id)
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

        # Phase 8: hard-block folder_copy when target tree is foreign
        if (
            not skip_target_ownership_check
            and not str(ctx.custom_deploy_path or "").strip()
            and ctx.deploy_type == DEPLOY_TYPE_FOLDER_COPY
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
                    error = DEPLOY_ERR_TARGET_FOREIGN
                    logger.warning("%s result=fail error=%s", log_prefix, error)
                    self._mark_failed(ctx.mod_id, app_id=ctx.app_id, error=error)
                    return {
                        "success": False,
                        "error": error,
                        "mod_id": ctx.mod_id,
                        "deploy_type": ctx.deploy_type,
                        "deployment_status": "conflict",
                    }

        result = strategy.deploy(ctx)
        if not result.success:
            err = _normalize_deploy_error(result.error)
            logger.warning(
                "%s source=%s result=fail error=%s",
                log_prefix,
                ctx.source,
                err,
            )
            self._mark_failed(ctx.mod_id, app_id=ctx.app_id, error=err)
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
        manifest_root = ctx.library_folder()
        try:
            enrich_manifest_fingerprint(
                result.manifest,
                source=manifest_root,
                managed=manifest_root,
            )
            save_manifest(manifest_root, result.manifest)
        except OSError as exc:
            error = f"文件已复制，但写入部署清单失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            self._mark_failed(ctx.mod_id, app_id=ctx.app_id, error=error)
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
        except Exception as exc:  # noqa: BLE001
            error = f"文件已复制，但更新部署状态失败：{exc}"
            logger.warning("%s result=fail error=%s", log_prefix, error)
            return {"success": False, "error": error, "mod_id": ctx.mod_id}

        # Post-deploy: refresh library-wide file-path conflicts into SQLite
        try:
            ConflictDetector(
                self.library_root, db=self._database()
            ).check_all_mods(persist=True)
        except Exception:  # noqa: BLE001
            logger.exception("%s post-deploy conflict scan failed", log_prefix)

        logger.info(
            "%s source=%s target=%s type=%s result=ok copied=%s",
            log_prefix,
            ctx.source,
            result.target,
            result.deploy_type,
            result.copied_files,
        )
        out = {
            "success": True,
            "mod_id": ctx.mod_id,
            "target": result.target,
            "copied_files": result.copied_files,
            "deploy_type": result.deploy_type,
            "deploy_time": result.deploy_time,
            "deployment_status": "deployed",
        }
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
        Pre-deploy path-overlap preview. Never blocks; returns warning payload.
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

        if str(ctx.custom_deploy_path or "").strip():
            strategy = CustomPathStrategy()
        else:
            strategy = get_strategy(ctx.deploy_type, app_id=ctx.app_id)
            if strategy is None:
                strategy = get_strategy(DEPLOY_TYPE_FOLDER_COPY, app_id=0)
        assert strategy is not None

        manifest_root = ctx.library_folder()
        manifest = load_manifest(manifest_root)
        # Phase 8: refuse undeploy when ownership cannot be confirmed
        if manifest is None:
            info = self._database().get_mod_deploy_info(mid) if mid.isdigit() else None
            deploy_path = str(info.deploy_path or "").strip() if info else ""
            if deploy_path and Path(deploy_path).exists() and any(
                Path(deploy_path).rglob("*")
            ):
                error = DEPLOY_ERR_UNDEPLOY_MISMATCH
                logger.warning("%s result=fail error=%s", log_prefix, error)
                return {"success": False, "error": error, "mod_id": mid}
        elif str(manifest.mod_id or "").strip() not in ("", mid):
            error = DEPLOY_ERR_UNDEPLOY_MISMATCH
            logger.warning("%s result=fail error=%s", log_prefix, error)
            return {"success": False, "error": error, "mod_id": mid}

        # Manifest from a prior custom deploy still undeploys via CustomPathStrategy.
        if (
            manifest is not None
            and str(manifest.deploy_type or "") == DEPLOY_TYPE_CUSTOM_PATH
            and not str(ctx.custom_deploy_path or "").strip()
        ):
            strategy = CustomPathStrategy()
        result = strategy.undeploy(ctx, manifest)
        if not result.success:
            logger.warning("%s result=fail error=%s", log_prefix, result.error)
            return {
                "success": False,
                "error": result.error,
                "mod_id": ctx.mod_id,
            }

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
