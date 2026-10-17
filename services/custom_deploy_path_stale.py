"""Custom deploy path stale detection and safe repair candidates.

Does not change Mod identity. Does not look up by workspace_id.
Never auto-writes DB — callers (tools / APPROVE apply) own persistence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Machine codes for audit / repair plans
STALE_MISSING = "CUSTOM_DEPLOY_PATH_STALE_MISSING"
STALE_DRIVE_MIGRATE = "CUSTOM_DEPLOY_PATH_STALE_DRIVE_MIGRATE"
OK_LIVE = "CUSTOM_DEPLOY_PATH_OK"
EMPTY = "CUSTOM_DEPLOY_PATH_EMPTY"

ACTION_CLEAR_TO_INHERIT = "CLEAR_TO_INHERIT"
ACTION_REBIND_UNDER_INSTALL = "REBIND_UNDER_INSTALL"
ACTION_MANUAL_REVIEW = "MANUAL_REVIEW"

DECISION_APPROVE = "APPROVE"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _norm_key(path: Path) -> str:
    try:
        return str(path.expanduser()).replace("\\", "/").casefold()
    except OSError:
        return str(path).replace("\\", "/").casefold()


def steam_common_game_segment(path: Path) -> tuple[str, str] | None:
    """
    Return ``(prefix_before_game, game_folder_name)`` when path contains
    ``.../steamapps/common/<Game>/...``.
    """
    try:
        parts = list(Path(path).expanduser().parts)
    except OSError:
        return None
    lowered = [p.casefold() for p in parts]
    for i in range(len(lowered) - 2):
        if lowered[i] == "steamapps" and lowered[i + 1] == "common":
            game = parts[i + 2]
            prefix = Path(*parts[: i + 2]) if i + 2 > 0 else Path(parts[0])
            # On Windows, Path(*parts[:n]) may drop drive correctly via parts[0]
            try:
                prefix = Path(parts[0]).joinpath(*parts[1 : i + 2])
            except Exception:
                prefix = Path(*parts[: i + 2])
            return str(prefix), str(game)
    return None


def remapped_under_install(
    custom_path: str | Path,
    install_path: str | Path,
) -> Path | None:
    """
    If *custom_path* and *install_path* share the same Steam game folder name,
    rewrite custom onto the current install root (same relative suffix).

    Example::

        custom  = D:/SteamLibrary/steamapps/common/Anno 1800/Bin/Win64
        install = F:/SteamLibrary/steamapps/common/Anno 1800
        → F:/SteamLibrary/steamapps/common/Anno 1800/Bin/Win64
    """
    custom = Path(str(custom_path or "").strip())
    install = Path(str(install_path or "").strip())
    if not str(custom) or not str(install):
        return None
    c_seg = steam_common_game_segment(custom)
    i_seg = steam_common_game_segment(install)
    if not c_seg or not i_seg:
        return None
    _c_prefix, c_game = c_seg
    _i_prefix, i_game = i_seg
    if c_game.casefold() != i_game.casefold():
        return None
    # Relative parts after .../common/<Game>
    c_parts = list(custom.expanduser().parts)
    c_lower = [p.casefold() for p in c_parts]
    for i in range(len(c_lower) - 2):
        if (
            c_lower[i] == "steamapps"
            and c_lower[i + 1] == "common"
            and c_parts[i + 2].casefold() == c_game.casefold()
        ):
            rel = c_parts[i + 3 :]
            return install.expanduser().joinpath(*rel) if rel else install.expanduser()
    return None


def custom_target_is_usable(custom_deploy_path: str | Path | None) -> bool:
    """Same rule as deploy gate: target or its parent must exist."""
    raw = _text(custom_deploy_path)
    if not raw:
        return True
    root = Path(raw).expanduser()
    parent = root if root.exists() else root.parent
    return parent.exists()


@dataclass(frozen=True, slots=True)
class StaleCustomDeployFinding:
    mod_id: str
    internal_id: str
    app_id: int
    title: str
    workspace_id: str
    custom_deploy_path: str
    game_install_path: str
    game_mod_path: str
    code: str
    recommended_action: str
    remapped_candidate: str = ""
    remapped_usable: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_custom_deploy_path(
    *,
    mod_id: str | int,
    internal_id: str = "",
    app_id: int = 0,
    title: str = "",
    workspace_id: str = "",
    custom_deploy_path: str,
    game_install_path: str = "",
    game_mod_path: str = "",
) -> StaleCustomDeployFinding | None:
    """
    Classify one row. Returns None when path is empty or currently usable.

    Never uses workspace_id for entity lookup — it is display-only metadata
    on the finding payload.
    """
    raw = _text(custom_deploy_path)
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        is_abs = path.is_absolute()
    except OSError:
        is_abs = False
    if not is_abs:
        # Relative custom paths are unusual; treat missing as stale missing.
        if custom_target_is_usable(path):
            return None
        return StaleCustomDeployFinding(
            mod_id=str(mod_id),
            internal_id=_text(internal_id) or str(mod_id),
            app_id=int(app_id or 0),
            title=_text(title),
            workspace_id=_text(workspace_id),
            custom_deploy_path=raw,
            game_install_path=_text(game_install_path),
            game_mod_path=_text(game_mod_path),
            code=STALE_MISSING,
            recommended_action=ACTION_CLEAR_TO_INHERIT,
            notes="relative_or_unusable_custom_path",
        )

    if custom_target_is_usable(path):
        return None

    install = _text(game_install_path)
    remapped = remapped_under_install(path, install) if install else None
    remapped_s = str(remapped) if remapped is not None else ""
    remapped_ok = bool(remapped_s) and custom_target_is_usable(remapped_s)

    if remapped_s and _norm_key(Path(remapped_s)) != _norm_key(path):
        # Prefer CLEAR: do not auto-promote remapped Bin/Win64 unless APPROVE REBIND.
        return StaleCustomDeployFinding(
            mod_id=str(mod_id),
            internal_id=_text(internal_id) or str(mod_id),
            app_id=int(app_id or 0),
            title=_text(title),
            workspace_id=_text(workspace_id),
            custom_deploy_path=raw,
            game_install_path=install,
            game_mod_path=_text(game_mod_path),
            code=STALE_DRIVE_MIGRATE,
            recommended_action=ACTION_CLEAR_TO_INHERIT,
            remapped_candidate=remapped_s,
            remapped_usable=remapped_ok,
            notes=(
                "old_install_prefix_mismatch; prefer CLEAR_TO_INHERIT "
                "(inherit game.mod_path); REBIND only with explicit APPROVE"
            ),
        )

    return StaleCustomDeployFinding(
        mod_id=str(mod_id),
        internal_id=_text(internal_id) or str(mod_id),
        app_id=int(app_id or 0),
        title=_text(title),
        workspace_id=_text(workspace_id),
        custom_deploy_path=raw,
        game_install_path=install,
        game_mod_path=_text(game_mod_path),
        code=STALE_MISSING,
        recommended_action=ACTION_CLEAR_TO_INHERIT,
        notes="absolute_path_missing_no_install_remap",
    )


def audit_stale_custom_deploy_paths(db: Any) -> list[StaleCustomDeployFinding]:
    """Scan DB for non-empty absolute custom_deploy_path that fail lifecycle."""
    findings: list[StaleCustomDeployFinding] = []
    games: dict[int, dict[str, Any]] = {}
    try:
        for g in db.list_games() if hasattr(db, "list_games") else []:
            aid = int(getattr(g, "app_id", 0) or 0)
            if aid:
                games[aid] = {
                    "install_path": _text(getattr(g, "install_path", "")),
                    "mod_path": "",
                }
    except Exception:  # noqa: BLE001
        games = {}

    # Prefer deploy config for install/mod paths
    try:
        rows = db._conn.execute(  # noqa: SLF001 — audit tool / tests
            "SELECT mod_id, internal_id, app_id, title, workspace_id, custom_deploy_path "
            "FROM mods WHERE TRIM(COALESCE(custom_deploy_path, '')) != ''"
        ).fetchall()
    except Exception:  # noqa: BLE001
        return findings

    for row in rows:
        mid = str(row["mod_id"])
        app_id = int(row["app_id"] or 0)
        install = ""
        mod_path = ""
        try:
            cfg = db.get_game_deploy_config(app_id)
            if cfg is not None:
                install = _text(cfg.install_path)
                mod_path = _text(cfg.mod_path)
        except Exception:  # noqa: BLE001
            pass
        if not install and app_id in games:
            install = games[app_id].get("install_path", "")
        finding = classify_custom_deploy_path(
            mod_id=mid,
            internal_id=_text(row["internal_id"]) if "internal_id" in row.keys() else mid,
            app_id=app_id,
            title=_text(row["title"]),
            workspace_id=_text(row["workspace_id"]) if "workspace_id" in row.keys() else "",
            custom_deploy_path=_text(row["custom_deploy_path"]),
            game_install_path=install,
            game_mod_path=mod_path,
        )
        if finding is not None:
            findings.append(finding)
    return findings


def apply_clear_custom_deploy_path(db: Any, mod_id: str | int) -> bool:
    """Set custom_deploy_path to empty so deploy inherits game.mod_path."""
    mid = str(mod_id).strip()
    if not mid.isdigit():
        return False
    try:
        if db.get_mod(mid) is None:
            return False
        db.update_mod_user_metadata(mid, {"custom_deploy_path": ""})
        return True
    except Exception:  # noqa: BLE001
        return False


def apply_rebind_custom_deploy_path(
    db: Any, mod_id: str | int, new_path: str
) -> bool:
    """Set custom_deploy_path to an explicit remapped absolute path."""
    mid = str(mod_id).strip()
    path = _text(new_path)
    if not mid.isdigit() or not path:
        return False
    if not Path(path).expanduser().is_absolute():
        return False
    if not custom_target_is_usable(path):
        return False
    try:
        if db.get_mod(mid) is None:
            return False
        db.update_mod_user_metadata(mid, {"custom_deploy_path": path})
        return True
    except Exception:  # noqa: BLE001
        return False
