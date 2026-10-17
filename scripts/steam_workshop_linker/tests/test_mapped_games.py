from __future__ import annotations

import json
from pathlib import Path

from config import GameConfig, load_mapped_games, save_mapped_game
from helpers import make_db, steam_mod
from mapping import sync_game
from sync import main, parse_args


def test_execute_flag_sets_dry_run_false() -> None:
    args = parse_args(["--app-id", "262060", "--execute"])
    assert args.execute is True
    assert args.dry_run is False


def test_default_without_execute_is_dry_run() -> None:
    args = parse_args(["--app-id", "262060"])
    assert args.execute is False
    assert (not args.execute) is True


def test_execute_and_dry_run_are_mutually_exclusive() -> None:
    import pytest

    with pytest.raises(SystemExit):
        parse_args(["--app-id", "262060", "--execute", "--dry-run"])


def _write_games(tmp_path: Path) -> tuple[Path, Path, GameConfig, Path]:
    workshop = tmp_path / "workshop"
    smm = tmp_path / "smm"
    workshop.mkdir()
    smm.mkdir()
    games_path = tmp_path / "games.json"
    games_path.write_text(
        json.dumps(
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
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    mapped_path = tmp_path / "mapped_games.json"
    mapped_path.write_text('{"mapped_games": {}}\n', encoding="utf-8")
    game = GameConfig("暗黑地牢", "262060", workshop, smm)
    mod = steam_mod(smm, "MyMod", "2853239091", internal_id="iid-x")
    db = make_db(
        tmp_path / "mod_manager.db",
        [
            {
                "workspace_id": "2853239091",
                "internal_id": "iid-x",
                "last_known_path": str(mod),
            }
        ],
    )
    return games_path, mapped_path, game, db


def test_dry_run_does_not_write_mapped_games(tmp_path: Path) -> None:
    games_path, mapped_path, _game, db = _write_games(tmp_path)
    before = mapped_path.read_text(encoding="utf-8")
    code = main(
        [
            "--games-json",
            str(games_path),
            "--mapped-json",
            str(mapped_path),
            "--db",
            str(db),
            "--app-id",
            "262060",
            "--dry-run",
        ]
    )
    assert code == 0
    assert mapped_path.read_text(encoding="utf-8") == before
    assert load_mapped_games(mapped_path) == {}


def test_execute_writes_mapped_games_by_app_id(tmp_path: Path) -> None:
    games_path, mapped_path, game, db = _write_games(tmp_path)
    code = main(
        [
            "--games-json",
            str(games_path),
            "--mapped-json",
            str(mapped_path),
            "--db",
            str(db),
            "--app-id",
            "262060",
            "--execute",
        ]
    )
    assert code == 0
    records = load_mapped_games(mapped_path)
    assert "262060" in records
    assert "暗黑地牢" not in records
    assert records["262060"]["name"] == "暗黑地牢"
    assert records["262060"]["smm_mod_root"] == str(game.smm_mod_root)
    assert records["262060"]["workshop_root"] == str(game.workshop_root)
    assert records["262060"]["mapped_at"]


def test_invalid_menu_selection_does_not_modify(tmp_path: Path) -> None:
    from junction import is_junction

    games_path, mapped_path, game, db = _write_games(tmp_path)
    old = game.workshop_root / "2853239091"
    old.mkdir()
    leftover = old / "dup.bin"
    leftover.write_bytes(b"stay")
    before = mapped_path.read_text(encoding="utf-8")
    answers = iter(["0", "99", "q"])
    code = main(
        [
            "--games-json",
            str(games_path),
            "--mapped-json",
            str(mapped_path),
            "--db",
            str(db),
        ],
        input_fn=lambda _prompt: next(answers),
    )
    assert code == 1
    assert leftover.read_bytes() == b"stay"
    assert mapped_path.read_text(encoding="utf-8") == before
    assert not is_junction(old)


def test_mapped_history_does_not_skip_filesystem_check(tmp_path: Path) -> None:
    _games_path, mapped_path, game, db = _write_games(tmp_path)
    save_mapped_game(mapped_path, game)
    assert "262060" in load_mapped_games(mapped_path)
    old = game.workshop_root / "2853239091"
    old.mkdir()
    (old / "dup.bin").write_bytes(b"copy")
    plan = sync_game(game, dry_run=True, db_path=db)
    actions = [item.action for item in plan.items if item.workspace_id == "2853239091"]
    assert actions == ["replace"]
    assert any("WOULD DELETE + LINK" in line for line in plan.logs)
    assert any("REGISTERED + REPLACE" in line for line in plan.logs)
    assert not any("REGISTERED + REPAIR" in line for line in plan.logs)
    assert plan.replaced == 1
    assert plan.repaired == 0
