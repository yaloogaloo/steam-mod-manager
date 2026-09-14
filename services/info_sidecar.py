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
    is_internal_mod_id,
    normalize_file_role,
    normalize_platform,
    normalize_platform_if_known,
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
    """Portable ``.info`` snapshot.

    Filesystem binding: ``internal_id`` (required after Sync/Import registration).
    Same value as Entity ``mods.internal_id`` — not a separate Mod ID.
    User display: ``workspace_id``.
    Platform identity: ``url`` / ``published_file_id`` (Steam Workshop ID for
    recovery — not a second user-facing Mod ID / not a runtime locator).
    """

    display_name: str = ""
    description: str = ""
    source_type: str = ""
    url: str = ""
    workspace_id: str = ""
    custom_deploy_path: str = ""
    offline_page_path: str = ""
    cover_path: str = ""
    # Steam Workshop ID for technical recovery only — not a second user Mod ID.
    published_file_id: str = ""
    # Same Entity.internal_id persisted on disk (``.info/metadata.json``).
    internal_id: str = ""
    category: str = ""
    # Workspace IDs this Mod depends on (deploy-before list).
    dependencies: list[str] = field(default_factory=list)
    file_roles: dict[str, str] = field(default_factory=dict)
    # Witcher 3 ONLY: original | next_gen | remake. Omit when empty (other games).
    # Never confuse with Mod.io ``version`` / Steam revision / mod_version.
    game_version: str = ""

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
        gv = str(data.get("game_version") or "").strip()
        if gv:
            data["game_version"] = gv
        else:
            data.pop("game_version", None)
        # Canonical write: internal_id only (strip temporary legacy entity_key).
        from services.mod_identity import set_info_internal_id

        key = str(data.pop("internal_id", "") or "").strip()
        data = set_info_internal_id(data, key)
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
        from core.witcher3_game_version import is_valid_witcher3_game_version
        from services.mod_identity import read_internal_id

        raw_gv = str(raw.get("game_version") or "").strip()
        # Never read Mod.io / Steam ``version`` into this field.
        game_version = raw_gv if is_valid_witcher3_game_version(raw_gv) else ""
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
            internal_id=read_internal_id(raw),
            category=str(raw.get("category") or "").strip(),
            dependencies=dependencies,
            file_roles=roles,
            game_version=game_version,
        )


def info_sidecar_path(managed_path: str | Path) -> Path:
    """Canonical ``.info/metadata.json`` path (write target)."""
    return Path(managed_path) / INFO_DIR_NAME / METADATA_FILENAME


def load_info_sidecar(managed_path: str | Path) -> InfoSidecar | None:
    data = read_info_metadata_dict(managed_path)
    if not data:
        return None
    return InfoSidecar.from_dict(data)


def save_info_sidecar(
    managed_path: str | Path,
    sidecar: InfoSidecar,
    *,
    sync_backup: bool = True,
    sync_reason: str = "edit",
) -> Path:
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
    return persist_unified_metadata_dict(
        root, merged, sync_backup=sync_backup, sync_reason=sync_reason
    )


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
    game_version = ""
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
        source_type = normalize_platform_if_known(info.platform) or str(info.platform or "")
        url = str(info.source_url or "").strip()
        from services.identity_service import persist_workspace_id, sidecar_published_file_id

        workspace_id = persist_workspace_id(
            platform=source_type,
            mod_id=info.mod_id,
            workspace_id=str(info.workspace_id or ""),
            source_url=url,
            external_id=str(info.external_id or ""),
        )
        custom_deploy_path = str(info.custom_deploy_path or "").strip()
        cover_path = str(info.cover_path or "").strip()
        published = sidecar_published_file_id(
            mod_id=info.mod_id,
            platform=source_type,
            external_id=str(info.external_id or ""),
        )
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
                )
                from core.mod_platform import is_internal_mod_id

                if is_internal_mod_id(wid):
                    wid = ""
                if not wid and tid and not is_internal_mod_id(tid):
                    wid = tid
                if wid not in dependencies:
                    dependencies.append(wid)
        except Exception:  # noqa: BLE001
            pass
        from core.witcher3_game_version import (
            is_valid_witcher3_game_version,
            is_witcher3_game,
        )

        if is_witcher3_game("", getattr(info, "app_id", 0)) and is_valid_witcher3_game_version(
            getattr(info, "game_version", "")
        ):
            game_version = str(info.game_version).strip()
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

    proof_internal = ""
    if info is not None:
        row = None
        try:
            row = database.get_mod_backup_row(mod_id)
        except Exception:  # noqa: BLE001
            row = None
        proof_internal = str((row or {}).get("internal_id") or "").strip()
        # Never collapse empty TEXT onto str(mod_id). Create mints a durable
        # UUID before sidecar write. Existing collapsed rows keep their value.

    sidecar = InfoSidecar(
        display_name=display_name,
        description=description,
        source_type=source_type,
        url=url,
        workspace_id=workspace_id,
        custom_deploy_path=custom_deploy_path,
        offline_page_path=offline_page_path,
        cover_path=cover_path,
        published_file_id=published,
        # Same Entity.internal_id persisted on disk (not a third Mod ID).
        internal_id=proof_internal,
        category=category,
        dependencies=dependencies,
        file_roles=file_roles_from_bundle(bundle),
        game_version=game_version,
    )
    return sidecar


