from __future__ import annotations

import json
from pathlib import Path

from config import ConfigError, GameConfig, load_games


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_load_games_ok(tmp_path: Path) -> None:
    workshop = tmp_path / "ws"
    smm = tmp_path / "smm"
    cfg = _write(
        tmp_path / "games.json",
        {
            "games": [
                {
                    "name": "暗黑地牢",
                    "app_id": "262060",
                    "workshop_root": str(workshop),
                    "smm_mod_root": str(smm),
                }
            ]
        },
    )
    games = load_games(cfg)
    assert len(games) == 1
    game = games[0]
    assert isinstance(game, GameConfig)
    assert game.name == "暗黑地牢"
    assert game.app_id == "262060"
    assert game.workshop_root == workshop
    assert game.smm_mod_root == smm


def test_missing_field_raises(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "games.json",
        {
            "games": [
                {
                    "name": "群星",
                    "app_id": "281990",
                    "workshop_root": str(tmp_path / "ws"),
                }
            ]
        },
    )
    try:
        load_games(cfg)
        assert False, "expected ConfigError"
    except ConfigError as exc:
        assert "smm_mod_root" in str(exc)


def test_duplicate_app_id_raises(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "games.json",
        {
            "games": [
                {
                    "name": "A",
                    "app_id": "262060",
                    "workshop_root": str(tmp_path / "ws1"),
                    "smm_mod_root": str(tmp_path / "smm1"),
                },
                {
                    "name": "B",
                    "app_id": "262060",
                    "workshop_root": str(tmp_path / "ws2"),
                    "smm_mod_root": str(tmp_path / "smm2"),
                },
            ]
        },
    )
    try:
        load_games(cfg)
        assert False, "expected ConfigError"
    except ConfigError as exc:
        assert "duplicate app_id" in str(exc)


def test_empty_path_raises(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path / "games.json",
        {
            "games": [
                {
                    "name": "A",
                    "app_id": "1",
                    "workshop_root": "   ",
                    "smm_mod_root": str(tmp_path / "smm"),
                }
            ]
        },
    )
    try:
        load_games(cfg)
        assert False, "expected ConfigError"
    except ConfigError as exc:
        assert "workshop_root" in str(exc)


def test_missing_file_raises(tmp_path: Path) -> None:
    try:
        load_games(tmp_path / "nope.json")
        assert False, "expected ConfigError"
    except ConfigError as exc:
        assert "not found" in str(exc)
