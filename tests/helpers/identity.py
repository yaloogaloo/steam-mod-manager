"""Canonical test identity seeds — call production IdentityService only.

Ordinary tests must not mint ``mods`` rows via ``upsert_mod``, raw SQL INSERT,
or filesystem-only folders. Use this module.

Repair / pollution forensic tests may still INSERT illegal rows explicitly;
do not route those through these helpers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.mod_platform import PLATFORM_OTHER, PLATFORM_STEAM
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import (
    create_mod_identity,
    identity_create_scope,
)
from services.mod_identity_authority import ResolvedIdentity


@dataclass(frozen=True)
class SeededMod:
    """Managed folder + identity handles for a test entity."""

    folder: Path
    internal_id: str
    workspace_id: str
    external_id: str
    identity: ResolvedIdentity


def create_test_mod_identity(
    db: Any,
    *,
    platform: str,
    title: str,
    external_id: str = "",
    workshop_id: str = "",
    source_url: str = "",
    app_id: int = 0,
    game_name: str = "",
    operation: str = "import",
) -> ResolvedIdentity:
    """Create a new Mod entity through the sole production create boundary."""
    with identity_create_scope():
        return create_mod_identity(
            db,
            platform=platform,
            external_id=str(external_id or workshop_id or ""),
            workshop_id=str(workshop_id or external_id or ""),
            source_url=source_url,
            title=title,
            app_id=int(app_id or 0),
            game_name=game_name,
            operation=operation,
        )


def create_steam_test_mod(
    db: Any,
    *,
    external_id: str,
    title: str,
    app_id: int = 0,
    game_name: str = "",
    source_url: str = "",
    operation: str = "import",
) -> ResolvedIdentity:
    """Steam Workshop entity (Workspace ID = Workshop ID under current contract)."""
    wid = str(external_id).strip()
    return create_test_mod_identity(
        db,
        platform=PLATFORM_STEAM,
        external_id=wid,
        workshop_id=wid,
        title=title,
        app_id=app_id,
        game_name=game_name,
        source_url=source_url,
        operation=operation,
    )


def create_other_test_mod(
    db: Any,
    *,
    title: str,
    external_id: str = "",
    source_url: str = "",
    app_id: int = 0,
    game_name: str = "",
    operation: str = "import",
) -> ResolvedIdentity:
    """Non-platform / local entity (system-generated workspace_id)."""
    return create_test_mod_identity(
        db,
        platform=PLATFORM_OTHER,
        external_id=external_id,
        title=title,
        app_id=app_id,
        game_name=game_name,
        source_url=source_url,
        operation=operation,
    )


def bind_managed_path(
    db: Any,
    internal_id: str | int,
    folder: Path,
    *,
    game_name: str | None = None,
    title: str | None = None,
) -> None:
    """Attach filesystem path to an already-created identity (no minting)."""
    db.update_mod_identity_fields(
        internal_id,
        last_known_path=str(folder),
        folder_present=True,
        game_name=game_name,
        title=title,
    )


def write_info_sidecar(
    folder: Path,
    *,
    internal_id: str,
    title: str,
    external_id: str = "",
    workspace_id: str = "",
    app_id: int = 0,
    game_name: str = "",
    platform: str = PLATFORM_STEAM,
    extra: Mapping[str, Any] | None = None,
) -> Path:
    """Write ``.info/mod.json`` aligned with identity fields (not a create path)."""
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "internal_id": str(internal_id),
        "published_file_id": str(external_id or ""),
        "external_id": str(external_id or ""),
        "workspace_id": str(workspace_id or external_id or ""),
        "title": title,
        "app_id": int(app_id or 0),
        "game_name": game_name,
        "platform": platform,
    }
    if extra:
        payload.update(dict(extra))
    path = info / METADATA_FILENAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def patch_library_get_db(monkeypatch: Any, db: Any) -> None:
    """Point Library + cache + core accessors at the test DatabaseManager."""
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("services.mod_library_cache.get_db", lambda: db)


def flush_library_search(view: Any) -> None:
    """Bypass search debounce so filter assertions run synchronously."""
    debounce = getattr(view, "_search_debounce", None)
    if debounce is not None:
        try:
            debounce.stop()
        except Exception:  # noqa: BLE001
            pass
    view._apply_view_filter()


def seed_steam_managed_mod(
    db: Any,
    library: Path,
    *,
    external_id: str,
    title: str,
    game_folder: str,
    app_id: int = 0,
    game_name: str = "",
    files: Mapping[str, str | bytes] | None = None,
    folder_name: str | None = None,
    operation: str = "import",
) -> SeededMod:
    """Create Steam identity + managed folder + sidecar + path bind."""
    created = create_steam_test_mod(
        db,
        external_id=str(external_id),
        title=title,
        app_id=app_id,
        game_name=game_name,
        operation=operation,
    )
    internal_id = str(created.mod_id)
    workspace_id = str(created.workspace_id or external_id)
    folder = library / game_folder / (folder_name or title)
    folder.mkdir(parents=True, exist_ok=True)
    write_info_sidecar(
        folder,
        internal_id=internal_id,
        title=title,
        external_id=str(external_id),
        workspace_id=workspace_id,
        app_id=app_id,
        game_name=game_name,
        platform=PLATFORM_STEAM,
    )
    if files:
        for rel, data in files.items():
            path = folder / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, bytes):
                path.write_bytes(data)
            else:
                path.write_text(data, encoding="utf-8")
    bind_managed_path(db, internal_id, folder, game_name=game_name or None, title=title)
    return SeededMod(
        folder=folder,
        internal_id=internal_id,
        workspace_id=workspace_id,
        external_id=str(external_id),
        identity=created,
    )
