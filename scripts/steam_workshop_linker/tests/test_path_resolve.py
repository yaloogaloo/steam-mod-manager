from __future__ import annotations

from pathlib import Path

from database import ModRow
from helpers import game_pair, steam_mod
from resolve import resolve_smm_mod_path


def _row(**kwargs) -> ModRow:
    payload = {
        "mod_id": 1,
        "internal_id": "iid-1",
        "workspace_id": "111",
        "app_id": 262060,
        "platform": "steam",
        "last_known_path": "",
        "folder_present": 1,
    }
    payload.update(kwargs)
    return ModRow(**payload)


def test_uses_valid_db_last_known_path(tmp_path: Path) -> None:
    game = game_pair(tmp_path)
    target = steam_mod(game.smm_mod_root, "ModA", "111", internal_id="iid-1")
    resolved = resolve_smm_mod_path(
        _row(last_known_path=str(target), internal_id="iid-1"),
        workspace_id="111",
        workshop_path=game.workshop_root / "111",
        workshop_root=game.workshop_root,
        smm_mod_root=game.smm_mod_root,
    )
    assert resolved.path == target
    assert resolved.reason == ""


def test_missing_db_path_falls_back_to_info_scan(tmp_path: Path) -> None:
    game = game_pair(tmp_path)
    target = steam_mod(game.smm_mod_root, "ModA", "111", internal_id="iid-1")
    resolved = resolve_smm_mod_path(
        _row(last_known_path=str(tmp_path / "gone"), internal_id="iid-1"),
        workspace_id="111",
        workshop_path=game.workshop_root / "111",
        workshop_root=game.workshop_root,
        smm_mod_root=game.smm_mod_root,
    )
    assert resolved.path == target


def test_db_path_workspace_mismatch_not_used(tmp_path: Path) -> None:
    game = game_pair(tmp_path)
    wrong = steam_mod(game.smm_mod_root, "Wrong", "999", internal_id="iid-1")
    resolved = resolve_smm_mod_path(
        _row(last_known_path=str(wrong), internal_id="iid-1", workspace_id="111"),
        workspace_id="111",
        workshop_path=game.workshop_root / "111",
        workshop_root=game.workshop_root,
        smm_mod_root=game.smm_mod_root,
    )
    assert resolved.path is None
    assert resolved.reason == "PATH_IDENTITY_MISMATCH"


def test_fallback_not_found(tmp_path: Path) -> None:
    game = game_pair(tmp_path)
    resolved = resolve_smm_mod_path(
        _row(),
        workspace_id="111",
        workshop_path=game.workshop_root / "111",
        workshop_root=game.workshop_root,
        smm_mod_root=game.smm_mod_root,
    )
    assert resolved.path is None
    assert resolved.reason == "SMM_PATH_NOT_FOUND"


def test_fallback_ambiguous_filesystem(tmp_path: Path) -> None:
    game = game_pair(tmp_path)
    steam_mod(game.smm_mod_root, "ModA", "111", internal_id="iid-1")
    steam_mod(game.smm_mod_root, "ModB", "111", internal_id="iid-1")
    resolved = resolve_smm_mod_path(
        _row(last_known_path=""),
        workspace_id="111",
        workshop_path=game.workshop_root / "111",
        workshop_root=game.workshop_root,
        smm_mod_root=game.smm_mod_root,
    )
    assert resolved.path is None
    assert resolved.reason == "AMBIGUOUS_FILESYSTEM_MAPPING"


def test_internal_id_mismatch(tmp_path: Path) -> None:
    game = game_pair(tmp_path)
    target = steam_mod(game.smm_mod_root, "ModA", "111", internal_id="disk-id")
    resolved = resolve_smm_mod_path(
        _row(last_known_path=str(target), internal_id="db-id"),
        workspace_id="111",
        workshop_path=game.workshop_root / "111",
        workshop_root=game.workshop_root,
        smm_mod_root=game.smm_mod_root,
    )
    assert resolved.path is None
    assert resolved.reason == "IDENTITY_MISMATCH"
