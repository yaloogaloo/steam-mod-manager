"""Canonical test identity seeds — call production IdentityService only.

Ordinary tests must not mint ``mods`` rows via ``upsert_mod``, raw SQL INSERT,
or filesystem-only folders. Use this module.

Identity contract for tests (matches production):

```
workspace_id  = Steam Workshop ID / Nexus ID / …
mods.mod_id   = SQLite-generated PK (never forced to Workshop ID)
internal_id   = durable Entity identity (UUID TEXT)
.info/internal_id = same Entity internal_id
```

Repair / pollution forensic tests may still INSERT illegal rows explicitly;
do not route those through these helpers.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.mod_platform import PLATFORM_MODIO, PLATFORM_OTHER, PLATFORM_STEAM
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
    mod_id: str  # SQLite PK / FK handle
    entity_internal_id: str  # durable Entity identity (== .info/internal_id)
    workspace_id: str
    external_id: str
    identity: ResolvedIdentity

    @property
    def internal_id(self) -> str:
        """Deprecated alias of :attr:`mod_id` (historical misname). Prefer ``mod_id``."""
        return self.mod_id


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
    """Steam Workshop entity.

    Returns ``ResolvedIdentity`` where:

    * ``workspace_id`` / ``external_id`` = Workshop ID (*external_id*)
    * ``mod_id`` = database-generated SQLite PK (not Workshop ID)
    * ``internal_id`` = durable Entity identity
    """
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


def create_modio_test_mod(
    db: Any,
    *,
    external_id: str,
    title: str,
    source_url: str,
    app_id: int,
    game_name: str = "",
    operation: str = "import",
) -> ResolvedIdentity:
    """Mod.io entity — ``workspace_id``/``external_id`` = platform id; PK is DB-generated."""
    return create_test_mod_identity(
        db,
        platform=PLATFORM_MODIO,
        external_id=str(external_id).strip(),
        title=title,
        source_url=source_url,
        app_id=int(app_id or 0),
        game_name=game_name,
        operation=operation,
    )


def resolve_test_mod_pk(db: Any, handle: str | int) -> str:
    """Resolve a test handle to ``mods.mod_id`` (SQLite PK).

    Accepts, in order:

    1. Existing ``mods.mod_id`` digit PK
    2. Existing ``mods.internal_id`` (Entity identity)
    3. Unique ``workspace_id`` / ``external_id`` match (lookup only — never
       invents a row, never assigns Workshop ID as PK)

    Raises ``AssertionError`` when unresolved or ambiguous.
    """
    token = str(handle or "").strip()
    if not token:
        raise AssertionError("resolve_test_mod_pk: empty handle")
    resolved = soft_resolve_test_mod_pk(db, token)
    if resolved != token or (
        token.isdigit() and _mod_pk_exists(db, token)
    ):
        if resolved != token:
            warnings.warn(
                f"resolve_test_mod_pk: handle {token!r} resolved via "
                f"workspace/external/internal_id to mods.mod_id={resolved}. "
                f"Prefer create_steam_test_mod(...).mod_id.",
                stacklevel=2,
            )
        if _mod_pk_exists(db, resolved):
            return resolved
    if token.isdigit() and _mod_pk_exists(db, token):
        return token
    raise AssertionError(
        f"resolve_test_mod_pk: no Mod for handle={token!r}. "
        f"Use create_steam_test_mod(...).mod_id (PK), not Workshop ID as mod_id."
    )


def soft_resolve_test_mod_pk(db: Any, handle: str | int) -> str:
    """Like :func:`resolve_test_mod_pk` but returns *handle* unchanged on miss.

    Used by pytest autouse wrappers so intentional missing-row checks still work
    when the handle is not a workspace_id of an existing Entity.
    """
    token = str(handle or "").strip()
    if not token:
        return ""
    if _mod_pk_exists(db, token):
        return token
    try:
        found = db.find_mod_by_internal_id(token)
        if found is not None and str(found).strip():
            return str(found).strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        with db._lock:
            rows = list(
                db._conn.execute(
                    """
                    SELECT mod_id FROM mods
                    WHERE workspace_id = ? OR external_id = ?
                    """,
                    (token, token),
                ).fetchall()
            )
    except Exception:  # noqa: BLE001
        return token
    if len(rows) == 1:
        return str(rows[0]["mod_id"])
    return token


def _mod_pk_exists(db: Any, token: str) -> bool:
    if not str(token).isdigit():
        return False
    try:
        with db._lock:
            row = db._conn.execute(
                "SELECT 1 AS ok FROM mods WHERE mod_id = ?",
                (int(token),),
            ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        try:
            return db.get_mod(token) is not None
        except Exception:  # noqa: BLE001
            return False


def bind_managed_path(
    db: Any,
    mod_id: str | int,
    folder: Path,
    *,
    game_name: str | None = None,
    title: str | None = None,
) -> str:
    """Attach filesystem path to an already-created identity (no minting).

    *mod_id* should be ``mods.mod_id`` PK. For transitional test debt, a unique
    ``workspace_id`` / ``external_id`` / Entity ``internal_id`` is resolved to PK
    via :func:`resolve_test_mod_pk` (lookup only).

    Returns the resolved PK.
    """
    pk = resolve_test_mod_pk(db, mod_id)
    db.update_mod_identity_fields(
        pk,
        last_known_path=str(folder),
        folder_present=True,
        game_name=game_name,
        title=title,
    )
    return pk


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
    """Write ``.info/metadata.json`` with filesystem ``internal_id`` proof.

    Value must be Entity ``mods.internal_id`` — not workspace_id, not mods.mod_id.
    """
    from services.mod_identity import set_info_internal_id

    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = set_info_internal_id(
        {
            "published_file_id": str(external_id or ""),
            "external_id": str(external_id or ""),
            "workspace_id": str(workspace_id or external_id or ""),
            "title": title,
            "app_id": int(app_id or 0),
            "game_name": game_name,
            "platform": platform,
        },
        str(internal_id),
    )
    if extra:
        payload.update(dict(extra))
        # Re-assert internal_id after extra merge (extra must not reintroduce legacy).
        payload = set_info_internal_id(payload, str(internal_id))
    path = info / METADATA_FILENAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def prove_managed_folder(
    db: Any,
    folder: Path,
    *,
    handle: str | int,
    title: str = "",
    app_id: int = 0,
    game_name: str = "",
    platform: str = PLATFORM_STEAM,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """Stamp ``.info/internal_id`` from an existing Entity and bind *folder*.

    *handle* may be ``mods.mod_id``, Entity ``internal_id``, or unique
    ``workspace_id`` / ``external_id`` (lookup only). Returns resolved PK.

    Never writes Workshop ID into ``.info/internal_id``.
    """
    pk = resolve_test_mod_pk(db, handle)
    row = {}
    try:
        # Prefer raw row to avoid wrapped helpers during bootstrap.
        with db._lock:
            raw = db._conn.execute(
                """
                SELECT internal_id, workspace_id, external_id, platform, app_id
                FROM mods WHERE mod_id = ?
                """,
                (int(pk),),
            ).fetchone()
        if raw is not None:
            row = {k: raw[k] for k in raw.keys()}
    except Exception:  # noqa: BLE001
        row = db.get_mod_backup_row(pk) or {}
    frozen = str(row.get("internal_id") or "").strip()
    if not frozen:
        raise AssertionError(
            f"prove_managed_folder: Entity mod_id={pk} has empty internal_id"
        )
    ws = str(row.get("workspace_id") or row.get("external_id") or "").strip()
    ext = str(row.get("external_id") or ws).strip()
    plat = str(row.get("platform") or platform or PLATFORM_STEAM).strip()
    aid = int(app_id or row.get("app_id") or 0)
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title=title or folder.name,
        external_id=ext,
        workspace_id=ws or ext,
        app_id=aid,
        game_name=game_name,
        platform=plat or PLATFORM_STEAM,
        extra=extra,
    )
    bind_managed_path(db, pk, folder, game_name=game_name or None, title=title or None)
    return pk


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
    mod_pk = str(created.mod_id)
    frozen = str(created.internal_id or "").strip()
    if not frozen or frozen == mod_pk:
        raise AssertionError(
            "seed_steam_managed_mod requires durable Frozen internal_id "
            f"(got {frozen!r} for mod_id={mod_pk})"
        )
    workspace_id = str(created.workspace_id or external_id)
    folder = library / game_folder / (folder_name or title)
    folder.mkdir(parents=True, exist_ok=True)
    write_info_sidecar(
        folder,
        internal_id=frozen,
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
    bind_managed_path(db, mod_pk, folder, game_name=game_name or None, title=title)
    return SeededMod(
        folder=folder,
        mod_id=mod_pk,
        entity_internal_id=frozen,
        workspace_id=workspace_id,
        external_id=str(external_id),
        identity=created,
    )
