"""Batch library snapshot — one scan, cards do not need per-row SQLite."""

from __future__ import annotations

import json
from pathlib import Path

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.mod_library_cache import (
    get_library_cache,
    reset_library_cache,
)


def test_load_all_mod_cards_batch(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "lib.db")
    lib = tmp_path / "library"
    reset_library_cache()
    for i in range(3):
        folder = lib / "Game" / f"Mod{i}"
        info = folder / INFO_DIR_NAME
        info.mkdir(parents=True)
        (info / METADATA_FILENAME).write_text(
            json.dumps(
                {
                    "published_file_id": str(70000 + i),
                    "title": f"Mod{i}",
                    "game_name": "Game",
                }
            ),
            encoding="utf-8",
        )
        (folder / "a.pak").write_bytes(b"x")
        db.upsert_mod(
            ModMetadata(
                published_file_id=str(70000 + i),
                title=f"Mod{i}",
                managed_path=str(folder),
                game_name="Game",
            )
        )

    cache = get_library_cache()
    cards = cache.load_all_mod_cards(lib)
    assert len(cards) == 3
    one = cache.get_card_data("70001")
    assert one is not None
    assert one.title
    assert one.game_folder == "Game"
    cache.invalidate("70001")
    assert cache.get_card_data("70001") is None
    DatabaseManager.reset_instance()
    reset_library_cache()
