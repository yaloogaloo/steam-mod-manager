"""Backup storage identity: Frozen UUID key; SQLite PK is SQL / read fallback only.

Write key is always Frozen ``mods.internal_id``. Digit ``mods.mod_id`` is never a
new directory name. Reads prefer the UUID directory, then the legacy PK directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from services.deploy_identity import frozen_internal_id_for_pk, is_frozen_internal_uuid

INVALID_FROZEN_INTERNAL_ID = "Invalid frozen internal_id"

BACKUP_DIR_NAME = "mod_backup"
BACKUP_METADATA_NAME = "metadata.json"
DEPLOY_BACKUP_DIR_NAME = "deploy_backup"


class BackupIdentityError(ValueError):
    """Backup storage key is not a Frozen Internal UUID."""

    def __init__(self, message: str = INVALID_FROZEN_INTERNAL_ID) -> None:
        super().__init__(message)


def is_frozen_backup_uuid(token: str | int | None) -> bool:
    """True when *token* is a hyphenated Frozen UUID (not a digit PK)."""
    return is_frozen_internal_uuid(token)


def resolve_backup_storage_key(
    internal_id: str | int | None = None,
    *,
    mod_pk: int | str | None = None,
) -> str:
    """Return the UUID storage key. Digit PK is never a legal key.

    *mod_pk* is accepted for call-site clarity and ignored for the key.
    """
    del mod_pk
    token = str(internal_id or "").strip()
    if is_frozen_backup_uuid(token):
        return token
    raise BackupIdentityError(INVALID_FROZEN_INTERNAL_ID)


def resolve_backup_mod_pk(
    *,
    internal_id: str | int | None = None,
    mod_pk: int | str | None = None,
    db: Any | None = None,
) -> str:
    """SQLite PK for SQL / legacy PK-directory fallback. Empty when unknown."""
    pk = str(mod_pk or "").strip()
    if pk.isdigit():
        return pk
    token = str(internal_id or "").strip()
    if token.isdigit():
        return token
    if not is_frozen_backup_uuid(token):
        return ""
    try:
        database = db
        if database is None:
            from core.db_manager import get_db

            database = get_db()
        found = database.find_mod_by_internal_id(token)
    except Exception:  # noqa: BLE001
        return ""
    text = str(found or "").strip()
    return text if text.isdigit() else ""


def frozen_uuid_for_mod_pk(mod_pk: int | str | None, *, db: Any | None = None) -> str:
    """DAL: PK → Frozen UUID. Empty when missing or collapsed/synthetic."""
    pk = str(mod_pk or "").strip()
    if not pk.isdigit():
        return ""
    try:
        database = db
        if database is None:
            from core.db_manager import get_db

            database = get_db()
        return frozen_internal_id_for_pk(pk, db=database)
    except Exception:  # noqa: BLE001
        return ""


def prove_frozen_backup_key(
    hint: str | int | None = None,
    *,
    managed_path: str | Path | None = None,
    info: Any | None = None,
    db: Any | None = None,
) -> str:
    """Prove a Frozen UUID write key. Empty when the entity has no Frozen UUID.

    Never returns a digit PK. Collapsed / synthetic internal_id → ``""``.
    """
    token = str(hint or "").strip()
    if is_frozen_backup_uuid(token):
        return token
    if token.isdigit():
        found = frozen_uuid_for_mod_pk(token, db=db)
        if found:
            return found
    sidecar = ""
    payload = dict(info or {}) if info is not None else {}
    root = Path(managed_path) if managed_path else None
    if not payload and root is not None:
        try:
            from services.file_ops import read_info_metadata_dict

            payload = read_info_metadata_dict(root) or {}
        except Exception:  # noqa: BLE001
            payload = {}
    if payload:
        try:
            from services.mod_identity import read_internal_id

            sidecar = str(read_internal_id(payload) or "").strip()
        except Exception:  # noqa: BLE001
            sidecar = ""
    if is_frozen_backup_uuid(sidecar):
        return sidecar
    return ""


def _mod_backup_base(base: Path | None = None) -> Path:
    if base is not None:
        return base
    from core.paths import data_dir

    return data_dir() / BACKUP_DIR_NAME


def backup_write_root(
    internal_id: str | int | None,
    *,
    mod_pk: int | str | None = None,
    base: Path | None = None,
) -> Path:
    """``data/mod_backup/<UUID>/`` — raises if *internal_id* is not Frozen UUID."""
    key = resolve_backup_storage_key(internal_id=internal_id, mod_pk=mod_pk)
    return _mod_backup_base(base) / key


def backup_read_root(
    internal_id: str | int | None = None,
    *,
    mod_pk: int | str | None = None,
    db: Any | None = None,
    base: Path | None = None,
) -> Path | None:
    """UUID directory first (if it has a payload or exists), else legacy PK dir."""
    uuid_key = ""
    token = str(internal_id or "").strip()
    pk_hint = str(mod_pk or "").strip()
    if is_frozen_backup_uuid(token):
        uuid_key = token
    elif token.isdigit():
        if not pk_hint:
            pk_hint = token
        uuid_key = frozen_uuid_for_mod_pk(token, db=db)

    pk = resolve_backup_mod_pk(
        internal_id=uuid_key or token, mod_pk=pk_hint or None, db=db
    )
    store = _mod_backup_base(base)
    uuid_dir = (store / uuid_key) if uuid_key else None
    pk_dir = (store / pk) if pk else None

    if uuid_dir is not None and _backup_payload_present(uuid_dir):
        return uuid_dir
    if pk_dir is not None and _backup_payload_present(pk_dir):
        return pk_dir
    if uuid_dir is not None and uuid_dir.exists():
        return uuid_dir
    if pk_dir is not None and pk_dir.exists():
        return pk_dir
    return None


def write_backup_root_for(
    hint: str | int | None,
    *,
    managed_path: str | Path | None = None,
    info: Any | None = None,
    db: Any | None = None,
    base: Path | None = None,
) -> Path:
    """Resolve Frozen UUID from a caller hint (UUID or PK) then return write root."""
    key = prove_frozen_backup_key(
        hint, managed_path=managed_path, info=info, db=db
    )
    if not key:
        raise BackupIdentityError(INVALID_FROZEN_INTERNAL_ID)
    return backup_write_root(key, base=base)


def _backup_payload_present(path: Path) -> bool:
    try:
        return (path / BACKUP_METADATA_NAME).is_file()
    except OSError:
        return False


def _deploy_backup_base(base: Path | None = None) -> Path:
    if base is not None:
        return base
    from core.paths import data_dir

    return data_dir() / DEPLOY_BACKUP_DIR_NAME


def deploy_backup_write_root(
    internal_id: str | int | None,
    *,
    mod_pk: int | str | None = None,
    base: Path | None = None,
) -> Path:
    """``data/deploy_backup/<UUID>/`` — raises if *internal_id* is not Frozen UUID."""
    key = resolve_backup_storage_key(internal_id=internal_id, mod_pk=mod_pk)
    return _deploy_backup_base(base) / key


def deploy_backup_read_root(
    internal_id: str | int | None = None,
    *,
    mod_pk: int | str | None = None,
    db: Any | None = None,
    base: Path | None = None,
) -> Path | None:
    """UUID directory first if it exists, else legacy PK directory."""
    uuid_key = ""
    token = str(internal_id or "").strip()
    pk_hint = str(mod_pk or "").strip()
    if is_frozen_backup_uuid(token):
        uuid_key = token
    elif token.isdigit():
        if not pk_hint:
            pk_hint = token
        uuid_key = frozen_uuid_for_mod_pk(token, db=db)

    pk = resolve_backup_mod_pk(
        internal_id=uuid_key or token, mod_pk=pk_hint or None, db=db
    )
    store = _deploy_backup_base(base)
    uuid_dir = (store / uuid_key) if uuid_key else None
    pk_dir = (store / pk) if pk else None

    if uuid_dir is not None and uuid_dir.exists():
        return uuid_dir
    if pk_dir is not None and pk_dir.exists():
        return pk_dir
    return None
