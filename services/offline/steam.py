"""Steam Workshop offline provider — wraps ``services.archive`` unchanged."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.db_manager import get_db
from core.mod_platform import PLATFORM_STEAM, is_internal_mod_id, normalize_platform
from core.models import ModMetadata
from core.paths import default_mod_library
from services.archive import (
    ARCHIVE_OUTCOME_FAILED,
    ARCHIVE_OUTCOME_RATE_LIMITED,
    ARCHIVE_OUTCOME_SKIPPED,
    ARCHIVE_OUTCOME_SUCCESS,
    ArchiveEnsureResult,
    OfflinePageArchiver,
    is_stub_offline_page,
    is_valid_steam_workshop_page,
)
from services.file_ops import ModFileManager
from services.importers.materialize import find_managed_mod_path
from services.offline.base import (
    OFFLINE_OUTCOME_FAILED,
    OFFLINE_OUTCOME_RATE_LIMITED,
    OFFLINE_OUTCOME_SKIPPED,
    OFFLINE_OUTCOME_SUCCESS,
    OFFLINE_STATUS_ARCHIVED,
    OFFLINE_STATUS_FAILED,
    OfflineProvider,
    OfflineUpdateResult,
    PROVIDER_STEAM_ARCHIVE,
)

_WORKSHOP_ID_IN_URL = re.compile(r"[?&]id=(\d+)", re.IGNORECASE)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _digit_token(value: Any) -> str:
    text = _text(value)
    return text if text.isdigit() else ""


def resolve_steam_workshop_id_for_archive(
    entity_mod_id: str | int,
    *,
    metadata: Any | None = None,
    db: Any | None = None,
    managed_path: str | Path | None = None,
) -> str:
    """
    Resolve the Steam Workshop file id used for archive HTTP.

    Entity ``mods.mod_id`` / ``ModMetadata.published_file_id`` may equal the
    Internal PK after Identity rebuild and must **not** be used as Workshop id
    when ``workspace_id`` / legal ``external_id`` / source URL provide the
    platform registration number.
    """
    mid = _text(entity_mod_id)
    database = db if db is not None else get_db()
    row: dict[str, Any] = {}
    try:
        if mid.isdigit():
            row = dict(database.get_mod_backup_row(mid) or {})
    except Exception:  # noqa: BLE001
        row = {}

    info = None
    try:
        if mid.isdigit():
            info = database.get_mod_display_info(mid)
    except Exception:  # noqa: BLE001
        info = None

    candidates: list[str] = []

    def _push(raw: Any) -> None:
        token = _digit_token(raw)
        if not token:
            return
        # Never treat 9000… Internal PK range as a Workshop id.
        if is_internal_mod_id(token):
            return
        if token not in candidates:
            candidates.append(token)

    # 1) Registration / display number (authoritative after rebuild).
    _push(row.get("workspace_id"))
    _push(getattr(info, "workspace_id", ""))
    if isinstance(metadata, dict):
        _push(metadata.get("workspace_id"))
    else:
        _push(getattr(metadata, "workspace_id", ""))

    # 2) Legal external_id (must not be the Internal PK mirror).
    ext = _digit_token(row.get("external_id"))
    if ext and ext != mid:
        _push(ext)
    if info is not None:
        ext2 = _digit_token(getattr(info, "external_id", ""))
        if ext2 and ext2 != mid:
            _push(ext2)

    # 3) source_url / portable url ?id=
    meta_url = ""
    if isinstance(metadata, ModMetadata):
        meta_url = _text(metadata.url)
    elif isinstance(metadata, dict):
        meta_url = _text(metadata.get("source_url") or metadata.get("url"))
    else:
        meta_url = _text(getattr(metadata, "url", "") or getattr(metadata, "source_url", ""))
    for url in (
        row.get("source_url"),
        getattr(info, "source_url", "") if info is not None else "",
        meta_url,
    ):
        m = _WORKSHOP_ID_IN_URL.search(_text(url))
        if m:
            _push(m.group(1))
    # 4) Sidecar published_file_id / workspace when metadata object lacks them.
    if managed_path is not None:
        try:
            from services.file_ops import read_info_metadata_dict

            data = read_info_metadata_dict(managed_path) or {}
            _push(data.get("workspace_id"))
            pub = _digit_token(data.get("published_file_id"))
            if pub and pub != mid:
                _push(pub)
        except Exception:  # noqa: BLE001
            pass

    # 5) metadata.published_file_id only when it is not the Internal PK stand-in.
    pub_meta = ""
    if isinstance(metadata, ModMetadata):
        pub_meta = _digit_token(metadata.published_file_id)
    elif isinstance(metadata, dict):
        pub_meta = _digit_token(metadata.get("published_file_id"))
    if pub_meta and pub_meta != mid:
        _push(pub_meta)

    if candidates:
        return candidates[0]

    # Last resort: historical Steam PK == Workshop id (pre-rebuild scheme).
    if mid.isdigit() and not is_internal_mod_id(mid):
        return mid
    return ""


def _coerce_ensure(value: Any) -> ArchiveEnsureResult:
    if isinstance(value, ArchiveEnsureResult):
        return value
    path = Path(value)
    if is_valid_steam_workshop_page(path):
        return ArchiveEnsureResult(
            path=path,
            outcome=ARCHIVE_OUTCOME_SUCCESS,
            http_performed=True,
            write_performed=True,
        )
    if is_stub_offline_page(path):
        return ArchiveEnsureResult(
            path=path,
            outcome=ARCHIVE_OUTCOME_FAILED,
            http_performed=True,
            write_performed=True,
            error="Steam offline page is a stub (archive incomplete)",
        )
    return ArchiveEnsureResult(
        path=path,
        outcome=ARCHIVE_OUTCOME_FAILED,
        http_performed=True,
        write_performed=True,
        error="Steam offline page invalid",
    )


class SteamOfflineProvider(OfflineProvider):
    """Delegates to ``OfflinePageArchiver.ensure_offline_page``."""

    def can_handle(self, mod: Any) -> bool:
        platform = getattr(mod, "platform", None)
        if platform is None and isinstance(mod, dict):
            platform = mod.get("platform")
        return normalize_platform(str(platform or "")) == PLATFORM_STEAM

    def get_provider_name(self) -> str:
        return PROVIDER_STEAM_ARCHIVE

    def update_offline_page(
        self,
        mod_id: str | int,
        *,
        managed_path: str | Path | None = None,
        library_root: str | Path | None = None,
        metadata: Any | None = None,
        force_refresh: bool = False,
    ) -> OfflineUpdateResult:
        mid = str(mod_id).strip()
        force = bool(force_refresh)
        root = Path(library_root) if library_root else default_mod_library()
        path = Path(managed_path) if managed_path else find_managed_mod_path(root, mid)
        if path is None:
            raise FileNotFoundError(f"Managed Mod folder not found for mod_id={mid}")

        mgr = ModFileManager(root)
        info_dir = mgr.ensure_info_dir(path)
        meta = metadata
        if meta is None:
            meta = mgr.load_metadata(path)
        if meta is None:
            info = get_db().get_mod_display_info(mid)
            meta = ModMetadata(
                published_file_id=mid,
                title=(info.display_name if info else "") or mid,
                app_id=int(info.app_id) if info else 0,
            )

        # Workshop id ≠ entity Internal PK. Existing archives must refresh with
        # the same resolution as first-time saves (force_refresh path included).
        workshop_id = resolve_steam_workshop_id_for_archive(
            mid,
            metadata=meta,
            db=get_db(),
            managed_path=path,
        )
        if not workshop_id:
            status = OFFLINE_STATUS_FAILED
            error = "无法解析 Steam Workshop ID（workspace_id / source_url 缺失）"
            get_db().update_mod_offline_status(
                mid,
                status=status,
                provider=self.get_provider_name(),
            )
            index = info_dir / "index.html"
            return OfflineUpdateResult(
                mod_id=mid,
                index_path=Path(index),
                status=status,
                provider=self.get_provider_name(),
                error=error,
                outcome=OFFLINE_OUTCOME_FAILED,
                force_refresh=force,
                http_performed=False,
                write_performed=False,
            )

        error = ""
        try:
            with OfflinePageArchiver() as archiver:
                ensured = _coerce_ensure(
                    archiver.ensure_offline_page(
                        info_dir,
                        workshop_id,
                        metadata=meta if isinstance(meta, ModMetadata) else None,
                        force_refresh=force,
                    )
                )
            index = ensured.path
            if ensured.outcome == ARCHIVE_OUTCOME_SKIPPED:
                # Keep prior archived status; do not bump offline_updated_at.
                return OfflineUpdateResult(
                    mod_id=mid,
                    index_path=Path(index),
                    status=OFFLINE_STATUS_ARCHIVED,
                    provider=self.get_provider_name(),
                    error="",
                    outcome=OFFLINE_OUTCOME_SKIPPED,
                    skip_reason=ensured.skip_reason or "cache_hit",
                    force_refresh=force,
                    http_performed=False,
                    write_performed=False,
                )

            if ensured.outcome == ARCHIVE_OUTCOME_RATE_LIMITED:
                status = (
                    OFFLINE_STATUS_ARCHIVED
                    if is_valid_steam_workshop_page(index)
                    else OFFLINE_STATUS_FAILED
                )
                error = ensured.error or "Steam rate limited"
                get_db().update_mod_offline_status(
                    mid,
                    status=status,
                    provider=self.get_provider_name(),
                )
                return OfflineUpdateResult(
                    mod_id=mid,
                    index_path=Path(index),
                    status=status,
                    provider=self.get_provider_name(),
                    error=error,
                    outcome=OFFLINE_OUTCOME_RATE_LIMITED,
                    force_refresh=force,
                    http_performed=ensured.http_performed,
                    write_performed=ensured.write_performed,
                )

            if ensured.outcome == ARCHIVE_OUTCOME_FAILED or is_stub_offline_page(index):
                status = OFFLINE_STATUS_FAILED
                error = (
                    ensured.error
                    or "Steam offline page is a stub (archive incomplete)"
                )
            elif ensured.outcome == ARCHIVE_OUTCOME_SUCCESS and (
                is_valid_steam_workshop_page(index)
            ):
                status = OFFLINE_STATUS_ARCHIVED
            else:
                status = OFFLINE_STATUS_FAILED
                error = ensured.error or "Steam offline archive did not succeed"
        except Exception as exc:  # noqa: BLE001
            status = OFFLINE_STATUS_FAILED
            error = str(exc)
            index = info_dir / "index.html"
            get_db().update_mod_offline_status(
                mid,
                status=status,
                provider=self.get_provider_name(),
            )
            if not index.is_file():
                raise
            return OfflineUpdateResult(
                mod_id=mid,
                index_path=Path(index),
                status=status,
                provider=self.get_provider_name(),
                error=error,
                outcome=OFFLINE_OUTCOME_FAILED,
                force_refresh=force,
                http_performed=False,
                write_performed=False,
            )

        get_db().update_mod_offline_status(
            mid,
            status=status,
            provider=self.get_provider_name(),
        )
        outcome = (
            OFFLINE_OUTCOME_SUCCESS
            if status == OFFLINE_STATUS_ARCHIVED
            else OFFLINE_OUTCOME_FAILED
        )
        return OfflineUpdateResult(
            mod_id=mid,
            index_path=Path(index),
            status=status,
            provider=self.get_provider_name(),
            error=error,
            outcome=outcome,
            force_refresh=force,
            http_performed=ensured.http_performed,
            write_performed=ensured.write_performed,
        )