def write_sidecar_for_mod(
    managed_path: str | Path,
    mod_id: int | str | None = None,
    *,
    db=None,
    sync_backup: bool = True,
    sync_reason: str = "edit",
) -> Path | None:
    """Persist current DB state into ``.info/metadata.json``."""
    root = Path(managed_path)
    if not root.is_dir():
        return None
    mid = str(mod_id or "").strip()
    if not mid:
        data = read_info_metadata_dict(root)
        if data:
            from services.mod_identity import read_internal_id

            mid = read_internal_id(data)
            if not mid.isdigit():
                mid = ""
    if not mid or not mid.isdigit():
        return None
    try:
        sidecar = build_sidecar_from_db(mid, root, db=db)
        path = save_info_sidecar(
            root, sidecar, sync_backup=sync_backup, sync_reason=sync_reason
        )
        # Ensure entity proof + registration axes are present on disk.
        from core.db_manager import get_db
        from services.mod_identity import set_info_internal_id

        database = db if db is not None else get_db()
        info = database.get_mod_display_info(mid)
        patch: dict[str, Any] = {}
        if sidecar.internal_id:
            patch = set_info_internal_id(patch, sidecar.internal_id)
        if info is not None:
            if int(getattr(info, "app_id", 0) or 0) > 0:
                patch["app_id"] = int(info.app_id)
            plat = normalize_platform_if_known(info.platform) or str(info.platform or "")
            if plat:
                patch["platform"] = plat
                patch[METADATA_SOURCE_TYPE_KEY] = plat
            ws = str(info.workspace_id or "").strip()
            if ws:
                patch["workspace_id"] = ws
        if patch:
            base = read_info_metadata_dict(root) or {}
            merged = _merge_metadata_patch(base, patch)
            persist_unified_metadata_dict(
                root, merged, sync_backup=False, sync_reason=sync_reason
            )
        return path
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write info sidecar for %s: %s", root, exc)
        return None


def ensure_registration_info_proof(
    managed_path: str | Path,
    mod_id: int | str,
    *,
    db=None,
) -> None:
    """
    After Sync/Import entity registration: require ``.info`` with
    ``internal_id`` + ``workspace_id`` (when DB has a workspace).

    ``.info/internal_id`` must equal Entity ``mods.internal_id`` — same
    business identity persisted on disk, not a separate Mod ID.

    Raises ``ValueError`` when proof cannot be written — DB entity must not
    remain without disk identity proof. Never writes external/workshop-only
    sidecar as sole identity. Never writes workspace_id / external_id alone.
    """
    from services.mod_identity import read_internal_id, set_info_internal_id

    mid = str(mod_id or "").strip()
    root = Path(managed_path)
    if not mid.isdigit() or not root.is_dir():
        raise ValueError("registration failed: missing managed folder or mod_id")
    written = write_sidecar_for_mod(
        root, mid, db=db, sync_backup=False, sync_reason="import"
    )
    data = read_info_metadata_dict(root) or {}
    disk_key = read_internal_id(data)
    if not disk_key:
        raise ValueError(
            f"registration failed: .info missing internal_id for mod_id={mid}"
        )
    try:
        from core.db_manager import get_db

        database = db if db is not None else get_db()
        info = database.get_mod_display_info(mid)
        expected_ws = str(getattr(info, "workspace_id", "") or "").strip() if info else ""
        row = database.get_mod_backup_row(mid) or {}
        expected_key = str(row.get("internal_id") or "").strip() or mid
    except Exception:  # noqa: BLE001
        expected_ws = ""
        expected_key = mid
    if expected_key and disk_key != expected_key:
        # Force Frozen Entity.internal_id onto disk when mismatched.
        patch = set_info_internal_id(dict(data), expected_key)
        if expected_ws:
            patch["workspace_id"] = expected_ws
        from services.file_ops import persist_unified_metadata_dict

        persist_unified_metadata_dict(
            root, patch, sync_backup=False, sync_reason="import"
        )
        data = read_info_metadata_dict(root) or {}
        disk_key = read_internal_id(data)
        if disk_key != expected_key:
            raise ValueError(
                f"registration failed: .info internal_id mismatch "
                f"disk={disk_key!r} db_internal_id={expected_key!r} for mod_id={mid}"
            )
    disk_ws = str(data.get("workspace_id") or "").strip()
    if expected_ws and disk_ws != expected_ws:
        raise ValueError(
            f"registration failed: .info missing workspace_id={expected_ws!r} "
            f"for mod_id={mid}"
        )
    if written is None and not root.joinpath(INFO_DIR_NAME).is_dir():
        raise ValueError(
            f"registration failed: could not write .info for mod_id={mid}"
        )


