"""``.info/metadata.json`` sidecar — portable Mod identity for folder copy/import."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from core.mod_platform import (
    FILE_ROLE_GITHUB_RELEASE_ASSET,
    FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
    FILE_ROLE_NEXUS_MAIN,
    FILE_ROLE_STEAM_CONTENT,
    FILE_ROLE_UNKNOWN,
    METADATA_SOURCE_TYPE_KEY,
    ModFileEntry,
    ModFilesBundle,
    normalize_file_role,
    normalize_platform,
    parse_metadata_platform,
)
from services.file_ops import (
    INFO_DIR_NAME,
    LEGACY_INFO_DIR_NAME,
    LEGACY_METADATA_FILENAME,
    METADATA_FILENAME,
    persist_unified_metadata_dict,
    read_info_metadata_dict,
)

logger = logging.getLogger(__name__)

# Badge labels persisted in ``file_roles`` (filename → role).
ROLE_MAIN = "Main"
ROLE_SOURCE = "Source"
ROLE_OTHER = "Other"

_BADGE_MAIN_ROLES = frozenset(
    {
        FILE_ROLE_NEXUS_MAIN,
        FILE_ROLE_GITHUB_RELEASE_ASSET,
        FILE_ROLE_STEAM_CONTENT,
    }
)
_BADGE_SOURCE_ROLES = frozenset({FILE_ROLE_GITHUB_SOURCE_ARCHIVE})


def _badge_kind(entry: ModFileEntry) -> str | None:
    role = normalize_file_role(getattr(entry, "file_role", None))
    if role in _BADGE_MAIN_ROLES:
        return ROLE_MAIN
    if role in _BADGE_SOURCE_ROLES:
        return ROLE_SOURCE
    return None


@dataclass
class InfoSidecar:
    """Full portable snapshot written beside the Mod under ``.info/``."""

    display_name: str = ""
    description: str = ""
    source_type: str = ""
    url: str = ""
    workspace_id: str = ""
    custom_deploy_path: str = ""
    offline_page_path: str = ""
    cover_path: str = ""
    published_file_id: str = ""
    category: str = ""
    # Workspace IDs this Mod depends on (deploy-before list).
    dependencies: list[str] = field(default_factory=list)
    file_roles: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Stable key order for readable diffs.
        roles = data.get("file_roles") or {}
        data["file_roles"] = {
            str(k): str(v)
            for k, v in sorted(roles.items(), key=lambda kv: str(kv[0]).lower())
            if str(k).strip()
        }
        deps = data.get("dependencies") or []
        data["dependencies"] = [
            str(x).strip()
            for x in deps
            if str(x or "").strip()
        ]
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> InfoSidecar:
        raw = dict(data or {})
        roles_in = raw.get("file_roles") or {}
        roles: dict[str, str] = {}
        if isinstance(roles_in, Mapping):
            for key, value in roles_in.items():
                name = str(key or "").strip()
                if not name:
                    continue
                label = str(value or "").strip()
                if label.lower() in ("main", "主文件"):
                    roles[name] = ROLE_MAIN
                elif label.lower() in ("source", "源码", "src"):
                    roles[name] = ROLE_SOURCE
                else:
                    roles[name] = ROLE_OTHER
        deps_raw = raw.get("dependencies") or []
        dependencies: list[str] = []
        if isinstance(deps_raw, (list, tuple)):
            for item in deps_raw:
                if isinstance(item, Mapping):
                    wid = str(
                        item.get("workspace_id")
                        or item.get("mod_id")
                        or item.get("id")
                        or ""
                    ).strip()
                else:
                    wid = str(item or "").strip()
                if wid and wid not in dependencies:
                    dependencies.append(wid)
        return cls(
            display_name=str(raw.get("display_name") or "").strip(),
            description=str(raw.get("description") or "").strip(),
            source_type=parse_metadata_platform(raw),
            url=str(
                raw.get("url")
                or raw.get("source_url")
                or raw.get("website")
                or ""
            ).strip(),
            workspace_id=str(raw.get("workspace_id") or "").strip(),
            custom_deploy_path=str(raw.get("custom_deploy_path") or "").strip(),
            offline_page_path=str(
                raw.get("offline_page_path") or raw.get("offline_page") or ""
            ).strip(),
            cover_path=str(raw.get("cover_path") or "").strip(),
            published_file_id=str(raw.get("published_file_id") or "").strip(),
            category=str(raw.get("category") or "").strip(),
            dependencies=dependencies,
            file_roles=roles,
        )


def info_sidecar_path(managed_path: str | Path) -> Path:
    """Canonical ``.info/metadata.json`` path (write target)."""
    return Path(managed_path) / INFO_DIR_NAME / METADATA_FILENAME


def load_info_sidecar(managed_path: str | Path) -> InfoSidecar | None:
    data = read_info_metadata_dict(managed_path)
    if not data:
        return None
    return InfoSidecar.from_dict(data)


def save_info_sidecar(managed_path: str | Path, sidecar: InfoSidecar) -> Path:
    root = Path(managed_path)
    base = read_info_metadata_dict(root) or {}
    payload = sidecar.to_dict()
    # Canonical platform key — never write legacy ``platform`` alias.
    if sidecar.source_type:
        payload[METADATA_SOURCE_TYPE_KEY] = normalize_platform(sidecar.source_type)
    merged = _merge_metadata_patch(base, payload)
    if sidecar.display_name and not str(merged.get("title") or "").strip():
        merged["title"] = sidecar.display_name
    if sidecar.description:
        merged["description"] = sidecar.description
    return persist_unified_metadata_dict(root, merged)


def _merge_metadata_patch(
    base: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Incremental merge into existing ``metadata.json``.

    Never overwrite stored keys with empty values — preserves paths, URLs,
    offline pages, and other fields not touched by the current edit.
    """
    merged = dict(base)
    for key, value in patch.items():
        if key == "file_roles":
            if value:
                merged["file_roles"] = value
            continue
        if value in (None, "", {}, []):
            continue
        merged[key] = value
    return merged


