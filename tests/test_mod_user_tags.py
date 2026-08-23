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
    TAG_TYPE_CONFLICT,
    TAG_TYPE_INVALID,
    DatabaseManager,
)
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
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
    manager = DatabaseManager(tmp_path / "mod_tags.db")
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
    db.upsert_mod(ModMetadata(published_file_id="1001", title="Broken Mod"))
    tag = db.add_mod_tag("1001", TAG_TYPE_INVALID, tag_value="游戏更新后失效")
    assert tag.tag_type == TAG_TYPE_INVALID
    assert tag.tag_value == "游戏更新后失效"

    tags = db.get_mod_tags("1001")
    assert len(tags) == 1
    assert db.get_mods_by_tag(TAG_TYPE_INVALID) == ["1001"]

    # Upsert same type updates value
    db.add_mod_tag("1001", TAG_TYPE_INVALID, tag_value="新原因")
    assert db.get_mod_tags("1001")[0].tag_value == "新原因"

    assert db.remove_mod_tag("1001", TAG_TYPE_INVALID) == 1
    assert db.get_mod_tags("1001") == []
    assert db.get_mods_by_tag(TAG_TYPE_INVALID) == []


def test_conflict_relation(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="2001", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="2002", title="B"))
    db.upsert_mod(ModMetadata(published_file_id="2003", title="C"))

    rels = db.set_mod_conflict_targets("2001", ["2002", "2003"], note="overlap")
    assert len(rels) == 2
    assert {r.target_mod_id for r in rels} == {"2002", "2003"}
    assert any(t.tag_type == TAG_TYPE_CONFLICT for t in db.get_mod_tags("2001"))

    flags = db.get_mods_tag_flags(["2001", "2002"])
    assert flags["2001"].conflict is True
    assert flags["2002"].conflict is False

    db.set_mod_conflict_targets("2001", [])
    assert db.get_mod_relations("2001") == []
    assert not any(t.tag_type == TAG_TYPE_CONFLICT for t in db.get_mod_tags("2001"))


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
    inv = _idx(mod_id="1", invalid=True, tag_values="旧版本失效")
    conf = _idx(mod_id="2", conflict=True)
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
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        '{"published_file_id":"3001","title":"Tagged","app_id":1}\n',
        encoding="utf-8",
    )
    db.upsert_mod(ModMetadata(published_file_id="3001", title="Tagged"))
    db.upsert_mod(ModMetadata(published_file_id="3002", title="Other"))

    panel = ModDetailPanel()
    panel.set_peer_mods([("3002", "Other")])
    panel.show_mod(mod)

    panel.tag_invalid_check.setChecked(True)
    panel.tag_invalid_reason.setText("crash on load")
    panel.tag_conflict_check.setChecked(True)
    item = panel.tag_conflict_list.item(0)
    assert item is not None
    item.setCheckState(Qt.CheckState.Checked)
    panel._save_user_tags()

    tags = {t.tag_type: t.tag_value for t in db.get_mod_tags("3001")}
    assert TAG_TYPE_INVALID in tags
    assert tags[TAG_TYPE_INVALID] == "crash on load"
    assert TAG_TYPE_CONFLICT in tags
    rels = db.get_mod_relations("3001")
    assert len(rels) == 1
    assert rels[0].target_mod_id == "3002"
    assert rels[0].relation_type == RELATION_TYPE_CONFLICT

    # Remove tags
    panel.tag_invalid_check.setChecked(False)
    panel.tag_conflict_check.setChecked(False)
    panel._save_user_tags()
    assert db.get_mod_tags("3001") == []
    assert db.get_mod_relations("3001") == []


def test_mod_card_badge_overlay(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    mod = tmp_path / "Game" / "BadgeMod"
    (mod / INFO_DIR_NAME).mkdir(parents=True)
    (mod / "payload.bin").write_bytes(b"ok")
    db.upsert_mod(ModMetadata(published_file_id="4001", title="Badge"))
    db.add_mod_tag("4001", TAG_TYPE_INVALID, "gone")
    db.add_mod_tag("4001", TAG_TYPE_CONFLICT, "")

    card = ModCardWidget(
        mod, ModMetadata(published_file_id="4001", title="Badge", managed_path=str(mod))
    )
    # Conflict is the unified status badge — not the legacy top-left overlay.
    assert card.tag_badge.isHidden() or "Conflict" not in (card.tag_badge.text() or "")
    assert "冲突" in (card.missing_badge.text() or "")
    assert not card.missing_badge.isHidden()
    # Layout height unchanged vs untagged card
    plain = tmp_path / "Game" / "Plain"
    (plain / INFO_DIR_NAME).mkdir(parents=True)
    card_b = ModCardWidget(plain)
    assert card.height() == card_b.height()

    # Invalid alone → footer chip, not cover overlay
    db.remove_mod_tag("4001", TAG_TYPE_CONFLICT)
    card._apply_user_tag_badges()
    card._render_missing_content_badge()
    assert card.tag_badge.isHidden() or not (card.tag_badge.text() or "").strip()
    assert "失效" in (card.invalid_badge.text() or "")
    assert not card.invalid_badge.isHidden()


def test_deploy_hint_does_not_block(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)

    library = tmp_path / "mod"
    mod = library / "Game" / "Warn"
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        '{"published_file_id":"5001","title":"Warn","app_id":1}\n',
        encoding="utf-8",
    )
    db.upsert_mod(ModMetadata(published_file_id="5001", title="Warn"))
    db.add_mod_tag("5001", TAG_TYPE_INVALID, "broken")
    db.add_mod_tag("5001", TAG_TYPE_CONFLICT, "")

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
    view._on_deploy_action("5001", "deploy")
    assert started == ["yes"]
    hint = view.detail_panel.view_tag_deploy_hint.text()
    assert "失效" in hint
    assert "冲突" in hint


def test_library_filter_index_includes_tags(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "mod"
    for mid, title, tag in (
        ("6001", "Bad", TAG_TYPE_INVALID),
        ("6002", "Clash", TAG_TYPE_CONFLICT),
        ("6003", "Ok", None),
    ):
        mod = library / "Game" / title
        info = mod / INFO_DIR_NAME
        info.mkdir(parents=True)
        (info / METADATA_FILENAME).write_text(
            f'{{"published_file_id":"{mid}","title":"{title}","app_id":1}}\n',
            encoding="utf-8",
        )
        db.upsert_mod(ModMetadata(published_file_id=mid, title=title))
        if tag == TAG_TYPE_INVALID:
            db.add_mod_tag(mid, tag, "reason-xyz")
        elif tag:
            db.add_mod_tag(mid, tag, "")

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    assert any(idx.invalid for idx, _ in view._card_entries)
    assert any(idx.conflict for idx, _ in view._card_entries)
    # Search by tag_value
    assert matches_search(
        next(idx for idx, _ in view._card_entries if idx.invalid),
        "reason-xyz",
    )
