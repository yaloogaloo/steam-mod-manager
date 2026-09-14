"""Batch library snapshot — one scan, cards do not need per-row SQLite."""

from __future__ import annotations

from pathlib import Path

from core.db_manager import DatabaseManager
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
from services.mod_library_cache import (
    get_library_cache,
    reset_library_cache,
)


def test_load_all_mod_cards_batch(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "lib.db")
    lib = tmp_path / "library"
    reset_library_cache()
    pks: list[str] = []
    for i in range(3):
        folder = lib / "Game" / f"Mod{i}"
        folder.mkdir(parents=True)
        (folder / "a.pak").write_bytes(b"x")
        created = create_steam_test_mod(
            db, external_id=str(70000 + i), title=f"Mod{i}", game_name="Game"
        )
        pk = str(created.mod_id)
        prove_managed_folder(
            db, folder, handle=pk, title=f"Mod{i}", game_name="Game"
        )
        pks.append(pk)

    cache = get_library_cache()
    cards = cache.load_all_mod_cards(lib)
    assert len(cards) == 3
    mid = pks[1]
    one = cache.get_card_data(mid)
    assert one is not None
    assert one.title
    assert one.game_folder == "Game"
    cache.invalidate(mid)
    assert cache.get_card_data(mid) is None
    DatabaseManager.reset_instance()
    reset_library_cache()