def file_roles_from_bundle(bundle: ModFilesBundle | None) -> dict[str, str]:
    """Map physical filename → Main / Source / Other."""
    roles: dict[str, str] = {}
    if bundle is None:
        return roles
    for entry in bundle.files or []:
        name = str(getattr(entry, "filename", "") or "").strip()
        if not name:
            name = Path(str(getattr(entry, "path", "") or "")).name
        if not name:
            continue
        kind = _badge_kind(entry)
        if kind == ROLE_MAIN:
            roles[name] = ROLE_MAIN
        elif kind == ROLE_SOURCE:
            roles[name] = ROLE_SOURCE
        else:
            roles[name] = ROLE_OTHER
    return roles


def build_sidecar_from_db(
    mod_id: int | str,
    managed_path: str | Path | None = None,
    *,
    db=None,
) -> InfoSidecar:
    """Snapshot SQLite (+ optional mod.json cover/offline) into a sidecar."""
    from core.db_manager import get_db

    database = db if db is not None else get_db()
    info = database.get_mod_display_info(mod_id)
    meta = None
    if managed_path is not None:
        data = read_info_metadata_dict(managed_path)
        if data:
            from services.file_ops import _metadata_from_dict

            meta = _metadata_from_dict(data, Path(managed_path))

    display_name = ""
    description = ""
    source_type = ""
    url = ""
    workspace_id = ""
    custom_deploy_path = ""
    offline_page_path = ""
    cover_path = ""
    published = str(mod_id)
    category = ""
    dependencies: list[str] = []
    bundle = None
    if info is not None:
        # Only the raw user override — never the resolved steam/unknown label.
        display_name = str(info.user_display_name or "").strip()
        try:
            from core.models import is_unknown_mod_title

            if is_unknown_mod_title(
                display_name, published_file_id=str(info.mod_id or mod_id)
            ):
                display_name = ""
        except Exception:  # noqa: BLE001
            pass
        description = str(info.custom_description or "").strip()
        source_type = normalize_platform(info.platform)
        url = str(info.source_url or "").strip()
        workspace_id = str(info.workspace_id or "").strip()
        custom_deploy_path = str(info.custom_deploy_path or "").strip()
        cover_path = str(info.cover_path or "").strip()
        published = str(info.mod_id or mod_id)
        bundle = info.mod_files
        cat_tags = database.get_category_tags(str(mod_id))
        category = cat_tags[0] if cat_tags else ""
        try:
            grouped = database.get_mod_relationships(str(mod_id))
            for item in grouped.get("dependencies") or []:
                tid = str(item.get("mod_id") or "").strip()
                if not tid:
                    continue
                dep_info = database.get_mod_display_info(tid)
                wid = (
                    str(dep_info.workspace_id or "").strip()
                    if dep_info is not None
                    else ""
                ) or tid
                if wid not in dependencies:
                    dependencies.append(wid)
        except Exception:  # noqa: BLE001
            pass
    if meta is not None:
        if not display_name:
            candidate = str(meta.effective_title() or "").strip()
            try:
                from core.models import is_unknown_mod_title

                if not is_unknown_mod_title(
                    candidate, published_file_id=str(meta.published_file_id or "")
                ):
                    display_name = candidate
            except Exception:  # noqa: BLE001
                display_name = candidate
        if not description:
            description = str(meta.description or "").strip()
        if not url:
            url = str(getattr(meta, "url", "") or "").strip()
        if not offline_page_path:
            offline_page_path = str(meta.offline_page_path or "").strip()
        if not cover_path:
            cover_path = str(meta.cover_path or "").strip()
        if meta.published_file_id:
            published = str(meta.published_file_id)

    if managed_path is not None:
        try:
            side = load_info_sidecar(managed_path)
            if side is not None:
                for wid in side.dependencies:
                    if wid not in dependencies:
                        dependencies.append(wid)
        except Exception:  # noqa: BLE001
            pass

    return InfoSidecar(
        display_name=display_name,
        description=description,
        source_type=source_type,
        url=url,
        workspace_id=workspace_id,
        custom_deploy_path=custom_deploy_path,
        offline_page_path=offline_page_path,
        cover_path=cover_path,
        published_file_id=published,
        category=category,
        dependencies=dependencies,
        file_roles=file_roles_from_bundle(bundle),
    )


