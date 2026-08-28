"""Relocate a missing Mod folder to a new path (path update only).

Does **not** move files. Verifies identity against the expected mod_id, then
updates ``last_known_path`` / ``folder_present`` / ``content_status`` and runs
``sync_after_metadata_change(reason="restore")``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.file_ops import read_info_metadata_dict
from services.mod_identity import read_internal_id, resolve_existing_mod_id


@dataclass
class RelocateResult:
    success: bool
    mod_id: str = ""
    path: str = ""
    error: str = ""
    matched_by: str = ""


def _identity_matches_expected(
    *,
    expected_mod_id: str,
    payload: dict[str, Any],
) -> tuple[bool, str]:
    """
    Verify folder metadata identity matches *expected_mod_id*.

    Accepts: published_file_id == expected, or resolve_existing_mod_id → expected,
    or internal_id / workspace_id already bound to expected in SQLite.
    """
    mid = str(expected_mod_id or "").strip()
    if not mid.isdigit():
        return False, "invalid mod_id"

    pub = str(payload.get("published_file_id") or "").strip()
    if pub == mid:
        return True, "published_file_id"

    resolved = resolve_existing_mod_id(payload)
    if resolved == mid:
        return True, "identity_match"

    # Explicit checks against the expected row
    try:
        from core.db_manager import get_db

        db = get_db()
        row = db.get_mod_backup_row(mid) or {}
        exp_internal = str(row.get("internal_id") or "").strip()
        got_internal = read_internal_id(payload)
        if exp_internal and got_internal and exp_internal == got_internal:
            return True, "internal_id"

        with db._lock:
            ws_row = db._conn.execute(
                "SELECT workspace_id FROM mods WHERE mod_id = ?",
                (int(mid),),
            ).fetchone()
        exp_ws = str((ws_row["workspace_id"] if ws_row else "") or "").strip()
        got_ws = str(payload.get("workspace_id") or "").strip()
        if exp_ws and got_ws and exp_ws == got_ws:
            return True, "workspace_id"
    except Exception:  # noqa: BLE001
        pass

    return False, "identity_mismatch"


def relocate_mod_folder(
    mod_id: str | int,
    new_folder: str | Path,
) -> RelocateResult:
    """
    Point *mod_id* at *new_folder* after identity verification.

    Never moves or copies files — only updates SQLite path fields + backup sync.
    """
    mid = str(mod_id or "").strip()
    folder = Path(new_folder)
    if not mid.isdigit():
        return RelocateResult(success=False, error="无效的 Mod ID")
    if not folder.is_dir():
        return RelocateResult(success=False, mod_id=mid, error="所选路径不是有效目录")

    payload = read_info_metadata_dict(folder) or {}
    if not payload:
        return RelocateResult(
            success=False,
            mod_id=mid,
            path=str(folder),
            error="目录中缺少 .info / metadata.json，无法验证身份",
        )

    ok, matched_by = _identity_matches_expected(expected_mod_id=mid, payload=payload)
    if not ok:
        return RelocateResult(
            success=False,
            mod_id=mid,
            path=str(folder),
            error=(
                "目录身份与当前 Mod 不匹配"
                "（published_file_id / workspace_id / internal_id）"
            ),
            matched_by=matched_by,
        )

    resolved_path = str(folder.resolve())
    try:
        from core.db_manager import get_db
        from services.path_lifecycle import commit_path_change

        db = get_db()
        commit = commit_path_change(
            mid,
            old_path=str((db.get_mod_backup_row(mid) or {}).get("last_known_path") or ""),
            new_path=folder,
            renamed=False,
            reason="restore",
            sync_backup=True,
            db=db,
        )
        if not commit.success:
            return RelocateResult(
                success=False,
                mod_id=mid,
                path=resolved_path,
                error=commit.error or "更新路径失败",
                matched_by=matched_by,
            )
        db.update_mod_identity_fields(
            mid,
            internal_id=read_internal_id(payload) or None,
            workspace_id=str(payload.get("workspace_id") or "") or None,
            sticky_source=True,
        )
    except Exception as exc:  # noqa: BLE001
        return RelocateResult(
            success=False,
            mod_id=mid,
            path=resolved_path,
            error=f"更新路径失败: {exc}",
            matched_by=matched_by,
        )

    return RelocateResult(
        success=True,
        mod_id=mid,
        path=resolved_path,
        matched_by=matched_by,
    )
