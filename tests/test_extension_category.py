"""Optional「分类」metadata for Mod type「拓展」only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import MOD_TYPE_EXTENSION, visible_extension_category
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.mod_type_catalog import get_mod_type_catalog
from tests.helpers.identity import create_steam_test_mod
from ui.edit_mod_dialog import EditModDialog

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

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
    manager = DatabaseManager.instance(tmp_path / "extension_category.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(
    library: Path,
    db: DatabaseManager,
    *,
    external_id: str,
    title: str,
    display_name: str = "",
    description: str = "介绍正文",
) -> tuple[Path, str]:
    folder = library / "SomeGame" / title
    folder.mkdir(parents=True)
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    db.update_game_deploy_config(100, name="SomeGame", mod_path="")
    created = create_steam_test_mod(
        db,
        external_id=external_id,
        title=title,
        app_id=100,
        game_name="SomeGame",
    )
    pk = str(created.mod_id)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "internal_id": pk,
                "published_file_id": external_id,
                "title": title,
                "description": description,
                "app_id": 100,
                "game_name": "SomeGame",
                "source_type": "steam",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (folder / "a.txt").write_text("x", encoding="utf-8")
    db.update_mod_identity_fields(
        pk,
        last_known_path=str(folder),
        folder_present=True,
    )
    if display_name:
        db.update_mod_user_metadata(
            pk,
            {
                "display_name": display_name,
                "custom_description": description,
                "user_notes": "",
                "favorite": False,
            },
        )
    else:
        db.update_mod_user_metadata(
            pk,
            {
                "display_name": "",
                "custom_description": description,
                "user_notes": "",
                "favorite": False,
            },
        )
    return folder, pk


def _bind_named_type(db: DatabaseManager, pk: str, app_id: int, name: str):
    catalog = get_mod_type_catalog()
    for existing in catalog.list_types(app_id):
        if existing.name == name:
            db.set_mod_type_id(pk, existing.type_id)
            return existing
    created = catalog.add_type(app_id, name)
    db.set_mod_type_id(pk, created.type_id)
    return created


def _show(panel: ModDetailPanel, folder: Path, pk: str, *, width: int = 420) -> None:
    panel.resize(width, 900)
    panel.show()
    panel.show_mod(folder, mod_id=pk, game_id=100, game_name="SomeGame")
    QApplication.processEvents()


def _rich_and_category(panel: ModDetailPanel) -> tuple[str, str, bool]:
    rich = str(panel.meta_rich_label.text() or "")
    marker = "<b>分类：</b>"
    shown = marker in rich
    cat = ""
    if shown:
        tail = rich.split(marker, 1)[1]
        value = tail.split("<", 1)[0].replace("&amp;", "&").strip()
        cat = f"分类：{value}"
    return rich, cat, shown


def _rich_needed_height(label) -> int:
    from PySide6.QtGui import QTextDocument

    width = int(label.contentsRect().width() or label.width() or 0)
    if width <= 0:
        width = 360
    doc = QTextDocument()
    doc.setDefaultFont(label.font())
    doc.setDocumentMargin(0)
    doc.setHtml(str(label.text() or ""))
    doc.setTextWidth(width)
    return int(doc.size().height())


def _assert_category_geometry_visible(panel: ModDetailPanel, expected: str) -> None:
    rich, cat, visible = _rich_and_category(panel)
    assert visible is True
    assert cat == f"分类：{expected}"
    needed = _rich_needed_height(panel.meta_rich_label)
    assert needed <= panel.meta_rich_label.height() + 2
    assert panel.meta_rich_label.height() <= panel.meta_rich_label.maximumHeight()


def _rename_type_display_name(app_id: int, type_id: int, new_name: str) -> None:
    catalog = get_mod_type_catalog()
    payload = json.loads(catalog.path.read_text(encoding="utf-8"))
    game = payload["games"][str(app_id)]
    for item in game["types"]:
        if int(item["id"]) == int(type_id):
            item["name"] = new_name
            break
    else:
        raise AssertionError(f"type_id={type_id} missing for app_id={app_id}")
    catalog.path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    catalog.reload()


def test_extension_category_shows_without_original_name(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed(
        tmp_path / "lib",
        db,
        external_id="88110",
        title="SameName",
        description="介绍正文",
    )
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": "动画",
        },
    )
    panel = ModDetailPanel()
    _show(panel, folder, pk)
    rich, cat, visible = _rich_and_category(panel)
    assert visible is True
    assert cat == "分类：动画"
    assert "原名" not in rich
    assert rich.find("名称") < rich.find("分类")
    needed = _rich_needed_height(panel.meta_rich_label)
    assert needed <= panel.meta_rich_label.height() + 2
    assert panel.meta_rich_label.height() <= panel.meta_rich_label.maximumHeight()


def test_extension_category_not_clipped_when_original_name_present(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed(
        tmp_path / "lib",
        db,
        external_id="88111",
        title="秘密结社增强模组\\Secret Society Enhanced Mod",
        display_name="秘密结社增强模组",
        description="介绍正文",
    )
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "秘密结社增强模组",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": "秘密结社",
        },
    )
    panel = ModDetailPanel()
    _show(panel, folder, pk)
    rich, cat, visible = _rich_and_category(panel)
    assert visible is True
    assert cat == "分类：秘密结社"
    assert "<b>原名：</b>" in rich
    assert rich.find("<b>名称：</b>") < rich.find("<b>原名：</b>") < rich.find(
        "<b>分类：</b>"
    )
    needed = _rich_needed_height(panel.meta_rich_label)
    assert needed <= panel.meta_rich_label.height() + 2
    assert needed <= panel.meta_rich_label.maximumHeight()
    assert panel.meta_rich_label.height() <= panel.meta_rich_label.maximumHeight()


def test_visible_extension_category_uses_type_id_not_name() -> None:
    catalog = get_mod_type_catalog()
    ext = catalog.add_type(100, MOD_TYPE_EXTENSION)
    other = catalog.add_type(100, "美化")
    assert visible_extension_category("", app_id=100, type_id=ext.type_id) == ""
    assert visible_extension_category("   ", app_id=100, type_id=ext.type_id) == ""
    assert visible_extension_category("动画", app_id=100, type_id=ext.type_id) == "动画"
    assert visible_extension_category("UI", app_id=100, type_id=ext.type_id) == "UI"
    assert visible_extension_category("动画", app_id=100, type_id=other.type_id) == ""
    assert visible_extension_category("动画", app_id=100, type_id=None) == ""
    assert visible_extension_category("动画", app_id=0, type_id=ext.type_id) == ""

    _rename_type_display_name(100, ext.type_id, "扩展")
    assert catalog.resolve_name(100, ext.type_id) == "扩展"
    assert catalog.extension_type_id(100) == ext.type_id
    assert catalog.find_type_by_name(100, "拓展") is None
    assert visible_extension_category("动画", app_id=100, type_id=ext.type_id) == "动画"
    assert visible_extension_category("动画", app_id=100, type_id=other.type_id) == ""


def test_default_extension_hides_empty_category(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed(tmp_path / "lib", db, external_id="88101", title="ExtEmpty")
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    info = db.get_mod_display_info(pk)
    assert info is not None
    assert (info.category or "") == ""

    panel = ModDetailPanel()
    _show(panel, folder, pk)
    rich, cat, visible = _rich_and_category(panel)
    assert "名称" in rich
    assert "分类" not in rich
    assert cat == ""
    assert visible is False
    assert "介绍" in (panel.meta_desc_caption.text() or "")


def test_extension_with_category_shows_between_name_and_intro(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed(
        tmp_path / "lib",
        db,
        external_id="88102",
        title="Steam Original Title",
        display_name="显示名称",
        description="介绍正文",
    )
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "显示名称",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": "动画",
        },
    )

    panel = ModDetailPanel()
    _show(panel, folder, pk)
    rich, cat, visible = _rich_and_category(panel)
    assert "名称" in rich
    assert "原名" in rich
    assert visible is True
    assert cat == "分类：动画"
    assert "<b>分类：</b>" in rich
    assert "介绍正文" in (panel.meta_desc_label.text() or "")

    i_name = rich.find("名称")
    i_orig = rich.find("原名")
    i_cat = rich.find("分类")
    assert 0 <= i_name < i_orig < i_cat

    body = panel.meta_rich_label.parentWidget()
    assert body is not None
    layout = body.layout()
    assert layout is not None
    ordered: list = []
    for i in range(layout.count()):
        item = layout.itemAt(i)
        w = item.widget() if item is not None else None
        if w is not None:
            ordered.append(w)
    i_rich = ordered.index(panel.meta_rich_label)
    i_intro = ordered.index(panel.meta_desc_frame)
    assert i_rich < i_intro
    assert panel.meta_category_line not in ordered
    assert panel.meta_rich_label.objectName() == panel.meta_desc_caption.objectName()
    assert panel.meta_rich_label.objectName() == "detailMetaLine"
    assert panel.meta_rich_label.x() == panel.meta_desc_frame.x()


def test_clear_category_hides_row(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed(tmp_path / "lib", db, external_id="88103", title="ExtClear")
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": "动画",
        },
    )
    panel = ModDetailPanel()
    _show(panel, folder, pk)
    rich, cat, visible = _rich_and_category(panel)
    assert visible is True
    assert cat == "分类：动画"

    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": "",
        },
    )
    _show(panel, folder, pk)
    info = db.get_mod_display_info(pk)
    assert info is not None
    assert (info.category or "") == ""
    rich, cat, visible = _rich_and_category(panel)
    assert visible is False
    assert "分类" not in rich


def test_non_extension_hides_historical_category(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed(tmp_path / "lib", db, external_id="88104", title="NormalCat")
    _bind_named_type(db, pk, 100, "普通")
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": "动画",
        },
    )
    info = db.get_mod_display_info(pk)
    assert info is not None
    assert info.category == "动画"

    panel = ModDetailPanel()
    _show(panel, folder, pk)
    rich, _cat, visible = _rich_and_category(panel)
    assert visible is False
    assert "分类：动画" not in rich
    assert "分类" not in rich


def test_edit_save_persists_category(db: DatabaseManager) -> None:
    db.update_game_deploy_config(100, name="SomeGame", mod_path="")
    created = create_steam_test_mod(
        db, external_id="88105", title="EditCat", app_id=100, game_name="SomeGame"
    )
    pk = str(created.mod_id)
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
            "category": "UI",
        },
    )
    again = db.get_mod_display_info(pk)
    assert again is not None
    assert again.category == "UI"


def test_type_switch_hides_then_restores_category(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed(tmp_path / "lib", db, external_id="88106", title="SwitchType")
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": "动画",
        },
    )
    panel = ModDetailPanel()
    _show(panel, folder, pk)
    rich, cat, visible = _rich_and_category(panel)
    assert visible is True
    assert cat == "分类：动画"

    _bind_named_type(db, pk, 100, "美化")
    _show(panel, folder, pk)
    assert db.get_mod_display_info(pk).category == "动画"
    rich, _cat, visible = _rich_and_category(panel)
    assert visible is False
    assert "分类" not in rich

    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    _show(panel, folder, pk)
    assert db.get_mod_display_info(pk).category == "动画"
    rich, cat, visible = _rich_and_category(panel)
    assert visible is True
    assert cat == "分类：动画"


def test_edit_dialog_category_only_for_extension(qapp: QApplication) -> None:
    catalog = get_mod_type_catalog()
    ext_type = catalog.add_type(100, MOD_TYPE_EXTENSION)
    other_type = catalog.add_type(100, "美化")
    ext = EditModDialog(
        mod_id="1",
        game_id=100,
        mod_type_id=ext_type.type_id,
        type_options=[
            (ext_type.type_id, catalog.resolve_name(100, ext_type.type_id)),
            (other_type.type_id, "美化"),
        ],
        category="动画",
        description="介绍正文",
    )
    assert not ext.category_edit.isHidden()
    assert ext.category_edit.text() == "动画"
    assert ext.values()["category"] == "动画"
    assert ext.values()["mod_type"] == MOD_TYPE_EXTENSION
    assert ext.values()["type_id"] == ext_type.type_id

    ext.type_combo.setCurrentIndex(ext.type_combo.findData(other_type.type_id))
    QApplication.processEvents()
    assert ext.category_edit.isHidden()
    assert ext.values()["category"] == "动画"
    assert ext.values()["type_id"] == other_type.type_id

    other = EditModDialog(
        mod_id="2",
        game_id=100,
        mod_type_id=other_type.type_id,
        type_options=[
            (ext_type.type_id, catalog.resolve_name(100, ext_type.type_id)),
            (other_type.type_id, "美化"),
        ],
        category="动画",
    )
    assert other.category_edit.isHidden()
    assert other.values()["category"] == "动画"
    assert other.values()["type_id"] == other_type.type_id


def test_category_shares_name_row_geometry(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed(
        tmp_path / "lib",
        db,
        external_id="88107",
        title="Steam Original Title",
        display_name="显示名称",
        description="介绍正文",
    )
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "显示名称",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": "动画",
        },
    )
    panel = ModDetailPanel()
    _show(panel, folder, pk)

    rich = str(panel.meta_rich_label.text() or "")
    assert "<b>名称：</b>" in rich
    assert "<b>原名：</b>" in rich
    assert "<b>分类：</b>" in rich
    assert rich.find("<b>名称：</b>") < rich.find("<b>原名：</b>") < rich.find(
        "<b>分类：</b>"
    )

    spacing = panel.meta_rich_label.parentWidget().layout().spacing()
    gap = panel.meta_desc_frame.y() - (
        panel.meta_rich_label.y() + panel.meta_rich_label.height()
    )
    assert gap == spacing
    assert panel.meta_rich_label.x() == panel.meta_desc_frame.x()


def test_empty_category_leaves_no_extra_gap(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed(tmp_path / "lib", db, external_id="88108", title="NoCatGap")
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    panel = ModDetailPanel()
    _show(panel, folder, pk)
    assert "<b>分类：</b>" not in (panel.meta_rich_label.text() or "")
    spacing = panel.meta_rich_label.parentWidget().layout().spacing()
    gap = panel.meta_desc_frame.y() - (
        panel.meta_rich_label.y() + panel.meta_rich_label.height()
    )
    assert gap == spacing


def test_long_category_stays_in_name_block(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    long_cat = "动画" * 40
    folder, pk = _seed(tmp_path / "lib", db, external_id="88109", title="LongCat")
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": long_cat,
        },
    )
    panel = ModDetailPanel()
    _show(panel, folder, pk)
    rich, cat, visible = _rich_and_category(panel)
    assert visible is True
    assert cat.startswith("分类：")
    assert "动画" in cat
    assert panel.meta_rich_label.height() <= panel.meta_rich_label.maximumHeight()
    assert panel.meta_rich_label.y() + panel.meta_rich_label.height() <= (
        panel.meta_desc_frame.y()
    )


def test_renaming_type_name_does_not_hide_category(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed(
        tmp_path / "lib",
        db,
        external_id="88120",
        title="RenameType",
        description="介绍正文",
    )
    created = _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    type_id = created.type_id
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": "动画",
        },
    )
    catalog = get_mod_type_catalog()
    assert catalog.extension_type_id(100) == type_id
    assert db.get_mod_type_id(pk) == type_id

    panel = ModDetailPanel()
    _show(panel, folder, pk)
    _assert_category_geometry_visible(panel, "动画")

    _rename_type_display_name(100, type_id, "扩展")
    assert db.get_mod_type_id(pk) == type_id
    assert catalog.resolve_name(100, type_id) == "扩展"
    assert catalog.extension_type_id(100) == type_id
    assert catalog.find_type_by_name(100, "拓展") is None

    panel.show_mod(folder, mod_id=pk, game_id=100, game_name="SomeGame")
    QApplication.processEvents()
    _assert_category_geometry_visible(panel, "动画")
    assert db.get_mod_type_id(pk) == type_id
    panel.close()


def test_edit_dialog_category_follows_type_id_after_rename(qapp: QApplication) -> None:
    catalog = get_mod_type_catalog()
    ext_type = catalog.add_type(100, MOD_TYPE_EXTENSION)
    other_type = catalog.add_type(100, "美化")
    _rename_type_display_name(100, ext_type.type_id, "扩展")
    assert catalog.resolve_name(100, ext_type.type_id) == "扩展"
    dlg = EditModDialog(
        mod_id="3",
        game_id=100,
        mod_type_id=ext_type.type_id,
        type_options=[
            (ext_type.type_id, "扩展"),
            (other_type.type_id, "美化"),
        ],
        category="动画",
    )
    assert dlg.values()["mod_type"] == "扩展"
    assert dlg.values()["type_id"] == ext_type.type_id
    assert not dlg.category_edit.isHidden()


def test_extension_category_is_per_game_type_id(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    catalog = get_mod_type_catalog()
    other_game = catalog.add_type(200, "美化")
    ext_other = catalog.add_type(200, MOD_TYPE_EXTENSION)
    ext_here = catalog.add_type(100, MOD_TYPE_EXTENSION)
    assert catalog.extension_type_id(100) == ext_here.type_id
    assert catalog.extension_type_id(200) == ext_other.type_id
    assert catalog.is_extension_type(100, ext_here.type_id)
    assert catalog.is_extension_type(200, ext_other.type_id)
    assert not catalog.is_extension_type(200, other_game.type_id)
    assert not catalog.is_extension_type(200, ext_here.type_id)

    folder, pk = _seed(
        tmp_path / "lib",
        db,
        external_id="88121",
        title="OtherGameType",
        description="介绍正文",
    )
    db.set_mod_type_id(pk, ext_other.type_id)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": "动画",
        },
    )
    panel = ModDetailPanel()
    _show(panel, folder, pk)
    rich, _cat, visible = _rich_and_category(panel)
    assert visible is False
    assert "分类" not in rich
    panel.close()


def test_resize_keeps_category_visible_wide_and_narrow(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed(
        tmp_path / "lib",
        db,
        external_id="88122",
        title="Secret Society Enhanced Mod",
        display_name="秘密结社增强模组",
        description="介绍正文",
    )
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "秘密结社增强模组",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": "动画",
        },
    )
    panel = ModDetailPanel()
    _show(panel, folder, pk, width=720)
    _assert_category_geometry_visible(panel, "动画")
    assert "<b>原名：</b>" in (panel.meta_rich_label.text() or "")

    panel.resize(240, 900)
    QApplication.processEvents()
    _assert_category_geometry_visible(panel, "动画")
    panel.close()


def test_narrow_width_original_name_and_category(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed(
        tmp_path / "lib",
        db,
        external_id="88123",
        title="Original English Title",
        display_name="显示名称",
        description="介绍正文",
    )
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "显示名称",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": "动画",
        },
    )
    panel = ModDetailPanel()
    _show(panel, folder, pk, width=240)
    _assert_category_geometry_visible(panel, "动画")
    assert "<b>原名：</b>" in (panel.meta_rich_label.text() or "")
    panel.close()


def test_original_name_and_long_category_geometry(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    long_cat = "动画扩展分类标记" * 6
    folder, pk = _seed(
        tmp_path / "lib",
        db,
        external_id="88124",
        title="Original English Title",
        display_name="显示名称",
        description="介绍正文",
    )
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "显示名称",
            "custom_description": "介绍正文",
            "user_notes": "",
            "favorite": False,
            "category": long_cat,
        },
    )
    panel = ModDetailPanel()
    _show(panel, folder, pk)
    _assert_category_geometry_visible(panel, long_cat)
    panel.close()


def test_no_category_has_no_extra_blank_after_resize(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed(tmp_path / "lib", db, external_id="88125", title="NoCatResize")
    _bind_named_type(db, pk, 100, MOD_TYPE_EXTENSION)
    panel = ModDetailPanel()
    _show(panel, folder, pk, width=720)
    assert "<b>分类：</b>" not in (panel.meta_rich_label.text() or "")
    assert panel.meta_rich_label.height() < 80
    panel.resize(240, 900)
    QApplication.processEvents()
    assert "<b>分类：</b>" not in (panel.meta_rich_label.text() or "")
    assert panel.meta_rich_label.height() < 80
    panel.close()
