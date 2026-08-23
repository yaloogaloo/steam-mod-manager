"""Phase 3 polish: game counts, search placeholder, card consistency."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from ui.library_view import ALL_GAMES_LABEL, GAME_ROLE, ModLibraryView
from ui.mod_card import OFFLINE_MISSING_LABEL, ModCardWidget


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "phase3.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(root: Path, game: str, pub_id: str, title: str) -> Path:
    folder = root / game / title
    info = folder / ".info"
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps(
            {
                "published_file_id": pub_id,
                "title": title,
                "game_name": game,
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_game_list_shows_mod_counts(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "library"
    _seed(lib, "Palworld", "93001", "A")
    _seed(lib, "Palworld", "93002", "B")
    _seed(lib, "OtherGame", "93003", "C")
    for mid, title in (("93001", "A"), ("93002", "B"), ("93003", "C")):
        db.upsert_mod(ModMetadata(published_file_id=mid, title=title))

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()

    # Display comes from item widgets (item.text() stays empty to avoid ghost paint)
    rows = []
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        widget = view.game_list.itemWidget(item)
        assert widget is not None
        assert item.text() == ""
        rows.append((widget.name_label.text(), widget.count_label.text()))
    assert any(n == ALL_GAMES_LABEL and c == "3" for n, c in rows)
    assert any(n == "Palworld" and c == "2" for n, c in rows)
    assert any(n == "OtherGame" and c == "1" for n, c in rows)

    # Filter key stored separately from display text
    pal = None
    for i in range(view.game_list.count()):
        item = view.game_list.item(i)
        if item.data(GAME_ROLE) == "Palworld":
            pal = item
            break
    assert pal is not None


def test_search_box_ui_present_and_editable(
    qapp: QApplication, tmp_path: Path
) -> None:
    view = ModLibraryView()
    view.set_target_root(str(tmp_path / "library"))
    (tmp_path / "library").mkdir()
    view.refresh()

    assert hasattr(view, "search_box")
    assert not view.search_box.isReadOnly()
    assert view.search_box.isClearButtonEnabled()
    assert view.search_box.minimumHeight() >= 32
    assert "搜索" in view.search_box.placeholderText()
    assert set(view._filter_buttons) >= {"all", "favorite", "deployed", "anomaly"}


def test_card_tooltip_is_simple_title(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import MagicMock

    mod = tmp_path / "G" / "M"
    (mod / ".info").mkdir(parents=True)
    info = MagicMock(
        steam_name="Steam Title Long Enough",
        display_name="Display",
        user_display_name="Display",
        favorite=False,
        platform="steam",
        source_url="",
        external_id="1",
        mod_version="",
        installed_version="",
        offline_status="none",
    )
    db = MagicMock()
    db.get_mod_display_info.return_value = info
    db.get_mod_deploy_info.return_value = None
    db.get_mod_status.return_value = None
    db.is_mod_enabled.return_value = True
    db.get_mods_tag_flags.return_value = {}
    db.get_relationship_counts.return_value = {}
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)

    card = ModCardWidget(
        mod, ModMetadata(published_file_id="1", title="Steam Title Long Enough")
    )
    assert not hasattr(card, "steam_label")
    assert not hasattr(card, "meta_label")
    tip = card.toolTip()
    # Hover panel removed — tooltip is display name only.
    assert tip == "Display"
    assert "Platform:" not in tip
    assert "External ID" not in tip
    assert not card.offline_badge.isHidden()
    assert card.offline_badge.text() == OFFLINE_MISSING_LABEL
    assert card.height() == card.minimumHeight()
    assert card.title_label.toolTip() == "Display"


def test_selecting_mod_does_not_touch_archive(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clicking a card must not archive, network, or read index.html contents."""
    calls: list[str] = []

    def boom(*_a, **_k):
        calls.append("archive")
        raise AssertionError("archive must not run on select")

    monkeypatch.setattr(
        "services.archive.OfflinePageArchiver.archive", boom, raising=False
    )
    monkeypatch.setattr(
        "services.archive.OfflinePageArchiver.ensure_offline_page",
        boom,
        raising=False,
    )

    lib = tmp_path / "library"
    folder = _seed(lib, "G", "94001", "SafeSelect")
    index = folder / ".info" / "index.html"
    index.write_text("<html>heavy</html>", encoding="utf-8")

    # Fail if anyone reads the full HTML for status
    real_read = Path.read_text

    def guarded_read(self: Path, *a, **k):
        if self.name == "index.html":
            calls.append("read_html")
            raise AssertionError("must not read index.html on select")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", guarded_read)

    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.refresh()
    assert view._cards
    view.on_mod_selected(folder)
    assert view.detail_panel.view_title.text()
    assert calls == []


def test_detail_panel_sections_and_footer(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    from ui.mod_detail_panel import MODE_VIEW, ModDetailPanel

    lib = tmp_path / "library"
    folder = _seed(lib, "Palworld", "93111", "SectionMod")
    db.upsert_mod(ModMetadata(published_file_id="93111", title="SectionMod"))

    panel = ModDetailPanel()
    panel.show_mod(folder)
    assert panel._mode == MODE_VIEW
    assert panel.view_title.text() == "SectionMod"
    assert panel.view_title.toolTip() == "SectionMod"
    assert "名称：SectionMod" in panel.meta_name_line.text()
    assert panel.meta_source_line.text().startswith("来源：")
    # Action strip is independent; Deploy lives in footer
    assert panel.btn_folder is not None
    assert panel.btn_folder.text() == "打开目录"
    assert panel.btn_steam.text() == "打开官网"
    assert panel.btn_offline.text() == "打开离线页面"
    assert panel._view_footer is not None
    assert panel.btn_deploy.parentWidget() is panel._view_footer
    assert panel.btn_folder.parentWidget() is panel._view_footer
    assert panel.btn_steam.parentWidget() is panel._view_footer
    assert panel._view_footer is not panel._view_scroll
    assert panel.btn_deploy.text() == "部署"
    assert panel.btn_remove_mod.isHidden()
    assert panel.btn_folder.text() == "打开目录"
    assert "目录" in (panel.btn_folder.toolTip() or "")
    assert panel.btn_tag_conflict.text() == "冲突"
    assert panel.btn_tag_invalid.text() == "失效"
    # Status banner hidden for healthy mods
    assert panel._status_banner.isHidden()