def _apply_roles_to_bundle(
    bundle: ModFilesBundle, roles: Mapping[str, str], *, platform: str
) -> ModFilesBundle:
    """Apply Main/Source labels from sidecar onto scanned/existing entries.

    Each filename keeps its own label. Multiple Main / Source keys are all
    applied — first-wins is not used.
    """
    from services.mod_files import main_role_for_platform, source_role_for_platform

    main_role = main_role_for_platform(platform)
    source_role = source_role_for_platform(platform)
    for entry in bundle.files:
        name = str(entry.filename or Path(entry.path or "").name or "").strip()
        label = roles.get(name, ROLE_OTHER)
        if label == ROLE_MAIN:
            entry.file_role = main_role
            # Role only — never force Main checked (user may uncheck for deploy).
        elif label == ROLE_SOURCE:
            entry.file_role = source_role
            # Source is never deployed.
            entry.set_selection(False)
        elif label == ROLE_OTHER:
            kind = _badge_kind(entry)
            if kind in (ROLE_MAIN, ROLE_SOURCE):
                entry.file_role = FILE_ROLE_UNKNOWN
                # Keep existing selection; do not lock checkboxes.
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
    from services.identity_service import lifecycle_scope
    from services.importers.local_scanner import scan_mod_directory
    from services.mod_files import ModFileManager as JsonMgr

    root = Path(managed_path)
    sidecar = load_info_sidecar(root)
    if sidecar is None:
        return False

    database = db if db is not None else get_db()
    pub = str(sidecar.published_file_id or "").strip()
    if is_internal_mod_id(pub):
        pub = ""
    # Entity PK only — never fall back to Workshop published_file_id.
    mid = str(mod_id or getattr(sidecar, "internal_id", "") or "").strip()
    if not mid or not mid.isdigit():
        return False

    with lifecycle_scope("sidecar"):
        existing = database.get_mod_display_info(mid)
        if existing is None:
            logger.warning("apply_sidecar_to_db refused create for missing mod_id=%s", mid)
            return False
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
    if sidecar.custom_deploy_path:
        patch["custom_deploy_path"] = sidecar.custom_deploy_path
    elif existing is not None:
        patch["custom_deploy_path"] = existing.custom_deploy_path or ""
    from core.witcher3_game_version import (
        is_valid_witcher3_game_version,
        is_witcher3_game,
    )

    if (
        existing is not None
        and is_witcher3_game("", getattr(existing, "app_id", 0))
        and is_valid_witcher3_game_version(sidecar.game_version)
    ):
        patch["game_version"] = sidecar.game_version
    try:
        database.update_mod_user_metadata(mid, patch)
    except Exception as exc:  # noqa: BLE001
        logger.warning("apply sidecar metadata failed: %s", exc)

    try:
        from services.identity_service import persist_identity
        from services.mod_identity import source_url_embeds_internal

        ident_patch: dict[str, Any] = {}
        url = str(sidecar.url or "").strip()
        if url and not source_url_embeds_internal(url, internal_pk=mid):
            ident_patch["source_url"] = url
        ws = str(sidecar.workspace_id or "").strip()
        if ws and not is_internal_mod_id(ws) and ws != mid:
            ident_patch["workspace_id"] = ws
        plat = str(sidecar.source_type or "").strip()
        if plat and plat != "steam":
            ident_patch["platform"] = plat
        elif plat == "steam" and url and not source_url_embeds_internal(url, internal_pk=mid):
            ident_patch["platform"] = plat
        if ident_patch:
            persist_identity(
                database,
                mid,
                source="sidecar",
                reason="apply_sidecar",
                **ident_patch,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("apply sidecar identity persist failed: %s", exc)

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
        if sidecar is not None:
            mid = str(getattr(sidecar, "internal_id", "") or "").strip()
            # Never treat Workshop published_file_id as entity PK.
    if not mid:
        data = read_info_metadata_dict(root)
        if data:
            from services.mod_identity import read_internal_id

            # Prefer ``.info/internal_id``; temporary legacy entity_key accepted.
            mid = read_internal_id(data)

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
        # Silent Nexus workspace_id wash — never blocks refresh.
        try:
            from core.mod_platform import silent_correct_nexus_workspace_id

            silent_correct_nexus_workspace_id(mid)
        except Exception:  # noqa: BLE001
            pass
        write_sidecar_for_mod(
            root, mid, db=database, sync_backup=True, sync_reason="rescan"
        )
    return bundle
