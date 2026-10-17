from __future__ import annotations

from pathlib import Path

import pytest

from config import GameConfig
from mapping import build_plan
from metadata import normalize_workspace_id
from safety import (
    SafetyError,
    assert_roots_exist,
    assert_smm_target_exists,
    assert_target_is_not_smm_source,
    assert_workshop_path_in_root,
    delete_workshop_path,
)


def _game(tmp_path: Path) -> GameConfig:
    workshop = tmp_path / "workshop"
    smm = tmp_path / "smm"
    workshop.mkdir()
    smm.mkdir()
    return GameConfig("Test", "262060", workshop, smm)


def test_workshop_root_missing_aborts(tmp_path: Path) -> None:
    game = GameConfig("Test", "1", tmp_path / "missing_ws", tmp_path / "smm")
    (tmp_path / "smm").mkdir()
    with pytest.raises(SafetyError):
        assert_roots_exist(game.workshop_root, game.smm_mod_root)
    result = build_plan(game)
    assert result.aborted is True
    assert "workshop_root" in result.abort_reason


def test_smm_root_missing_aborts(tmp_path: Path) -> None:
    game = GameConfig("Test", "1", tmp_path / "ws", tmp_path / "missing_smm")
    (tmp_path / "ws").mkdir()
    with pytest.raises(SafetyError):
        assert_roots_exist(game.workshop_root, game.smm_mod_root)
    result = build_plan(game)
    assert result.aborted is True
    assert "smm_mod_root" in result.abort_reason


def test_smm_target_missing(tmp_path: Path) -> None:
    target = tmp_path / "smm" / "gone"
    with pytest.raises(SafetyError):
        assert_smm_target_exists(target)


def test_workshop_path_outside_root(tmp_path: Path) -> None:
    game = _game(tmp_path)
    outsider = tmp_path / "other" / "2853239091"
    outsider.mkdir(parents=True)
    with pytest.raises(SafetyError):
        assert_workshop_path_in_root(outsider, game.workshop_root)
    with pytest.raises(SafetyError):
        assert_workshop_path_in_root(game.workshop_root, game.workshop_root)


def test_workshop_equals_smm_target_refuses_delete(tmp_path: Path) -> None:
    game = _game(tmp_path)
    smm_mod = game.smm_mod_root / "MyMod"
    smm_mod.mkdir()
    (smm_mod / "keep.txt").write_text("safe", encoding="utf-8")
    with pytest.raises(SafetyError):
        assert_target_is_not_smm_source(
            smm_mod,
            smm_mod,
            game.smm_mod_root,
            workshop_root=game.workshop_root,
        )
    with pytest.raises(SafetyError):
        delete_workshop_path(
            smm_mod,
            workshop_root=game.workshop_root,
            smm_target=smm_mod,
            smm_mod_root=game.smm_mod_root,
        )
    assert (smm_mod / "keep.txt").read_text(encoding="utf-8") == "safe"


def test_illegal_workspace_ids_rejected() -> None:
    illegal = [".", "..", "../x", r"..\x", r"F:\abs", r"1\2", "1/2", "", "abc"]
    for value in illegal:
        assert normalize_workspace_id(value) is None


def test_delete_real_workshop_dir_keeps_smm(tmp_path: Path) -> None:
    game = _game(tmp_path)
    smm_mod = game.smm_mod_root / "MyMod"
    smm_mod.mkdir()
    marker = smm_mod / "keep.txt"
    marker.write_text("smm", encoding="utf-8")
    workshop_mod = game.workshop_root / "2853239091"
    workshop_mod.mkdir()
    (workshop_mod / "dup.bin").write_bytes(b"x")
    kind = delete_workshop_path(
        workshop_mod,
        workshop_root=game.workshop_root,
        smm_target=smm_mod,
        smm_mod_root=game.smm_mod_root,
    )
    assert kind == "directory"
    assert not workshop_mod.exists()
    assert marker.read_text(encoding="utf-8") == "smm"
