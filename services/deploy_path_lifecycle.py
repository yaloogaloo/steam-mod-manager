"""Deploy path lifecycle — validate game / custom / source roots before apply.

Ownership of *which* Mod is unchanged (``internal_id`` only). This module only
answers: is the chosen deploy *path* still a live path on disk?

Does **not**:
- look up entities by ``workspace_id`` / path / folder name
- invent identity fields
- auto-migrate stale drive letters
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


# --- Machine-stable error codes (deploy result ``error_code`` / ``error_kind``) ---

GAME_CONFIG_PATH_MISSING = "GAME_CONFIG_PATH_MISSING"
CUSTOM_DEPLOY_PATH_MISSING = "CUSTOM_DEPLOY_PATH_MISSING"
SOURCE_MOD_PATH_MISSING = "SOURCE_MOD_PATH_MISSING"

PATH_LIFECYCLE_CODES = frozenset(
    {
        GAME_CONFIG_PATH_MISSING,
        CUSTOM_DEPLOY_PATH_MISSING,
        SOURCE_MOD_PATH_MISSING,
    }
)

# --- Stable user-facing prefixes (do not collapse into vague game-settings) ---

DEPLOY_ERR_CUSTOM_PATH_MISSING_PREFIX = "Mod自定义部署路径不存在"
DEPLOY_ERR_GAME_INSTALL_MISSING_PREFIX = "游戏安装目录不存在"
DEPLOY_ERR_GAME_MOD_PATH_MISSING_PREFIX = "游戏Mod部署目录不存在"
DEPLOY_ERR_IDENTITY_PREFIX = "Identity解析失败"
DEPLOY_ERR_SOURCE_MISSING_PREFIX = "源 Mod 目录不存在"

# Forbidden vague copy — never emit this for path lifecycle failures.
FORBIDDEN_VAGUE_MOD_PATH_COPY = "Mod 安装目录不存在，请检查游戏设置"


@dataclass(frozen=True, slots=True)
class PathLifecycleFailure:
    """Structured deploy-resolve path failure."""

    code: str
    field: str
    path: str
    app_id: int = 0
    internal_id: str = ""
    message: str = ""

    def as_error_dict(self, *, mod_id: str = "") -> dict[str, Any]:
        mid = str(mod_id or self.internal_id or "").strip()
        out: dict[str, Any] = {
            "success": False,
            "error": self.message,
            "error_code": self.code,
            "error_kind": self.code,
            "path_field": self.field,
            "configured_path": self.path,
            "app_id": int(self.app_id or 0),
        }
        if mid:
            out["mod_id"] = mid
        return out


def _fmt_path(path: str | Path) -> str:
    try:
        return str(Path(path).expanduser())
    except OSError:
        return str(path)


def format_game_config_path_missing(
    *,
    field: str,
    path: str | Path,
    app_id: int = 0,
) -> str:
    """Human message for stale/missing game.install_path or game.mod_path."""
    path_s = _fmt_path(path)
    field_s = str(field or "").strip() or "mod_path"
    if field_s == "install_path":
        prefix = DEPLOY_ERR_GAME_INSTALL_MISSING_PREFIX
    else:
        prefix = DEPLOY_ERR_GAME_MOD_PATH_MISSING_PREFIX
        field_s = "mod_path"
    return (
        f"{prefix}: {path_s} "
        f"(field={field_s}, app_id={int(app_id or 0)}, code={GAME_CONFIG_PATH_MISSING})"
    )


def format_custom_deploy_path_missing(
    *,
    path: str | Path,
    app_id: int = 0,
) -> str:
    path_s = _fmt_path(path)
    return (
        f"{DEPLOY_ERR_CUSTOM_PATH_MISSING_PREFIX}: {path_s} "
        f"(field=custom_deploy_path, app_id={int(app_id or 0)}, "
        f"code={CUSTOM_DEPLOY_PATH_MISSING})"
    )


def format_source_mod_path_missing(
    *,
    library_root: Path | str,
    internal_id: str,
    path: str | Path | None = None,
) -> str:
    mid = str(internal_id or "").strip() or "?"
    path_s = _fmt_path(path) if path is not None and str(path).strip() else ""
    base = (
        f"{DEPLOY_ERR_SOURCE_MISSING_PREFIX}"
        f"（库：{library_root}，internal_id={mid}，code={SOURCE_MOD_PATH_MISSING}）"
    )
    if path_s:
        return f"{base} configured_path={path_s}"
    return base


def custom_deploy_path_missing_error(path: str | Path, *, app_id: int = 0) -> str:
    return format_custom_deploy_path_missing(path=path, app_id=app_id)


def game_install_missing_error(path: str | Path, *, app_id: int = 0) -> str:
    return format_game_config_path_missing(
        field="install_path", path=path, app_id=app_id
    )


def game_mod_path_missing_error(path: str | Path, *, app_id: int = 0) -> str:
    return format_game_config_path_missing(
        field="mod_path", path=path, app_id=app_id
    )


def identity_resolve_failed_error(token: str) -> str:
    return (
        f"{DEPLOY_ERR_IDENTITY_PREFIX}: 无效的 internal_id={token} "
        f"（禁止用 workspace_id / external_id 查实体）"
    )


def source_missing_error(
    *,
    library_root: Path | str,
    internal_id: str,
    path: str | Path | None = None,
) -> str:
    return format_source_mod_path_missing(
        library_root=library_root,
        internal_id=internal_id,
        path=path,
    )


def game_config_path_failure(
    *,
    field: str,
    path: str | Path,
    app_id: int = 0,
    internal_id: str = "",
) -> PathLifecycleFailure:
    field_s = str(field or "").strip() or "mod_path"
    path_s = _fmt_path(path)
    msg = format_game_config_path_missing(
        field=field_s, path=path_s, app_id=app_id
    )
    return PathLifecycleFailure(
        code=GAME_CONFIG_PATH_MISSING,
        field=field_s if field_s in ("install_path", "mod_path") else "mod_path",
        path=path_s,
        app_id=int(app_id or 0),
        internal_id=str(internal_id or "").strip(),
        message=msg,
    )


def custom_deploy_path_failure(
    *,
    path: str | Path,
    app_id: int = 0,
    internal_id: str = "",
) -> PathLifecycleFailure:
    path_s = _fmt_path(path)
    return PathLifecycleFailure(
        code=CUSTOM_DEPLOY_PATH_MISSING,
        field="custom_deploy_path",
        path=path_s,
        app_id=int(app_id or 0),
        internal_id=str(internal_id or "").strip(),
        message=format_custom_deploy_path_missing(path=path_s, app_id=app_id),
    )


def source_mod_path_failure(
    *,
    library_root: Path | str,
    internal_id: str,
    path: str | Path | None = None,
) -> PathLifecycleFailure:
    mid = str(internal_id or "").strip()
    path_s = _fmt_path(path) if path is not None and str(path).strip() else ""
    return PathLifecycleFailure(
        code=SOURCE_MOD_PATH_MISSING,
        field="source",
        path=path_s,
        app_id=0,
        internal_id=mid,
        message=format_source_mod_path_missing(
            library_root=library_root, internal_id=mid, path=path_s or None
        ),
    )


def validate_custom_deploy_target(
    custom_deploy_path: str | Path | None,
    *,
    app_id: int = 0,
) -> str | None:
    """
    Return an error when a configured custom deploy path cannot be used.

    Lifecycle rule: parent of the target must exist (deploy may create the
    leaf directory). Empty path → None (caller uses game-level roots).
    """
    raw = str(custom_deploy_path or "").strip()
    if not raw:
        return None
    custom_root = Path(raw).expanduser()
    parent = custom_root if custom_root.exists() else custom_root.parent
    if not parent.exists():
        return custom_deploy_path_missing_error(custom_root, app_id=app_id)
    return None


def validate_game_install_dir(
    install_path: str | Path | None,
    *,
    app_id: int = 0,
) -> str | None:
    raw = str(install_path or "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser()
    if not root.is_dir():
        return game_install_missing_error(root, app_id=app_id)
    return None


def validate_game_mod_path(
    mod_path: str | Path | None,
    *,
    app_id: int = 0,
) -> str | None:
    raw = str(mod_path or "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser()
    if not root.exists():
        return game_mod_path_missing_error(root, app_id=app_id)
    return None


def is_path_lifecycle_error(error: str) -> bool:
    text = str(error or "").strip()
    if not text:
        return False
    if any(code in text for code in PATH_LIFECYCLE_CODES):
        return True
    return text.startswith(
        (
            DEPLOY_ERR_CUSTOM_PATH_MISSING_PREFIX,
            DEPLOY_ERR_GAME_INSTALL_MISSING_PREFIX,
            DEPLOY_ERR_GAME_MOD_PATH_MISSING_PREFIX,
            DEPLOY_ERR_IDENTITY_PREFIX,
            DEPLOY_ERR_SOURCE_MISSING_PREFIX,
        )
    )


def resolve_entity_internal_id(token: Any, *, db: Any) -> tuple[str, str | None]:
    """
    Resolve deploy entity by internal_id only.

    Returns ``(mid, error)``. Never looks up by workspace_id / external_id.
    """
    from services.deploy_paths import resolve_deploy_identity

    mid = resolve_deploy_identity(token, db=db)
    if not str(mid).isdigit():
        return "", identity_resolve_failed_error(str(token or "").strip() or "?")
    try:
        row = db.get_mod(mid)
    except Exception:  # noqa: BLE001
        row = None
    if row is None:
        # Digit token that is not a mods.mod_id PK (e.g. workspace_id digits).
        return "", identity_resolve_failed_error(str(token or "").strip() or mid)
    return str(mid), None
