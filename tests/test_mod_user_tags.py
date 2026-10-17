"""User mod tags + conflict relations (SQLite only)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.db_manager import (
    RELATION_TYPE_CONFLICT,
    TAG_TYPE_ABANDONED,
    TAG_TYPE_CONFLICT,
    TAG_TYPE_INVALID,
    DatabaseManager,
)
from core.models import ModMetadata
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
from ui.library_query import (
    FILTER_CONFLICT,
    FILTER_INVALID,
    ModFilterIndex,
    filter_and_sort,
    matches_search,
    matches_status_filter,
)
from ui.library_view import ModLibraryView
from ui.mod_card import ModCardWidget
from ui.mod_detail_panel import ModDetailPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "mod_tags.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _idx(**kwargs) -> ModFilterIndex:
    base = dict(
        mod_id="1",
        display_name="Alpha",
        steam_name="Steam Alpha",
        notes="",
        game_name="Palworld",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=1.0,
        sort_name="Alpha",
        invalid=False,
        conflict=False,
        tag_values="",
    )
    base.update(kwargs)
    return ModFilterIndex(**base)


def test_add_and_remove_invalid_tag(db: DatabaseManager) -> None:
    created = create_steam_test_mod(db, external_id="1001", title="Broken Mod")
    pk = str(created.mod_id)
    tag = db.add_mod_tag(pk, TAG_TYPE_INVALID, tag_value="游戏更新后失效")
    assert tag.tag_type == TAG_TYPE_INVALID
    assert tag.tag_value == "游戏更新后失效"

    tags = db.get_mod_tags(pk)
    assert len(tags) == 1
    assert db.get_mods_by_tag(TAG_TYPE_INVALID) == [pk]

    # Upsert same type updates value
    db.add_mod_tag(pk, TAG_TYPE_INVALID, tag_value="新原因")
    assert db.get_mod_tags(pk)[0].tag_value == "新原因"

    assert db.remove_mod_tag(pk, TAG_TYPE_INVALID) == 1
    assert db.get_mod_tags(pk) == []
    assert db.get_mods_by_tag(TAG_TYPE_INVALID) == []


def test_conflict_relation(db: DatabaseManager) -> None:
    a = str(create_steam_test_mod(db, external_id="2001", title="A").mod_id)
    b = str(create_steam_test_mod(db, external_id="2002", title="B").mod_id)
    c = str(create_steam_test_mod(db, external_id="2003", title="C").mod_id)

    rels = db.set_mod_conflict_targets(a, [b, c], note="overlap")
    assert len(rels) == 2
    assert {r.target_mod_id for r in rels} == {b, c}
    assert any(t.tag_type == TAG_TYPE_CONFLICT for t in db.get_mod_tags(a))

    flags = db.get_mods_tag_flags([a, b])
    assert flags[a].conflict is True
    assert flags[b].conflict is False

    db.set_mod_conflict_targets(a, [])
    assert db.get_mod_relations(a) == []
    assert not any(t.tag_type == TAG_TYPE_CONFLICT for t in db.get_mod_tags(a))


def test_tables_created_on_open(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "schema_tags.db")
    names = {
        str(r[0])
        for r in manager._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "mod_tags" in names
    assert "mod_relations" in names
    manager.close()
    DatabaseManager.reset_instance()


def test_filter_invalid_and_conflict() -> None:
    from services.user_annotation import CONFLICT_STATUS_CONFLICT

    inv = _idx(mod_id="1", invalid=True, tag_values="旧版本失效")
    conf = _idx(mod_id="2", conflict=True, conflict_status=CONFLICT_STATUS_CONFLICT)
    plain = _idx(mod_id="3")

    assert matches_status_filter(inv, FILTER_INVALID)
    assert not matches_status_filter(conf, FILTER_INVALID)
    assert matches_status_filter(conf, FILTER_CONFLICT)
    assert not matches_status_filter(plain, FILTER_CONFLICT)

    assert matches_search(inv, "旧版本")
    assert not matches_search(plain, "旧版本")

    result = filter_and_sort(
        [(inv, "I"), (conf, "C"), (plain, "P")],
        filter_key=FILTER_INVALID,
    )
    assert result == ["I"]


def test_detail_panel_saves_tags(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    mod = tmp_path / "Game" / "Tagged"
    mod.mkdir(parents=True)
    created = create_steam_test_mod(db, external_id="3001", title="Tagged")
    other = create_steam_test_mod(db, external_id="3002", title="Other")
    pk = str(created.mod_id)
    other_pk = str(other.mod_id)
    prove_managed_folder(db, mod, handle=pk, title="Tagged")
    panel = ModDetailPanel()
    panel.set_peer_mods([(other_pk, "Other")])
    panel.show_mod(mod, mod_id=pk)

    panel.tag_invalid_check.setChecked(True)
    panel.tag_invalid_reason.setText("crash on load")
    panel.tag_conflict_check.setChecked(True)
    item = panel.tag_conflict_list.item(0)
    assert item is not None
    item.setCheckState(Qt.CheckState.Checked)
    panel._save_user_tags()

    tags = {t.tag_type: t.tag_value for t in db.get_mod_tags(pk)}
    assert TAG_TYPE_INVALID in tags
    assert tags[TAG_TYPE_INVALID] == "crash on load"
    assert TAG_TYPE_CONFLICT in tags
    rels = db.get_mod_relations(pk)
    assert len(rels) == 1
    assert rels[0].target_mod_id == other_pk
    assert rels[0].relation_type == RELATION_TYPE_CONFLICT

    # Remove tags
    panel.tag_invalid_check.setChecked(False)
    panel.tag_conflict_check.setChecked(False)
    panel._save_user_tags()
    assert db.get_mod_tags(pk) == []
    assert db.get_mod_relations(pk) == []


def test_mod_card_badge_overlay(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    from services.mod_library_cache import list_item_to_card_data, mod_list_item_from_row
    from services.user_annotation import set_conflict_annotation

    mod = tmp_path / "Game" / "BadgeMod"
    mod.mkdir(parents=True)
    (mod / "payload.bin").write_bytes(b"ok")
    created = create_steam_test_mod(db, external_id="4001", title="Badge")
    pk = str(created.mod_id)
    prove_managed_folder(db, mod, handle=pk, title="Badge")
    db.update_mod_status(pk, invalid=True, invalid_reason="gone")
    set_conflict_annotation(pk, note="user", db=db)
    db.add_mod_tag(pk, TAG_TYPE_ABANDONED, tag_value="")

    rows = db.list_mod_list_items(mod_id=pk)
    data = list_item_to_card_data(mod_list_item_from_row(rows[0]))
    card = ModCardWidget(
        mod,
        ModMetadata(published_file_id="4001", title="Badge", managed_path=str(mod)),
        card_data=data,
    )
    qapp.processEvents()
    # Cover overlay is Category-only — user flags live in footer chips.
    assert card.tag_badge.isHidden() or "Conflict" not in (card.tag_badge.text() or "")
    assert not card.invalid_badge.isHidden()
    assert card.invalid_badge.text() == "失效"
    assert not card.conflict_badge.isHidden()
    assert card.conflict_badge.text() == "冲突"
    assert not card.abandoned_badge.isHidden()
    assert card.abandoned_badge.text() == "停更"
    # Layout height unchanged vs untagged card
    plain = tmp_path / "Game" / "Plain"
    plain.mkdir(parents=True)
    card_b = ModCardWidget(plain)
    assert card.height() == card_b.height()


def test_deploy_hint_does_not_block(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)

    library = tmp_path / "mod"
    mod = library / "Game" / "Warn"
    mod.mkdir(parents=True)
    created = create_steam_test_mod(db, external_id="5001", title="Warn")
    pk = str(created.mod_id)
    prove_managed_folder(db, mod, handle=pk, title="Warn")
    db.add_mod_tag(pk, TAG_TYPE_INVALID, "broken")
    from services.user_annotation import set_conflict_annotation

    set_conflict_annotation(pk, note="overlap", db=db)
    db.add_mod_tag(pk, TAG_TYPE_CONFLICT, "")

    view = ModLibraryView()
    view.set_target_root(str(library))
    view._card_entries = []
    started: list[str] = []

    class FakeWorker:
        def __init__(self, *a, **k):
            self.deploy_started = MagicMock()
            self.deploy_finished = MagicMock()
            self.deploy_failed = MagicMock()
            self.finished = MagicMock()

        def isRunning(self):
            return False

        def start(self):
            started.append("yes")

    monkeypatch.setattr("ui.library_view.DeployWorker", FakeWorker)
    view._on_deploy_action(pk, "deploy")
    assert started == ["yes"]
    hint = view.detail_panel.view_tag_deploy_hint.text()
    assert "失效" in hint
    assert "冲突" in hint


def test_library_filter_index_includes_tags(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.game_info import GameInfo
    from services.user_annotation import set_conflict_annotation

    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    db.upsert_game(GameInfo(app_id=1, name="Game", folder_name="Game"))
    library = tmp_path / "mod"
    for workshop, title, tag in (
        ("6001", "Bad", TAG_TYPE_INVALID),
        ("6002", "Clash", TAG_TYPE_CONFLICT),
        ("6003", "Ok", None),
    ):
        mod = library / "Game" / title
        mod.mkdir(parents=True)
        created = create_steam_test_mod(
            db, external_id=workshop, title=title, app_id=1, game_name="Game"
        )
        pk = str(created.mod_id)
        prove_managed_folder(
            db, mod, handle=pk, title=title, app_id=1, game_name="Game"
        )
        if tag == TAG_TYPE_INVALID:
            db.add_mod_tag(pk, tag, "reason-xyz")
            db.update_mod_status(pk, invalid=True, invalid_reason="reason-xyz")
        elif tag:
            db.add_mod_tag(pk, tag, "")
            set_conflict_annotation(pk, note="clash", db=db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    assert view._card_entries, "library refresh produced no cards"
    assert any(idx.invalid for idx, _ in view._card_entries)
    assert any(
        (idx.conflict or idx.conflict_status == "conflict")
        for idx, _ in view._card_entries
    )
    # Search uses index fields (title / notes / tag_values when projected).
    bad = next(idx for idx, _ in view._card_entries if idx.invalid)
    assert matches_search(bad, "Bad")
    assert bad.invalid is True