def write_sidecar_for_mod(
    managed_path: str | Path,
    mod_id: int | str | None = None,
    *,
    db=None,
) -> Path | None:
    """Persist current DB state into ``.info/metadata.json``."""
    root = Path(managed_path)
    if not root.is_dir():
        return None
    mid = str(mod_id or "").strip()
    if not mid:
        data = read_info_metadata_dict(root)
        if data:
            mid = str(data.get("published_file_id") or "").strip()
    if not mid or not mid.isdigit():
        return None
    try:
        sidecar = build_sidecar_from_db(mid, root, db=db)
        return save_info_sidecar(root, sidecar)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write info sidecar for %s: %s", root, exc)
        return None


def _apply_roles_to_bundle(
    bundle: ModFilesBundle, roles: Mapping[str, str], *, platform: str
) -> ModFilesBundle:
    """Apply Main/Source labels from sidecar onto scanned/existing entries."""
    from services.mod_files import main_role_for_platform, source_role_for_platform

    main_id = ""
    source_id = ""
    for entry in bundle.files:
        name = str(entry.filename or Path(entry.path or "").name or "").strip()
        label = roles.get(name, "")
        if label == ROLE_MAIN and not main_id:
            main_id = str(entry.id or "")
        elif label == ROLE_SOURCE and not source_id:
            source_id = str(entry.id or "")

    main_role = main_role_for_platform(platform)
    source_role = source_role_for_platform(platform)
    for entry in bundle.files:
        eid = str(entry.id or "")
        name = str(entry.filename or Path(entry.path or "").name or "").strip()
        label = roles.get(name, ROLE_OTHER)
        if eid == main_id or label == ROLE_MAIN:
            entry.file_role = main_role
            entry.set_selection(True)
        elif eid == source_id or label == ROLE_SOURCE:
            entry.file_role = source_role
            entry.set_selection(False)
        elif label == ROLE_OTHER:
            kind = _badge_kind(entry)
            if kind in (ROLE_MAIN, ROLE_SOURCE):
                entry.file_role = FILE_ROLE_UNKNOWN
                entry.set_selection(False)
    return bundle


