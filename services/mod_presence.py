"""LIVE / MISS presence, Backup-backed metadata, and deployment source capability.

MISS is **filesystem entity source missing**. It is not a deploy status, not
``content_missing``, and not Deleted. Do not mkdir the Mod folder while MISS.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from services.file_ops import INFO_DIR_NAME, read_info_metadata_dict
from services.metadata_backup import (
    BACKUP_COVER_BASENAME,
    BACKUP_METADATA_NAME,
    BACKUP_OFFLINE_DIR,
    BACKUP_OFFLINE_INDEX,
    backup_root,
    load_backup,
)

logger = logging.getLogger(__name__)

ENTITY_LIVE = "live"
ENTITY_MISS = "miss"
ENTITY_ABSENT = "absent"

SOURCE_WORKSHOP = "workshop"
SOURCE_LOCAL_FOLDER = "local_mod_folder"

RECOVERY_LIVE = "live"
RECOVERY_BLOCKED = "blocked"
RECOVERY_NO_CANDIDATE = "no_candidate"
RECOVERY_AMBIGUOUS = "ambiguous"
RECOVERY_REDISCOVERED = "rediscovered"


@dataclass(frozen=True)
class DeploymentCapability:
    """UI / Deploy projection. Never inspect Steam paths from widgets."""

    allowed: bool
    source_available: bool
    source_kind: str
    reason: str = ""


@dataclass(frozen=True)
class PresenceProjection:
    """Cheap per-Mod presence + action capability."""

    entity_state: str
    folder_present: bool
    has_valid_backup: bool
    deployment: DeploymentCapability
    open_directory: bool
    edit_metadata: bool
    filesystem_actions: bool


@dataclass(frozen=True)
class RecoveryResult:
    success: bool
    state: str
    reason: str = ""
    internal_id: str = ""
    path: str = ""


class RediscoveryIndex:
    """Per-pass cache of ``internal_id → folders`` for one-level game roots."""

    def __init__(self) -> None:
        self.by_root: dict[str, dict[str, list[Path]]] = {}
        self.live_children: dict[str, set[str]] = {}
        self.roots_scanned: list[str] = []
        self.missing_attempted: int = 0
        self.backup_scanned: bool = False
        self.directory_scans: int = 0
        self.iterdir_entries: int = 0
        self.info_reads: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "missing_attempted": int(self.missing_attempted),
            "roots": list(self.roots_scanned),
            "backup_scanned": bool(self.backup_scanned),
            "directory_scans": int(self.directory_scans),
            "iterdir_entries": int(self.iterdir_entries),
            "info_reads": int(self.info_reads),
        }

    def prime_root(
        self,
        root: Path,
        *,
        live_paths: set[str],
        iid_map: dict[str, list[Path]],
        directory_scans: int = 1,
        iterdir_entries: int = 0,
        info_reads: int = 0,
    ) -> None:
        key = str(root)
        self.by_root[key] = dict(iid_map)
        self.live_children[key] = set(live_paths)
        if key not in self.roots_scanned:
            self.roots_scanned.append(key)
        self.directory_scans += int(directory_scans)
        self.iterdir_entries += int(iterdir_entries)
        self.info_reads += int(info_reads)


_LAST_REDISCOVERY: RediscoveryIndex | None = None


def last_rediscovery_stats() -> dict[str, Any]:
    """Diagnostics for tests: last rediscovery pass (never a scan of Backup)."""
    idx = _LAST_REDISCOVERY
    if idx is None:
        return {"missing_attempted": 0, "roots": [], "backup_scanned": False}
    return idx.as_dict()


def has_valid_backup(mod_id: int | str, *, db: Any | None = None) -> bool:
    """True when the known Backup bucket has ``metadata.json``. No tree scan."""
    mid = str(mod_id or "").strip()
    if not mid.isdigit():
        return False
    del db
    try:
        return (backup_root(mid) / BACKUP_METADATA_NAME).is_file()
    except OSError:
        return False


def is_backup_managed_miss(
    mod_id: int | str,
    *,
    db: Any | None = None,
    last_known_path: str | Path | None = None,
) -> bool:
    """Folder gone + valid Backup → backup-managed MISS. Not a fake MISS."""
    return entity_state(
        mod_id, db=db, last_known_path=last_known_path
    ) == ENTITY_MISS


def entity_state(
    mod_id: int | str,
    *,
    db: Any | None = None,
    last_known_path: str | Path | None = None,
) -> str:
    """Derive LIVE / MISS / ABSENT. Never writes, never mkdir."""
    mid = str(mod_id or "").strip()
    if not mid.isdigit():
        return ENTITY_ABSENT
    path: Path | None = Path(last_known_path) if last_known_path else None
    row: dict[str, Any] | None = None
    try:
        database = db
        if database is None:
            from core.db_manager import get_db

            database = get_db()
        row = database.get_mod_backup_row(mid)
    except Exception:  # noqa: BLE001
        row = None
    if path is None:
        lkp = str((row or {}).get("last_known_path") or "").strip()
        if lkp:
            path = Path(lkp)
    try:
        disk_live = path is not None and path.is_dir()
    except OSError:
        disk_live = False
    present = bool(int((row or {}).get("folder_present") or 0))
    backup = has_valid_backup(mid, db=db)
    if disk_live and present:
        return ENTITY_LIVE
    if backup and not present:
        return ENTITY_MISS
    if disk_live:
        return ENTITY_LIVE
    if backup:
        return ENTITY_MISS
    return ENTITY_ABSENT


def _workshop_deploy_type(app_id: int, deploy_type: str | None) -> bool:
    from services.deploy_rules import (
        DEPLOY_TYPE_STELLARIS,
        DEPLOY_TYPE_WARHAMMER3,
        resolve_deploy_type,
    )

    key = resolve_deploy_type(app_id, deploy_type)
    return key in {DEPLOY_TYPE_STELLARIS, DEPLOY_TYPE_WARHAMMER3}


def workshop_source_available(
    *,
    app_id: int,
    workspace_id: str,
    workshop_path: str | Path | None,
) -> bool:
    """Cheap existence check for ``<workshop-content>/<workspace_id>``."""
    wid = str(workspace_id or "").strip()
    if int(app_id or 0) <= 0 or not wid:
        return False
    root: Path | None = None
    try:
        from services.stellaris_activation import (
            STELLARIS_APP_ID,
            is_stellaris_activation_app,
            workshop_content_root as stellaris_workshop_root,
        )

        if is_stellaris_activation_app(app_id) or int(app_id) == int(STELLARIS_APP_ID):
            root = stellaris_workshop_root(workshop_path)
    except Exception:  # noqa: BLE001
        root = None
    if root is None:
        try:
            from services.wh3_activation import (
                WH3_APP_ID,
                is_wh3_activation_app,
                workshop_content_root as wh3_workshop_root,
            )

            if is_wh3_activation_app(app_id) or int(app_id) == int(WH3_APP_ID):
                root = wh3_workshop_root(str(workshop_path or ""))
        except Exception:  # noqa: BLE001
            root = None
    if root is None:
        return False
    try:
        candidate = root / wid
        return candidate.is_dir()
    except OSError:
        return False


def deployment_capability(
    mod_id: int | str,
    *,
    db: Any | None = None,
) -> DeploymentCapability:
    """Whether Deploy is allowed given game source policy + real source."""
    mid = str(mod_id or "").strip()
    if not mid.isdigit():
        return DeploymentCapability(
            allowed=False,
            source_available=False,
            source_kind=SOURCE_LOCAL_FOLDER,
            reason="invalid_mod_id",
        )
    try:
        database = db
        if database is None:
            from core.db_manager import get_db

            database = get_db()
        row = database.get_mod_backup_row(mid) or {}
    except Exception:  # noqa: BLE001
        row = {}
    try:
        app_id = int(row.get("app_id") or 0)
    except (TypeError, ValueError):
        app_id = 0
    deploy_type = ""
    workshop_path = ""
    if app_id > 0:
        try:
            cfg = database.get_game_deploy_config(app_id)
        except Exception:  # noqa: BLE001
            cfg = None
        if cfg is not None:
            deploy_type = str(getattr(cfg, "deploy_type", "") or "")
            workshop_path = str(getattr(cfg, "workshop_path", "") or "")
    live = entity_state(mid, db=database) == ENTITY_LIVE
    if _workshop_deploy_type(app_id, deploy_type):
        wid = str(row.get("workspace_id") or "").strip()
        available = workshop_source_available(
            app_id=app_id,
            workspace_id=wid,
            workshop_path=workshop_path,
        )
        if available:
            return DeploymentCapability(
                allowed=True,
                source_available=True,
                source_kind=SOURCE_WORKSHOP,
                reason="",
            )
        return DeploymentCapability(
            allowed=False,
            source_available=False,
            source_kind=SOURCE_WORKSHOP,
            reason="workshop_source_missing",
        )
    if live:
        return DeploymentCapability(
            allowed=True,
            source_available=True,
            source_kind=SOURCE_LOCAL_FOLDER,
            reason="",
        )
    return DeploymentCapability(
        allowed=False,
        source_available=False,
        source_kind=SOURCE_LOCAL_FOLDER,
        reason="local_mod_folder_missing",
    )


def presence_projection(
    mod_id: int | str,
    *,
    db: Any | None = None,
) -> PresenceProjection:
    state = entity_state(mod_id, db=db)
    live = state == ENTITY_LIVE
    miss = state == ENTITY_MISS
    cap = deployment_capability(mod_id, db=db)
    return PresenceProjection(
        entity_state=state,
        folder_present=live,
        has_valid_backup=miss or (live and has_valid_backup(mod_id, db=db)),
        deployment=cap,
        open_directory=live,
        edit_metadata=True,
        filesystem_actions=live,
    )


def persist_entity_metadata_to_backup(
    mod_id: int | str,
    *,
    db: Any | None = None,
) -> bool:
    """Write current DB user metadata into Backup. Never mkdir the Mod folder."""
    mid = str(mod_id or "").strip()
    if not mid.isdigit():
        return False
    try:
        database = db
        if database is None:
            from core.db_manager import get_db

            database = get_db()
        row = database.get_mod_backup_row(mid)
        display = database.get_mod_display_info(mid)
    except Exception:  # noqa: BLE001
        logger.debug("persist backup metadata lookup failed", exc_info=True)
        return False
    if row is None or display is None:
        return False

    dest = backup_root(mid)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("cannot create backup bucket for %s: %s", mid, exc)
        return False

    existing: dict[str, Any] = {}
    meta_file = dest / BACKUP_METADATA_NAME
    if meta_file.is_file():
        try:
            parsed = json.loads(meta_file.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                existing = parsed
        except (OSError, json.JSONDecodeError):
            existing = {}
    frozen = str(row.get("internal_id") or "").strip()
    if frozen:
        # Mirror .info binding (``.info/internal_id`` == Entity.internal_id).
        from services.mod_identity import set_info_internal_id

        existing = set_info_internal_id(existing, frozen)
    existing["display_name"] = str(display.user_display_name or display.display_name or "")
    title = str(display.steam_name or existing.get("title") or "").strip()
    if title:
        existing["title"] = title
    desc = str(display.custom_description or "").strip()
    if desc:
        existing["custom_description"] = desc
        existing["description"] = desc
    elif display.steam_description:
        existing.setdefault("description", str(display.steam_description))
    existing["user_notes"] = str(display.user_notes or "")
    existing["favorite"] = bool(display.favorite)
    plat = str(display.platform or existing.get("platform") or "").strip()
    if plat:
        existing["platform"] = plat
        existing["source_type"] = plat
    url = str(display.source_url or "").strip()
    if url:
        existing["source_url"] = url
        existing["url"] = url
    ws = str(display.workspace_id or existing.get("workspace_id") or "").strip()
    if ws:
        existing["workspace_id"] = ws
    ext = str(display.external_id or existing.get("external_id") or "").strip()
    if ext:
        existing["external_id"] = ext
    if int(display.app_id or 0) > 0:
        existing["app_id"] = int(display.app_id)
    cat = str(display.category or "").strip()
    if cat:
        existing["category"] = cat
    updated = ""
    try:
        with database._lock:
            ts_row = database._conn.execute(
                "SELECT updated_at FROM mods WHERE mod_id = ?",
                (int(mid),),
            ).fetchone()
        updated = str((ts_row["updated_at"] if ts_row else "") or "").strip()
    except Exception:  # noqa: BLE001
        updated = ""
    if updated:
        existing["updated_at"] = updated

    text = json.dumps(existing, ensure_ascii=False, indent=2)
    try:
        meta_file.write_text(text, encoding="utf-8")
    except OSError as exc:
        logger.warning("write backup metadata failed for %s: %s", mid, exc)
        return False

    cover_abs = ""
    offline_abs = ""
    try:
        for candidate in sorted(dest.glob(f"{BACKUP_COVER_BASENAME}.*")):
            if candidate.is_file():
                cover_abs = str(candidate.resolve())
                break
        offline = dest / BACKUP_OFFLINE_DIR
        from services.offline.backup_closure import usable_backup_offline_index

        found_off = usable_backup_offline_index(offline)
        if found_off is not None:
            offline_abs = str(found_off.resolve())
    except OSError:
        pass
    lkp = str(row.get("last_known_path") or "").strip()
    try:
        live = bool(lkp) and Path(lkp).is_dir()
    except OSError:
        live = False
    try:
        database.update_mod_backup_snapshot(
            mid,
            last_known_path=lkp,
            folder_present=live,
            backup_metadata_json=text,
            backup_cover_path=cover_abs or str(row.get("backup_cover_path") or ""),
            backup_offline_path=offline_abs or str(row.get("backup_offline_path") or ""),
        )
    except Exception:  # noqa: BLE001
        logger.debug("update backup snapshot after persist failed", exc_info=True)
    return True


def persist_miss_cover(
    mod_id: int | str,
    cover_source: str | Path,
    *,
    db: Any | None = None,
) -> str:
    """Install cover into Backup only. Never create a Mod folder or ``.info``."""
    mid = str(mod_id or "").strip()
    if not mid.isdigit():
        return ""
    from services.importers.image_picker import validate_cover_image
    from services.metadata_backup import _copy_cover

    try:
        src = validate_cover_image(cover_source)
    except (FileNotFoundError, ValueError):
        return ""
    dest = backup_root(mid)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    cover_abs = _copy_cover(src, dest)
    if not cover_abs:
        return ""
    persist_entity_metadata_to_backup(mid, db=db)
    try:
        database = db
        if database is None:
            from core.db_manager import get_db

            database = get_db()
        database.update_mod_cover_path(mid, cover_abs)
    except Exception:  # noqa: BLE001
        logger.debug("persist miss cover path failed", exc_info=True)
    return cover_abs


def backup_offline_index(mod_id: int | str) -> Path | None:
    mid = str(mod_id or "").strip()
    if not mid.isdigit():
        return None
    from services.offline.backup_closure import usable_backup_offline_index

    return usable_backup_offline_index(backup_root(mid) / BACKUP_OFFLINE_DIR)


def _iso_epoch(value: str | None) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0.0


def _independent_descriptor_workspace(folder: Path) -> str:
    """Workspace token from ``descriptor.mod`` — not from ``.info``."""
    desc = folder / "descriptor.mod"
    try:
        if not desc.is_file():
            return ""
        from services.stellaris_activation import parse_mod_descriptor

        parsed = parse_mod_descriptor(desc.read_text(encoding="utf-8", errors="replace"))
        return str(parsed.get("remote_file_id") or "").strip()
    except OSError:
        return ""


def _recovery_evidence(
    *,
    frozen_internal: str,
    info: Mapping[str, Any],
    row: Mapping[str, Any],
    folder: Path,
) -> tuple[bool, str]:
    """Two independent proofs. Both must not come only from the same ``.info``."""
    from services.mod_identity import read_internal_id

    proof = read_internal_id(dict(info))
    if not frozen_internal or not proof or proof != frozen_internal:
        return False, "internal_id_mismatch"
    db_ws = str(row.get("workspace_id") or "").strip()
    info_ws = str(info.get("workspace_id") or "").strip()
    desc_ws = _independent_descriptor_workspace(folder)
    if db_ws and info_ws and db_ws != info_ws:
        return False, "workspace_mismatch"
    if db_ws and desc_ws and db_ws != desc_ws:
        return False, "workspace_mismatch"
    try:
        db_app = int(row.get("app_id") or 0)
    except (TypeError, ValueError):
        db_app = 0
    try:
        info_app = int(info.get("app_id") or 0)
    except (TypeError, ValueError):
        info_app = 0
    if db_app > 0 and info_app > 0 and db_app != info_app:
        return False, "app_id_mismatch"
    db_plat = str(row.get("platform") or row.get("source_type") or "").strip().lower()
    info_plat = str(info.get("platform") or info.get("source_type") or "").strip().lower()
    if db_plat and info_plat and db_plat != info_plat:
        return False, "platform_mismatch"

    evidence_b = False
    if db_ws and info_ws and db_ws == info_ws:
        evidence_b = True
    if db_ws and desc_ws and db_ws == desc_ws:
        evidence_b = True
    if db_app > 0 and info_app > 0 and db_app == info_app and db_plat and info_plat and db_plat == info_plat:
        evidence_b = True
    if not evidence_b:
        return False, "insufficient_evidence"
    return True, "ok"


def _add_rediscovery_root(roots: list[Path], seen: set[str], path: Path) -> None:
    try:
        if not path.is_dir():
            return
        key = str(path.resolve())
    except OSError:
        return
    if key in seen:
        return
    seen.add(key)
    roots.append(Path(key))


def controlled_rediscovery_roots(
    row: Mapping[str, Any],
    *,
    library_root: str | Path | None = None,
    db: Any | None = None,
) -> list[Path]:
    """Game-level managed roots only. Never the whole disk / whole Backup tree."""
    roots: list[Path] = []
    seen: set[str] = set()
    lkp = str(row.get("last_known_path") or "").strip()
    if lkp:
        _add_rediscovery_root(roots, seen, Path(lkp).expanduser().parent)

    lib: Path | None = Path(library_root) if library_root is not None else None
    if lib is None and lkp:
        try:
            maybe = Path(lkp).expanduser().parent.parent
            if maybe.is_dir():
                lib = maybe
        except OSError:
            lib = None
    if lib is None:
        try:
            from core.paths import default_mod_library

            lib = Path(default_mod_library())
        except Exception:  # noqa: BLE001
            lib = None

    folder_name = ""
    try:
        app_id = int(row.get("app_id") or 0)
    except (TypeError, ValueError):
        app_id = 0
    if app_id > 0:
        try:
            database = db
            if database is None:
                from core.db_manager import get_db

                database = get_db()
            game = database.get_game(app_id)
            if game is not None:
                folder_name = str(game.folder_name or game.name or "").strip()
        except Exception:  # noqa: BLE001
            folder_name = ""
    if lib is not None and folder_name:
        _add_rediscovery_root(roots, seen, Path(lib) / folder_name)
    return roots


def _index_game_root(root: Path, index: RediscoveryIndex | None = None) -> dict[str, list[Path]]:
    """One-level ``internal_id`` index. Does not walk payload or Backup."""
    from services.mod_identity import read_internal_id

    mapping: dict[str, list[Path]] = {}
    try:
        children = list(root.iterdir())
    except OSError:
        return mapping
    if index is not None:
        index.directory_scans += 1
        index.iterdir_entries += len(children)
    live: set[str] = set()
    for child in children:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        try:
            live.add(str(child.resolve()))
        except OSError:
            live.add(str(child))
        try:
            info = child / INFO_DIR_NAME
            if not info.is_dir():
                continue
            if index is not None:
                index.info_reads += 1
            iid = read_internal_id(read_info_metadata_dict(child) or {})
        except OSError:
            continue
        if not iid:
            continue
        mapping.setdefault(iid, []).append(child)
    if index is not None:
        index.live_children[str(root)] = live
    return mapping


def _root_index(root: Path, index: RediscoveryIndex) -> dict[str, list[Path]]:
    key = str(root)
    cached = index.by_root.get(key)
    if cached is not None:
        return cached
    built = _index_game_root(root, index)
    index.by_root[key] = built
    index.roots_scanned.append(key)
    return built


def _adopt_rediscovered_path(mid: str, folder: Path, *, db: Any) -> bool:
    """Rebind storage path only. Never mint identity, never rewrite Backup/.info."""
    try:
        resolved = folder.resolve()
    except OSError:
        return False
    try:
        db.update_mod_identity_fields(
            mid,
            last_known_path=str(resolved),
            folder_present=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("rediscovery rebind failed mid=%s path=%s: %s", mid, resolved, exc)
        return False
    try:
        from services.managed_path_cache import remember_resolved

        remember_resolved(mid, resolved)
    except Exception:  # noqa: BLE001
        pass
    logger.info("rediscovered managed path mid=%s path=%s", mid, resolved)
    return True


def rediscover_entity_path(
    mod_id: int | str,
    *,
    db: Any | None = None,
    library_root: str | Path | None = None,
    index: RediscoveryIndex | None = None,
    row: Mapping[str, Any] | None = None,
) -> RecoveryResult:
    """
    Find the same Entity after ``last_known_path`` vanished (rename / move).

    Evidence A: ``candidate/.info/internal_id == mods.internal_id``
    (same Entity identity persisted on disk — not a third Mod ID).
    Evidence B: existing recovery independent proof (workspace / descriptor /
    platform+app_id). Folder names are never identity.

    Unique A+B match rebinds ``last_known_path`` and stays LIVE. Conflicts,
    multiples, or missing proof do not adopt.
    """
    global _LAST_REDISCOVERY
    mid = str(mod_id or "").strip()
    cache = index if index is not None else RediscoveryIndex()
    _LAST_REDISCOVERY = cache
    if not mid.isdigit():
        return RecoveryResult(success=False, state=RECOVERY_NO_CANDIDATE, reason="invalid_mod_id")
    cache.missing_attempted += 1
    try:
        database = db
        if database is None:
            from core.db_manager import get_db

            database = get_db()
        if row is None:
            row = database.get_mod_backup_row(mid)
    except Exception:  # noqa: BLE001
        return RecoveryResult(success=False, state=RECOVERY_NO_CANDIDATE, reason="lookup_failed")
    if row is None:
        return RecoveryResult(success=False, state=RECOVERY_NO_CANDIDATE, reason="no_row")
    frozen = str(row.get("internal_id") or "").strip()
    if not frozen:
        return RecoveryResult(
            success=False,
            state=RECOVERY_NO_CANDIDATE,
            reason="no_internal_id",
            internal_id=frozen,
        )

    lkp = str(row.get("last_known_path") or "").strip()
    skip_live_stat = False
    if lkp and cache.live_children:
        try:
            parent_key = str(Path(lkp).expanduser().resolve().parent)
            lkp_key = str(Path(lkp).expanduser().resolve())
        except OSError:
            parent_key = ""
            lkp_key = lkp
        live = cache.live_children.get(parent_key)
        if live is not None and lkp_key not in live:
            skip_live_stat = True
    if not skip_live_stat:
        try:
            if lkp and Path(lkp).is_dir():
                return RecoveryResult(
                    success=False,
                    state=RECOVERY_NO_CANDIDATE,
                    reason="path_still_present",
                    internal_id=frozen,
                    path=lkp,
                )
        except OSError:
            pass

    evidence_hits: list[Path] = []
    iid_hits: list[Path] = []
    conflict_reason = ""
    for root in controlled_rediscovery_roots(row, library_root=library_root, db=database):
        grouped = _root_index(root, cache)
        for folder in grouped.get(frozen, ()):
            if folder not in iid_hits:
                iid_hits.append(folder)
            info = read_info_metadata_dict(folder) or {}
            ok, why = _recovery_evidence(
                frozen_internal=frozen, info=info, row=row, folder=folder
            )
            if ok:
                if folder not in evidence_hits:
                    evidence_hits.append(folder)
            else:
                conflict_reason = why or "evidence_conflict"
                logger.info(
                    "rediscovery rejected mid=%s path=%s reason=%s",
                    mid,
                    folder,
                    why,
                )

    if len(iid_hits) > 1:
        logger.warning(
            "rediscovery ambiguous mid=%s frozen=%s candidates=%s",
            mid,
            frozen,
            [str(p) for p in iid_hits],
        )
        return RecoveryResult(
            success=False,
            state=RECOVERY_AMBIGUOUS,
            reason="ambiguous_candidates",
            internal_id=frozen,
        )
    if len(iid_hits) == 1 and not evidence_hits:
        return RecoveryResult(
            success=False,
            state=RECOVERY_BLOCKED,
            reason=conflict_reason or "evidence_conflict",
            internal_id=frozen,
            path=str(iid_hits[0]),
        )
    if len(evidence_hits) != 1:
        return RecoveryResult(
            success=False,
            state=RECOVERY_NO_CANDIDATE,
            reason="no_candidate",
            internal_id=frozen,
        )
    folder = evidence_hits[0]
    if not _adopt_rediscovered_path(mid, folder, db=database):
        return RecoveryResult(
            success=False,
            state=RECOVERY_BLOCKED,
            reason="rebind_failed",
            internal_id=frozen,
            path=str(folder),
        )
    return RecoveryResult(
        success=True,
        state=RECOVERY_REDISCOVERED,
        reason="rediscovered",
        internal_id=frozen,
        path=str(folder.resolve()),
    )


def _apply_backup_display_to_db(mid: str, backup_meta: Mapping[str, Any], *, db: Any) -> None:
    display = db.get_mod_display_info(mid)
    payload = {
        "display_name": str(
            backup_meta.get("display_name")
            or (display.user_display_name if display else "")
            or ""
        ),
        "custom_description": str(
            backup_meta.get("custom_description")
            or backup_meta.get("description")
            or (display.custom_description if display else "")
            or ""
        ),
        "user_notes": str(
            backup_meta.get("user_notes")
            or (display.user_notes if display else "")
            or ""
        ),
        "favorite": bool(
            backup_meta.get("favorite")
            if "favorite" in backup_meta
            else (display.favorite if display else False)
        ),
        "platform": str(
            backup_meta.get("platform")
            or backup_meta.get("source_type")
            or (display.platform if display else "")
            or ""
        ),
        "source_url": str(
            backup_meta.get("source_url")
            or backup_meta.get("url")
            or (display.source_url if display else "")
            or ""
        ),
        "category": str(
            backup_meta.get("category")
            or (display.category if display else "")
            or ""
        ),
    }
    db.update_mod_user_metadata(mid, payload)


def attempt_recovery(
    mod_id: int | str,
    *,
    library_root: str | Path | None = None,
    db: Any | None = None,
) -> RecoveryResult:
    """
    Rebind last_known_path only when two independent proofs match.

    Never scans the whole library. Never mints ``internal_id``. Latest
    semantic ``updated_at`` wins over a stale restored ``.info``.
    When ``last_known_path`` is gone, controlled rediscovery runs first.
    """
    mid = str(mod_id or "").strip()
    if not mid.isdigit():
        return RecoveryResult(success=False, state=RECOVERY_NO_CANDIDATE, reason="invalid_mod_id")
    try:
        database = db
        if database is None:
            from core.db_manager import get_db

            database = get_db()
        row = database.get_mod_backup_row(mid)
    except Exception:  # noqa: BLE001
        return RecoveryResult(success=False, state=RECOVERY_NO_CANDIDATE, reason="lookup_failed")
    if row is None:
        return RecoveryResult(success=False, state=RECOVERY_NO_CANDIDATE, reason="no_row")
    frozen = str(row.get("internal_id") or "").strip()
    lkp = str(row.get("last_known_path") or "").strip()
    if not lkp:
        found = rediscover_entity_path(
            mid, db=database, library_root=library_root
        )
        if found.success:
            return found
        return RecoveryResult(
            success=False,
            state=RECOVERY_NO_CANDIDATE,
            reason="no_last_known_path",
            internal_id=frozen,
        )
    folder = Path(lkp)
    try:
        path_live = folder.is_dir()
    except OSError:
        path_live = False
    if not path_live:
        found = rediscover_entity_path(
            mid, db=database, library_root=library_root
        )
        if found.success:
            return found
        return RecoveryResult(
            success=False,
            state=found.state if found.state else RECOVERY_NO_CANDIDATE,
            reason=found.reason or "folder_missing",
            internal_id=frozen,
            path=lkp,
        )

    info = read_info_metadata_dict(folder) or {}
    ok, why = _recovery_evidence(
        frozen_internal=frozen, info=info, row=row, folder=folder
    )
    if not ok:
        return RecoveryResult(
            success=False,
            state=RECOVERY_BLOCKED,
            reason=why,
            internal_id=frozen,
            path=str(folder),
        )

    db_ts = 0.0
    try:
        with database._lock:
            ts_row = database._conn.execute(
                "SELECT updated_at FROM mods WHERE mod_id = ?",
                (int(mid),),
            ).fetchone()
        db_ts = _iso_epoch(str((ts_row["updated_at"] if ts_row else "") or ""))
    except Exception:  # noqa: BLE001
        db_ts = 0.0
    snap = load_backup(mid)
    bak_meta: dict[str, Any] = dict(snap.metadata) if snap is not None else {}
    bak_ts = max(
        _iso_epoch(str(row.get("backup_updated_at") or "")),
        _iso_epoch(str(bak_meta.get("updated_at") or "")),
    )
    info_ts = _iso_epoch(str(info.get("updated_at") or ""))

    winner = "db"
    if bak_ts >= db_ts and bak_ts >= info_ts:
        winner = "backup"
    elif db_ts >= bak_ts and db_ts >= info_ts:
        winner = "db"
    else:
        winner = "info"

    try:
        if winner == "backup" and bak_meta:
            _apply_backup_display_to_db(mid, bak_meta, db=database)
            persist_entity_metadata_to_backup(mid, db=database)
        elif winner == "db":
            persist_entity_metadata_to_backup(mid, db=database)
        else:
            from services.metadata_backup_sync import sync_after_metadata_change

            sync_after_metadata_change(mid, folder, "restore", wait=True)
            _apply_backup_display_to_db(mid, info, db=database)

        from services.info_sidecar import write_sidecar_for_mod

        write_sidecar_for_mod(folder, mid, db=database, sync_backup=False)
        persist_entity_metadata_to_backup(mid, db=database)
        database.update_mod_identity_fields(
            mid,
            last_known_path=str(folder.resolve()),
            folder_present=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("recovery apply failed for %s: %s", mid, exc)
        return RecoveryResult(
            success=False,
            state=RECOVERY_BLOCKED,
            reason=f"apply_failed:{exc}",
            internal_id=frozen,
            path=str(folder),
        )
    return RecoveryResult(
        success=True,
        state=RECOVERY_LIVE,
        reason=winner,
        internal_id=frozen,
        path=str(folder.resolve()),
    )
