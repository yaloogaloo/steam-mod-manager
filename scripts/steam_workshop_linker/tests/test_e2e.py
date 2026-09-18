from __future__ import annotations

import os
from pathlib import Path

import pytest

from helpers import make_db, steam_mod
from junction import create_junction, is_junction
from mapping import sync_game

WINDOWS = os.name == "nt"


@pytest.mark.skipif(not WINDOWS, reason="Junctions require Windows mklink /J")
def test_e2e_registered_junctions_unregistered_untouched(tmp_path: Path) -> None:
    workshop = tmp_path / "workshop" / "262060"
    smm = tmp_path / "smm"
    workshop.mkdir(parents=True)
    smm.mkdir()
    from config import GameConfig

    game = GameConfig("暗黑地牢", "262060", workshop, smm)
    mod_a = steam_mod(smm, "ModA", "111", internal_id="iid-111")
    mod_b = steam_mod(smm, "ModB", "222", internal_id="iid-222")
    (mod_a / "keep-a.txt").write_text("A", encoding="utf-8")
    (mod_b / "keep-b.txt").write_text("B", encoding="utf-8")
    for name in ("111", "222", "333"):
        folder = workshop / name
        folder.mkdir()
        (folder / "payload.bin").write_bytes(name.encode("ascii"))
    db = make_db(
        tmp_path / "mod_manager.db",
        [
            {
                "workspace_id": "111",
                "internal_id": "iid-111",
                "last_known_path": str(mod_a),
            },
            {
                "workspace_id": "222",
                "internal_id": "iid-222",
                "last_known_path": str(mod_b),
            },
        ],
    )
    before_333 = (workshop / "333" / "payload.bin").read_bytes()
    result = sync_game(game, dry_run=False, db_path=db)
    assert result.errors == 0
    assert result.unregistered == 1
    assert result.replaced == 2
    assert result.repaired == 0
    assert is_junction(workshop / "111")
    assert is_junction(workshop / "222")
    assert (workshop / "111" / "keep-a.txt").read_text(encoding="utf-8") == "A"
    assert (workshop / "222" / "keep-b.txt").read_text(encoding="utf-8") == "B"
    assert (mod_a / "keep-a.txt").read_text(encoding="utf-8") == "A"
    assert (mod_b / "keep-b.txt").read_text(encoding="utf-8") == "B"
    assert (workshop / "333" / "payload.bin").read_bytes() == before_333
    assert not is_junction(workshop / "333")
    assert (workshop / "333").is_dir()
