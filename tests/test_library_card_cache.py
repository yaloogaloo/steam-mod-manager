"""Library card cache — game switch / refresh must reuse ModCardWidget."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager
from ui.library_view import ModLibraryView
from ui.mod_card import ModCardWidget


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "cache.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(lib: Path, game: str, title: str, mid: str) -> Path:
    folder = lib / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": title,
                "game_name": game,
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_refresh_reuses_cards(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    for i in range(5):
        path = _seed(lib, "GameA", f"Mod{i}", str(81000 + i))
        db.upsert_mod(
            ModMetadata(
                published_file_id=str(81000 + i),
                title=f"Mod{i}",
                managed_path=str(path),
                game_name="GameA",
            )
        )

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()

    assert view._card_create_count == 5
    first_ids = {id(c) for c in view._cards}
    assert len(first_ids) == 5

    view.refresh()
    qapp.processEvents()

    assert view._card_create_count == 0
    assert view._card_reuse_count == 5
    assert {id(c) for c in view._cards} == first_ids
    assert len(view._card_cache) == 5


def test_game_switch_reuses_cached_cards(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    a = _seed(lib, "GameA", "Alpha", "82001")
    b = _seed(lib, "GameB", "Beta", "82002")
    db.upsert_mod(
        ModMetadata(published_file_id="82001", title="Alpha", managed_path=str(a))
    )
    db.upsert_mod(
        ModMetadata(published_file_id="82002", title="Beta", managed_path=str(b))
    )

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()

    # Select GameA via filter context
    view._set_current_game_context("GameA")
    view._render_mod_cards(ModFileManager(lib))
    qapp.processEvents()
    assert len(view._cards) == 1
    alpha = view._cards[0]
    assert isinstance(alpha, ModCardWidget)
    alpha_id = id(alpha)

    view._set_current_game_context("GameB")
    view._render_mod_cards(ModFileManager(lib))
    qapp.processEvents()
    assert len(view._cards) == 1
    assert view._cards[0].managed_path.name == "Beta"

    view._set_current_game_context("GameA")
    view._render_mod_cards(ModFileManager(lib))
    qapp.processEvents()
    assert len(view._cards) == 1
    assert id(view._cards[0]) == alpha_id
    assert view._card_create_count == 0
    assert view._card_reuse_count == 1


def test_filter_does_not_create_cards(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    for i in range(4):
        path = _seed(lib, "GameA", f"Mod{i}", str(83000 + i))
        db.upsert_mod(
            ModMetadata(
                published_file_id=str(83000 + i),
                title=f"Mod{i}",
                managed_path=str(path),
            )
        )

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    qapp.processEvents()
    before = view._card_create_count
    cache_n = len(view._card_cache)

    view.search_box.setText("Mod1")
    view._apply_view_filter()
    qapp.processEvents()

    assert view._card_create_count == before
    assert len(view._card_cache) == cache_n
    visible = [c for c in view._cards if not c.isHidden()]
    assert len(visible) == 1
    assert "Mod1" in visible[0].managed_path.name
