"""User-settable Mod statuses must project through to ModCard badges.

失效 (is_invalid) / 停更 (abandoned tag) / 冲突 (conflict_status)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers.identity import bind_managed_path, create_steam_test_mod

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import TAG_TYPE_ABANDONED, DatabaseManager
from core.models import ModMetadata
from services.mod_library_cache import list_item_to_card_data, mod_list_item_from_row
from services.user_annotation import set_conflict_annotation
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
    manager = DatabaseManager.instance(tmp_path / "status_badges.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(db: DatabaseManager, tmp_path: Path, mid: str, title: str) -> Path:
    folder = tmp_path / "Game" / title
    folder.mkdir(parents=True)
    (folder / "mod.pak").write_bytes(b"x")
    db.update_game_deploy_config(4242, name="Game")
    create_steam_test_mod(db, external_id=mid, title=title, app_id=4242)
    bind_managed_path(db, mid, folder, title=title)

    db.update_mod_identity_fields(
        mid, last_known_path=str(folder.resolve()), folder_present=True
    )
    return folder


def _card_for(db: DatabaseManager, folder: Path, mid: str) -> ModCardWidget:
    rows = db.list_mod_list_items(mod_id=mid)
    assert rows, mid
    data = list_item_to_card_data(mod_list_item_from_row(rows[0]))
    meta = ModMetadata(
        published_file_id=mid,
        title=data.title,
        managed_path=str(folder),
    )
    return ModCardWidget(folder, meta, card_data=data)


def test_projection_carries_abandoned_from_existing_tag(
    db: DatabaseManager, tmp_path: Path
) -> None:
    _seed(db, tmp_path, "801", "AbandonedMod")
    db.add_mod_tag("801", TAG_TYPE_ABANDONED, tag_value="")
    rows = db.list_mod_list_items(mod_id="801")
    assert rows
    item = mod_list_item_from_row(rows[0])
    assert item.abandoned is True
    data = list_item_to_card_data(item)
    assert data.abandoned is True


def test_three_user_statuses_render_badges(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    invalid_folder = _seed(db, tmp_path, "811", "InvalidMod")
    abandoned_folder = _seed(db, tmp_path, "812", "AbandonedMod")
    conflict_folder = _seed(db, tmp_path, "813", "ConflictMod")

    db.update_mod_status("811", invalid=True, invalid_reason="已标记失效")
    db.add_mod_tag("812", TAG_TYPE_ABANDONED, tag_value="")
    set_conflict_annotation("813", note="user", db=db)

    invalid_card = _card_for(db, invalid_folder, "811")
    abandoned_card = _card_for(db, abandoned_folder, "812")
    conflict_card = _card_for(db, conflict_folder, "813")
    qapp.processEvents()

    assert not invalid_card.invalid_badge.isHidden()
    assert invalid_card.invalid_badge.text() == "失效"
    assert invalid_card.conflict_badge.isHidden()
    assert invalid_card.abandoned_badge.isHidden()

    assert not abandoned_card.abandoned_badge.isHidden()
    assert abandoned_card.abandoned_badge.text() == "停更"
    assert abandoned_card.invalid_badge.isHidden()
    assert abandoned_card.conflict_badge.isHidden()

    assert not conflict_card.conflict_badge.isHidden()
    assert conflict_card.conflict_badge.text() == "冲突"
    assert conflict_card.invalid_badge.isHidden()
    assert conflict_card.abandoned_badge.isHidden()