def apply_sidecar_to_db(
    managed_path: str | Path,
    *,
    mod_id: int | str | None = None,
    db=None,
    rescan_archives: bool = False,
) -> bool:
    """
    Restore SQLite (+ optional archive rescan) from ``.info/metadata.json``.

    Returns True when a sidecar was found and applied.
    """
    from core.db_manager import get_db
    from services.importers.local_scanner import scan_mod_directory
    from services.mod_files import ModFileManager as JsonMgr

    root = Path(managed_path)
    sidecar = load_info_sidecar(root)
    if sidecar is None:
        return False

    database = db if db is not None else get_db()
    mid = str(mod_id or sidecar.published_file_id or "").strip()
    if not mid or not mid.isdigit():
        return False

    # Ensure row exists.
    try:
        with database._lock:  # noqa: SLF001
            database._ensure_mod_stub(int(mid))  # noqa: SLF001
            database._conn.commit()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass

    existing = database.get_mod_display_info(mid)
    sidecar_display = str(sidecar.display_name or "").strip()
    try:
        from core.models import is_unknown_mod_title

        if is_unknown_mod_title(sidecar_display, published_file_id=mid):
            sidecar_display = ""
    except Exception:  # noqa: BLE001
        pass
    # Prefer a real user override; never re-stamp Unknown_Mod_* placeholders.
    existing_user = ""
    if existing is not None:
        existing_user = str(existing.user_display_name or "").strip()
        try:
            from core.models import is_unknown_mod_title

            if is_unknown_mod_title(existing_user, published_file_id=mid):
                existing_user = ""
        except Exception:  # noqa: BLE001
            pass
    patch: dict[str, Any] = {
        "display_name": sidecar_display or existing_user or "",
        "custom_description": (
            sidecar.description
            if sidecar.description
            else (existing.custom_description if existing else "")
        ),
        "user_notes": (existing.user_notes if existing else ""),
        "favorite": bool(existing.favorite) if existing else False,
    }
    if sidecar.url:
        patch["source_url"] = sidecar.url
    elif existing is not None and existing.source_url:
        patch["source_url"] = existing.source_url
    if sidecar.source_type:
        patch["platform"] = sidecar.source_type
    elif existing is not None and existing.platform:
        patch["platform"] = existing.platform
    if sidecar.custom_deploy_path:
        patch["custom_deploy_path"] = sidecar.custom_deploy_path
    elif existing is not None:
        patch["custom_deploy_path"] = existing.custom_deploy_path or ""
    try:
        database.update_mod_user_metadata(mid, patch)
    except Exception as exc:  # noqa: BLE001
        logger.warning("apply sidecar metadata failed: %s", exc)

    if sidecar.workspace_id:
        try:
            with database._lock:  # noqa: SLF001
                database._conn.execute(  # noqa: SLF001
                    "UPDATE mods SET workspace_id = ? WHERE mod_id = ?",
                    (sidecar.workspace_id, int(mid)),
                )
                database._conn.commit()  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            logger.warning("apply sidecar workspace_id failed: %s", exc)

    if sidecar.cover_path:
        try:
            database.update_mod_cover_path(mid, sidecar.cover_path)
        except Exception:  # noqa: BLE001
            pass

    # File roles: rescan archives and/or remap existing bundle.
    bundle = database.get_mod_files(mid)
    if rescan_archives or not bundle.files:
        scanned = scan_mod_directory(root)
        # Preserve descriptions / ids when filenames match.
        by_name = {
            str(e.filename or Path(e.path or "").name): e for e in bundle.files
        }
        merged: list[ModFileEntry] = []
        for entry in scanned.files:
            name = str(entry.filename or "").strip()
            old = by_name.get(name)
            if old is not None:
                entry.id = old.id
                entry.metadata = dict(old.metadata or {})
                entry.display_name = old.display_name or entry.display_name
                entry.file_role = old.file_role
                entry.selected_for_deploy = old.selected_for_deploy
                entry.enabled = old.enabled
            merged.append(entry)
        bundle = ModFilesBundle(files=merged)

    if sidecar.file_roles:
        plat = sidecar.source_type or normalize_platform(
            getattr(database.get_mod_display_info(mid), "platform", "") or ""
        )
        bundle = _apply_roles_to_bundle(bundle, sidecar.file_roles, platform=plat)

    try:
        JsonMgr(database).replace_all(mid, bundle)
    except Exception as exc:  # noqa: BLE001
        logger.warning("apply sidecar file_roles failed: %s", exc)

    # Offline path lives on mod.json primarily.
    if sidecar.offline_page_path:
        try:
            from services.file_ops import ModFileManager

            mgr = ModFileManager(root.parent.parent)
            meta = mgr.load_metadata(root)
            if meta is not None:
                from services.file_ops import _backfill_mod_runtime_paths

                _backfill_mod_runtime_paths(meta, root)
                meta.offline_page_path = sidecar.offline_page_path
                if sidecar.display_name:
                    meta.title = sidecar.display_name
                if sidecar.description:
                    meta.description = sidecar.description
                if sidecar.cover_path:
                    meta.cover_path = sidecar.cover_path
                mgr.save_metadata(meta, root)
        except Exception:  # noqa: BLE001
            pass

    return True


