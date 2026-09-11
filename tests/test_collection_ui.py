"""Collection Phase 1 — Library work mode, list, cards, drag isolation."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager, get_db
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from services import collection as coll
from services.identity_service import create_mod_identity, identity_create_scope
from ui.collection_card import (
    COLLECTION_CREATE_CACHE_KEY,
    CollectionCardData,
    CollectionCardWidget,
    CollectionCreateCard,
    collection_card_height,
)
from ui.flow_layout import FlowLayout
from ui.library_query import FILTER_ALL, FILTER_DEPLOYED, STATUS_FILTER_LABELS
from ui.library_view import (
    COLLECTION_MODE_LIST,
    COLLECTION_MODE_NORMAL,
    EMPTY_SEARCH,
    ModLibraryView,
)
from ui.mod_card import CARD_WIDTH, COVER_HEIGHT, COVER_WIDTH, ModCardWidget

STARDEW = 413150
PALWORLD = 1623730
WH3 = 1142710


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _seed_game(app_id: int, name: str) -> DatabaseManager:
    db = get_db()
    db.upsert_game(GameInfo(app_id=app_id, name=name, folder_name=name))
    return db


def _flow_widgets(view: ModLibraryView) -> list[QWidget]:
    out: list[QWidget] = []
    for i in range(view.library_layout.count()):
        item = view.library_layout.itemAt(i)
        widget = item.widget() if item is not None else None
        if widget is not None:
            out.append(widget)
    return out


def _enter_list(view: ModLibraryView, app_id: int, name: str) -> None:
    view._set_current_game_context(name, game_id=app_id)
    view.btn_collection_mode.setEnabled(True)
    view.btn_collection_mode.setChecked(True)


def test_collection_mode_can_enter(qapp: QApplication) -> None:
    _seed_game(STARDEW, "Stardew Valley")
    view = ModLibraryView()
    _enter_list(view, STARDEW, "Stardew Valley")
    assert view._is_collection_list_mode() is True
    assert view._collection_mode == COLLECTION_MODE_LIST
    assert view.btn_collection_mode.isChecked() is True
    view.deleteLater()


def test_status_filters_all_grey_in_collection_mode(qapp: QApplication) -> None:
    _seed_game(STARDEW, "Stardew Valley")
    view = ModLibraryView()
    view._set_library_status_filter(FILTER_DEPLOYED)
    _enter_list(view, STARDEW, "Stardew Valley")
    assert view._filter_group.exclusive() is False
    for key, _label in STATUS_FILTER_LABELS:
        btn = view._filter_buttons[key]
        assert btn.isChecked() is False
    assert view._status_filter == FILTER_DEPLOYED
    view.deleteLater()


def test_clicking_filter_exits_collection_and_checks_that_filter(
    qapp: QApplication,
) -> None:
    _seed_game(STARDEW, "Stardew Valley")
    view = ModLibraryView()
    _enter_list(view, STARDEW, "Stardew Valley")
    view._filter_buttons[FILTER_DEPLOYED].click()
    assert view._is_collection_list_mode() is False
    assert view._collection_mode == COLLECTION_MODE_NORMAL
    assert view._status_filter == FILTER_DEPLOYED
    assert view._filter_buttons[FILTER_DEPLOYED].isChecked() is True
    assert view._filter_group.exclusive() is True
    view.deleteLater()


def test_wh3_sorting_and_collection_are_mutually_exclusive(
    qapp: QApplication, tmp_path: Path
) -> None:
    db = get_db()
    db.upsert_game(
        GameInfo(
            app_id=WH3,
            name="Total War: WARHAMMER III",
            folder_name="Warhammer3",
        )
    )
    view = ModLibraryView()
    view._set_current_game_context("Warhammer3", game_id=WH3)
    view.btn_wh3_sort_mode.setVisible(True)
    view.btn_wh3_sort_mode.setEnabled(True)
    view.btn_wh3_sort_mode.setChecked(True)
    assert view._wh3_sort_mode is True
    view.btn_collection_mode.setEnabled(True)
    view._enter_collection_mode()
    assert view._is_collection_list_mode() is True
    assert view._wh3_sort_mode is False
    assert view.btn_wh3_sort_mode.isChecked() is False

    view.btn_wh3_sort_mode.setChecked(True)
    assert view._wh3_sort_mode is True
    assert view._is_collection_list_mode() is False
    assert view.btn_collection_mode.isChecked() is False
    view.deleteLater()


def test_switching_game_exits_collection_mode(qapp: QApplication) -> None:
    _seed_game(STARDEW, "Stardew Valley")
    _seed_game(PALWORLD, "Palworld")
    view = ModLibraryView()
    _enter_list(view, STARDEW, "Stardew Valley")
    assert view._is_collection_list_mode() is True
    view._set_current_game_context("Palworld", game_id=PALWORLD)
    assert view._is_collection_list_mode() is False
    assert view.btn_collection_mode.isChecked() is False
    view.deleteLater()


def test_empty_collection_list_still_shows_create_card(qapp: QApplication) -> None:
    _seed_game(STARDEW, "Stardew Valley")
    view = ModLibraryView()
    _enter_list(view, STARDEW, "Stardew Valley")
    widgets = _flow_widgets(view)
    create = [w for w in widgets if isinstance(w, CollectionCreateCard)]
    cards = [w for w in widgets if isinstance(w, CollectionCardWidget)]
    assert len(create) == 1
    assert cards == []
    assert create[0] is widgets[0]
    assert view.empty_overlay.isHidden()
    assert view._empty_kind != EMPTY_SEARCH
    assert view.count_label.text() == "0 合集"
    view.deleteLater()


def test_create_card_is_always_first(qapp: QApplication) -> None:
    db = _seed_game(STARDEW, "Stardew Valley")
    coll.create_collection(STARDEW, "Later", db=db)
    view = ModLibraryView()
    _enter_list(view, STARDEW, "Stardew Valley")
    widgets = _flow_widgets(view)
    assert isinstance(widgets[0], CollectionCreateCard)
    assert any(isinstance(w, CollectionCardWidget) for w in widgets[1:])
    view.deleteLater()


def test_collection_card_shows_name_and_count(qapp: QApplication) -> None:
    data = CollectionCardData(
        collection_id=7,
        name="My Pack",
        mod_count=3,
        sort_order=0,
        app_id=STARDEW,
    )
    card = CollectionCardWidget(data)
    assert "My Pack" in card.title_label.text() or card.title_label.toolTip() == "My Pack"
    assert card.count_overlay.text() == "3 Mods"
    assert card.count_overlay.parent() is card.cover_label
    assert card.findChild(QLabel, "collectionCountLabel") is None
    card.deleteLater()


def test_collection_card_has_no_mod_specific_ui(qapp: QApplication) -> None:
    card = CollectionCardWidget(
        CollectionCardData(collection_id=1, name="Clean", mod_count=0)
    )
    create = CollectionCreateCard()
    assert card.objectName() == "collectionCard"
    assert create.objectName() == "collectionCreateCard"
    assert card.width() == CARD_WIDTH
    assert create.width() == CARD_WIDTH
    assert not isinstance(card, ModCardWidget)
    forbidden = (
        "favorite_badge",
        "load_order_badge",
        "relation_badge",
        "deploy_badge",
        "source_badge",
        "type_badge",
        "status_strip",
    )
    for name in forbidden:
        assert not hasattr(card, name)
        assert not hasattr(create, name)
    assert card.findChild(QWidget, "modCard") is None
    assert card.findChild(QWidget, "modCardStatusStrip") is None
    assert card.findChild(QWidget, "modFooterFavoriteChip") is None
    widget_src = inspect.getsource(CollectionCardWidget)
    assert "favorite" not in widget_src.lower()
    assert "load_order" not in widget_src.lower()
    assert "deploy_status" not in widget_src.lower()
    card.deleteLater()
    create.deleteLater()


def test_create_and_collection_cards_share_fixed_size(qapp: QApplication) -> None:
    create = CollectionCreateCard()
    card = CollectionCardWidget(
        CollectionCardData(collection_id=1, name="Sized", mod_count=12)
    )
    expected = collection_card_height()
    assert create.width() == card.width() == CARD_WIDTH
    assert create.height() == card.height() == expected
    assert create.height() == create.minimumHeight() == create.maximumHeight()
    assert card.height() == card.minimumHeight() == card.maximumHeight()
    create.deleteLater()
    card.deleteLater()


def test_count_overlay_lives_on_cover_not_in_vbox(qapp: QApplication) -> None:
    card = CollectionCardWidget(
        CollectionCardData(collection_id=2, name="Overlay", mod_count=4)
    )
    overlay = card.count_overlay
    assert overlay.objectName() == "collectionCountOverlay"
    assert overlay.parent() is card.cover_label
    assert overlay.text() == "4 Mods"
    assert card.findChild(QLabel, "collectionCountLabel") is None
    body = card.layout()
    for i in range(body.count()):
        item = body.itemAt(i)
        widget = item.widget() if item is not None else None
        assert widget is not overlay
        if widget is not None:
            assert widget.objectName() != "collectionCountLabel"
    card.adjustSize()
    assert overlay.x() > COVER_WIDTH // 2
    assert overlay.y() > COVER_HEIGHT // 2
    assert overlay.x() + overlay.width() <= COVER_WIDTH
    assert overlay.y() + overlay.height() <= COVER_HEIGHT
    before_h = card.height()
    card.rebind(CollectionCardData(collection_id=2, name="Overlay", mod_count=99))
    assert overlay.text() == "99 Mods"
    assert card.height() == before_h == collection_card_height()
    card.deleteLater()


def test_collection_actions_are_compact_icon_row(qapp: QApplication) -> None:
    card = CollectionCardWidget(
        CollectionCardData(collection_id=3, name="Actions", mod_count=0)
    )
    assert card.btn_rename.parent() is card.action_row
    assert card.btn_cover.parent() is card.action_row
    assert card.btn_delete.parent() is card.action_row
    row_layout = card.action_row.layout()
    assert isinstance(row_layout, QHBoxLayout)
    assert not isinstance(row_layout, FlowLayout)
    assert row_layout.indexOf(card.btn_rename) == 0
    assert row_layout.indexOf(card.btn_cover) == 1
    assert row_layout.indexOf(card.btn_delete) == 2
    buttons = card.findChildren(QPushButton)
    assert len(buttons) == 3
    assert set(buttons) == {card.btn_rename, card.btn_cover, card.btn_delete}
    assert card.btn_rename.objectName() == "collectionIconButton"
    assert card.btn_cover.objectName() == "collectionIconButton"
    assert card.btn_delete.objectName() == "collectionIconDangerButton"
    assert card.btn_rename.objectName() not in {"libraryIconButton", "panelIconDangerButton"}
    assert card.btn_cover.height() == 24
    assert card.btn_rename.height() <= card.action_row.height()
    assert card.btn_cover.height() <= card.action_row.height()
    assert card.btn_delete.height() <= card.action_row.height()
    from ui.collection_card import ACTION_STRIP_HEIGHT, collection_card_height

    assert card.action_row.height() == ACTION_STRIP_HEIGHT
    assert card.height() == collection_card_height()
    card.deleteLater()


def test_empty_cover_uses_placeholder_not_loader(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.cover_loader import CoverLoaderManager

    calls: list[tuple] = []
    mgr = CoverLoaderManager.instance()
    monkeypatch.setattr(
        mgr, "request", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    card = CollectionCardWidget(
        CollectionCardData(collection_id=9, name="NoCover", cover="", mod_count=0)
    )
    assert calls == []
    assert card._cover_token == ""
    card.deleteLater()


def test_nonempty_cover_requests_cover_loader(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtGui import QImage

    from core.paths import collection_covers_dir
    from services.cover_loader import CoverLoaderManager

    covers = collection_covers_dir()
    image = QImage(32, 32, QImage.Format.Format_RGB32)
    image.fill(1)
    dest = covers / "11.png"
    image.save(str(dest), "PNG")
    calls: list[tuple] = []

    def _capture(token, managed_path, **kwargs):
        calls.append((token, Path(managed_path), kwargs.get("cover_ref", "")))

    mgr = CoverLoaderManager.instance()
    monkeypatch.setattr(mgr, "request", _capture)
    card = CollectionCardWidget(
        CollectionCardData(
            collection_id=11,
            name="HasCover",
            cover="collection_covers/11.png",
            mod_count=1,
        )
    )
    assert calls
    token, managed, ref = calls[0]
    assert str(token).startswith("collection:")
    assert Path(managed) == covers
    assert str(ref).strip()
    assert Path(ref).is_file()
    card.deleteLater()


def test_collection_drag_does_not_touch_wh3_or_deploy(
    qapp: QApplication, tmp_path: Path
) -> None:
    from services.wh3_activation import (
        load_saved_order,
        persist_load_order,
        sync_used_mods_txt,
        used_mods_path,
    )

    db = get_db()
    install = tmp_path / "WH3Install"
    data_dir = tmp_path / "WH3Data"
    install.mkdir()
    data_dir.mkdir()
    (install / "Warhammer3.exe").write_bytes(b"MZ")
    db.upsert_game(
        GameInfo(
            app_id=WH3,
            name="Total War: WARHAMMER III",
            folder_name="Warhammer3",
        )
    )
    db.update_game_deploy_config(
        WH3,
        name="Total War: WARHAMMER III",
        install_path=str(install),
        mod_path=str(data_dir),
        workshop_path=str(tmp_path / "workshop"),
    )
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id="41001",
            workshop_id="41001",
            title="WH3 Mod",
            app_id=WH3,
            game_name="Total War: WARHAMMER III",
            operation="import",
        )
    mid = str(created.mod_id)
    db.update_mod_deploy_status(mid, deploy_status=DEPLOY_STATUS_DEPLOYED, deploy_path="x")
    persist_load_order([mid], db, library_root=tmp_path / "mod")
    sync_used_mods_txt(db, library_root=tmp_path / "mod")
    before_order = list(load_saved_order())
    used = used_mods_path(install)
    before_used = used.read_text(encoding="utf-8") if used.is_file() else ""
    before_enabled = db.is_mod_enabled(mid)
    before_deploy = db.get_mod_deploy_info(mid).deploy_status

    a = coll.create_collection(WH3, "Alpha", db=db)
    b = coll.create_collection(WH3, "Beta", db=db)
    view = ModLibraryView()
    view._set_current_game_context("Warhammer3", game_id=WH3)
    view.btn_collection_mode.setEnabled(True)
    view._enter_collection_mode()
    view._on_collection_sort_drop(str(b.collection_id), str(a.collection_id))
    names = [
        item.name
        for item in view._collection_list_entries
        if item is not None
    ]
    assert names == ["Beta", "Alpha"]
    assert load_saved_order() == before_order
    after_used = used.read_text(encoding="utf-8") if used.is_file() else ""
    assert after_used == before_used
    assert db.is_mod_enabled(mid) is before_enabled
    assert db.get_mod_deploy_info(mid).deploy_status == before_deploy
    src = inspect.getsource(ModLibraryView._on_collection_sort_drop)
    assert "apply_card_drop" not in src
    assert "persist_load_order" not in src
    assert "sync_used_mods_txt" not in src
    view.deleteLater()


def test_selection_does_not_carry_into_collection_mode(qapp: QApplication) -> None:
    _seed_game(STARDEW, "Stardew Valley")
    view = ModLibraryView()
    view._selected_mod_ids = ["12", "34"]
    view._selection_anchor_mod_id = "12"
    view._selected_mod_id = "34"
    _enter_list(view, STARDEW, "Stardew Valley")
    assert view._selected_mod_ids == []
    assert view._selection_anchor_mod_id == ""
    assert view._selected_mod_id == ""
    view._exit_collection_mode(apply_filter=True, restore_chips=True)
    assert view._selected_mod_ids == []
    assert view._selection_anchor_mod_id == ""
    view.deleteLater()


def test_create_rename_delete_via_ui_dialogs(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _seed_game(STARDEW, "Stardew Valley")
    view = ModLibraryView()
    _enter_list(view, STARDEW, "Stardew Valley")

    monkeypatch.setattr(
        QInputDialog, "getText", staticmethod(lambda *a, **k: ("Alpha", True))
    )
    view._on_create_collection()
    recs = coll.list_collections(STARDEW, db=db)
    assert [r.name for r in recs] == ["Alpha"]
    cid = recs[0].collection_id

    monkeypatch.setattr(
        QInputDialog, "getText", staticmethod(lambda *a, **k: ("Beta", True))
    )
    view._on_rename_collection(cid)
    assert coll.get_collection(cid, db=db).name == "Beta"

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    view._on_delete_collection(cid)
    assert coll.get_collection(cid, db=db) is None
    widgets = _flow_widgets(view)
    assert isinstance(widgets[0], CollectionCreateCard)
    assert not any(isinstance(w, CollectionCardWidget) for w in widgets)
    view.deleteLater()


def test_collection_not_a_status_filter() -> None:
    from ui import library_query, library_view

    assert not hasattr(library_query, "FILTER_COLLECTION")
    src = inspect.getsource(library_view)
    assert "FILTER_COLLECTION" not in src
    assert library_view.COLLECTION_MODE_LIST != library_query.FILTER_ALL
    assert COLLECTION_CREATE_CACHE_KEY.startswith("collection:")


def test_collection_list_cards_share_outer_size(qapp: QApplication) -> None:
    db = _seed_game(STARDEW, "Stardew Valley")
    coll.create_collection(STARDEW, "Pack", db=db)
    view = ModLibraryView()
    _enter_list(view, STARDEW, "Stardew Valley")
    widgets = [
        w
        for w in _flow_widgets(view)
        if isinstance(w, (CollectionCreateCard, CollectionCardWidget))
    ]
    assert any(isinstance(w, CollectionCreateCard) for w in widgets)
    assert any(isinstance(w, CollectionCardWidget) for w in widgets)
    assert {w.width() for w in widgets} == {CARD_WIDTH}
    assert {w.height() for w in widgets} == {collection_card_height()}
    view.deleteLater()


def test_non_wh3_mode_column_does_not_move_filter(qapp: QApplication) -> None:
    from PySide6.QtCore import QCoreApplication

    from ui.styles import APP_STYLE

    qapp.setStyleSheet(APP_STYLE)
    _seed_game(STARDEW, "Stardew Valley")
    view = ModLibraryView()
    view.resize(1280, 800)
    view.show()
    QCoreApplication.processEvents()
    view._set_current_game_context("Stardew Valley", game_id=STARDEW)
    view._apply_filter_row_height()
    QCoreApplication.processEvents()
    chips_h = view._status_chips.height()
    meta_y = view._meta_bar.y()
    mode_h = view._record_actions.height()
    assert view.btn_wh3_sort_mode.isHidden()
    _enter_list(view, STARDEW, "Stardew Valley")
    QCoreApplication.processEvents()
    assert view._status_chips.height() == chips_h
    assert view._meta_bar.y() == meta_y
    assert view._record_actions.height() == mode_h
    view.deleteLater()


def test_empty_collection_disables_member_cover_menu(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PySide6.QtWidgets import QMenu

    db = _seed_game(STARDEW, "Stardew Valley")
    rec = coll.create_collection(STARDEW, "Empty", db=db)
    view = ModLibraryView()
    _enter_list(view, STARDEW, "Stardew Valley")
    enabled: dict[str, bool] = {}

    class _Menu(QMenu):
        def exec(self, *args, **kwargs):  # noqa: A003
            for act in self.actions():
                enabled[str(act.text())] = bool(act.isEnabled())
            return None

    monkeypatch.setattr("ui.library_view.QMenu", _Menu)
    view._on_collection_cover_requested(rec.collection_id)
    assert enabled.get("本地图片") is True
    assert enabled.get("从合集 Mod 选择") is False
    view.deleteLater()