def merge_archive_scan_with_existing(
    managed_path: str | Path,
    existing: ModFilesBundle | None,
) -> ModFilesBundle:
    """
    Rescan archives under *managed_path* and merge roles/notes by filename.

    Pure-directory Mods (no archives) → empty bundle.
    """
    from services.importers.local_scanner import scan_mod_directory

    root = Path(managed_path)
    scanned = scan_mod_directory(root)
    by_name: dict[str, ModFileEntry] = {}
    if existing is not None:
        for entry in existing.files:
            name = str(entry.filename or Path(entry.path or "").name or "").strip()
            if name:
                by_name[name] = entry
    merged: list[ModFileEntry] = []
    for entry in scanned.files:
        name = str(entry.filename or "").strip()
        old = by_name.get(name)
        if old is not None:
            entry.id = old.id or entry.id
            entry.file_role = old.file_role
            entry.source_type = old.source_type or entry.source_type
            entry.display_name = old.display_name or entry.display_name
            entry.name = old.name or entry.name
            entry.metadata = dict(old.metadata or {})
            entry.selected_for_deploy = old.selected_for_deploy
            entry.enabled = old.enabled
            entry.type = old.type or entry.type
        merged.append(entry)
    return ModFilesBundle(files=merged)


def rescan_mod_folder(
    managed_path: str | Path,
    *,
    mod_id: int | str | None = None,
    db=None,
) -> ModFilesBundle:
    """
    Physical rescan for Detail refresh: archives only + sidecar restore + persist.
    """
    from core.db_manager import get_db
    from services.mod_files import ModFileManager as JsonMgr

    root = Path(managed_path)
    database = db if db is not None else get_db()
    mid = str(mod_id or "").strip()
    if not mid:
        sidecar = load_info_sidecar(root)
        if sidecar and sidecar.published_file_id:
            mid = sidecar.published_file_id
    if not mid:
        data = read_info_metadata_dict(root)
        if data:
            mid = str(data.get("published_file_id") or "").strip()

    existing = database.get_mod_files(mid) if mid and mid.isdigit() else ModFilesBundle()
    # Prefer sidecar roles when present (portable copy).
    sidecar = load_info_sidecar(root)
    if sidecar is not None and mid and mid.isdigit():
        apply_sidecar_to_db(
            root, mod_id=mid, db=database, rescan_archives=True
        )
        bundle = database.get_mod_files(mid)
    else:
        bundle = merge_archive_scan_with_existing(root, existing)
        if mid and mid.isdigit():
            JsonMgr(database).replace_all(mid, bundle)

    if mid and mid.isdigit():
        write_sidecar_for_mod(root, mid, db=database)
    return bundle
