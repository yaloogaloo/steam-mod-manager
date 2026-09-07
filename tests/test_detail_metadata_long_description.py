"""Detail panel: stable metadata field layout + capped description frame."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel, QSizePolicy, QTextBrowser

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from ui.mod_detail_panel import (
    META_DESC_FRAME_MAX_HEIGHT_PX,
    META_DESC_MAX_HEIGHT_PX,
    META_DESC_MAX_PLAIN_CHARS,
    META_DESC_TRUNCATED_HINT,
    META_SECTION_SPACING_PX,
    ModDetailPanel,
    _collapse_description_html_gaps,
    _format_description_rich_html,
    _truncate_description_for_panel,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "detail_meta_layout.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed(
    library: Path,
    db: DatabaseManager,
    *,
    mid: str,
    title: str,
    description: str,
) -> Path:
    mods_root = library.parent / "GameMods"
    mods_root.mkdir(parents=True, exist_ok=True)
    db.update_game_deploy_config(
        100, name="SomeGame", mod_path=str(mods_root)
    )
    folder = library / "SomeGame" / title
    folder.mkdir(parents=True)
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "internal_id": mid,
                "published_file_id": mid,
                "title": title,
                "description": description,
                "app_id": 100,
                "game_name": "SomeGame",
                "workspace_id": mid,
                "source_type": "steam",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (folder / "a.txt").write_text("x", encoding="utf-8")
    db.upsert_mod(
        ModMetadata(
            published_file_id=mid,
            title=title,
            description=description,
            app_id=100,
            game_name="SomeGame",
        )
    )
    db.update_mod_identity_fields(
        mid,
        internal_id=mid,
        last_known_path=str(folder),
        folder_present=True,
        workspace_id=mid,
    )
    return folder


def _show(panel: ModDetailPanel, folder: Path, mid: str) -> None:
    panel.resize(420, 900)
    panel.show()
    panel.show_mod(folder, mod_id=mid, game_id=100, game_name="SomeGame")
    panel.meta_desc_frame.adjustSize()
    panel.updateGeometry()
    QApplication.processEvents()


def test_desc_max_height_constant() -> None:
    assert 520 <= META_DESC_MAX_HEIGHT_PX <= 600
    assert META_DESC_MAX_PLAIN_CHARS == 5000
    assert META_DESC_FRAME_MAX_HEIGHT_PX >= META_DESC_MAX_HEIGHT_PX
    assert META_SECTION_SPACING_PX == 8


def test_truncate_description_for_panel() -> None:
    short = "短介绍"
    out, truncated = _truncate_description_for_panel(short)
    assert truncated is False
    assert out == short

    long = ("段落内容。" * 4000)
    out, truncated = _truncate_description_for_panel(long)
    assert truncated is True
    assert len(out) <= META_DESC_MAX_PLAIN_CHARS
    assert META_DESC_TRUNCATED_HINT not in out


def test_collapse_steam_html_blank_gaps() -> None:
    raw = (
        "<p></p><p> </p><p>&nbsp;</p>"
        "hello<br/><br/><br/><br/><br/>world"
        "<p style='margin:2em 0;'></p>"
    )
    cleaned = _collapse_description_html_gaps(_format_description_rich_html(raw))
    assert cleaned.count("<br") <= 2
    assert "hello" in cleaned
    assert "world" in cleaned


def test_normal_description_shows_fully(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "library"
    description = "这是一段普通长度的 Mod 介绍，应完整显示。"
    folder = _seed(
        library,
        db,
        mid="88001",
        title="ShortMod",
        description=description,
    )
    panel = ModDetailPanel()
    _show(panel, folder, "88001")

    assert isinstance(panel.meta_desc_label, QLabel)
    assert not isinstance(panel.meta_desc_label, QTextBrowser)
    assert panel.meta_desc_label.maximumHeight() == META_DESC_MAX_HEIGHT_PX
    assert panel.meta_desc_frame.maximumHeight() == META_DESC_FRAME_MAX_HEIGHT_PX
    assert panel.meta_desc_frame.isVisible()
    assert description in panel.meta_desc_label.text()
    assert panel.meta_desc_truncated_hint.isHidden()
    assert panel.meta_source_line.isVisible()
    assert panel.meta_workspace_line.isVisible()
    assert "88001" in panel.meta_workspace_line.text()
    assert description not in panel.meta_rich_label.text()
    panel.close()


def test_short_description_stable_field_gaps(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "library"
    folder = _seed(
        library,
        db,
        mid="88010",
        title="GapShort",
        description="短介绍一行。",
    )
    panel = ModDetailPanel()
    _show(panel, folder, "88010")

    gap_name_desc = panel.meta_desc_frame.y() - (
        panel.meta_rich_label.y() + panel.meta_rich_label.height()
    )
    gap_desc_source = panel.meta_source_line.y() - (
        panel.meta_desc_frame.y() + panel.meta_desc_frame.height()
    )
    gap_source_ws = panel.meta_workspace_line.y() - (
        panel.meta_source_line.y() + panel.meta_source_line.height()
    )
    # Layout spacing is fixed; allow small Qt rounding / frame border delta.
    assert 4 <= gap_name_desc <= META_SECTION_SPACING_PX + 6
    assert 4 <= gap_desc_source <= META_SECTION_SPACING_PX + 6
    assert 4 <= gap_source_ws <= META_SECTION_SPACING_PX + 6
    assert panel.meta_desc_label.height() <= META_DESC_MAX_HEIGHT_PX
    assert (
        panel.meta_desc_label.sizePolicy().verticalPolicy()
        == QSizePolicy.Policy.Maximum
    )
    assert (
        panel.meta_source_line.sizePolicy().verticalPolicy()
        == QSizePolicy.Policy.Fixed
    )
    panel.close()


def test_mid_length_steam_description_not_char_truncated(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    """3000–5000 char Workshop copy must not hit the extreme char guard."""
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "library"
    description = ("Steam Workshop 中等偏长介绍段落，含更多说明。" * 120)
    assert 3000 <= len(description) <= 5000
    out, truncated = _truncate_description_for_panel(description)
    assert truncated is False
    assert out == description
    folder = _seed(
        library,
        db,
        mid="88012",
        title="MidLongMod",
        description=description,
    )
    panel = ModDetailPanel()
    _show(panel, folder, "88012")
    assert panel.meta_desc_frame.isVisible()
    assert panel.meta_desc_truncated_hint.isHidden()
    assert panel.meta_desc_label.height() <= META_DESC_MAX_HEIGHT_PX
    assert panel.meta_source_line.isVisible()
    assert panel.meta_workspace_line.isVisible()
    assert "88012" in panel.meta_workspace_line.text()
    panel.close()


def test_extreme_steam_html_description_capped(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "library"
    long_desc = "<p>" + ("超长 Steam HTML 描述段落。" * 800) + "</p>"
    assert len(long_desc) > META_DESC_MAX_PLAIN_CHARS
    folder = _seed(
        library,
        db,
        mid="88002",
        title="LongHtmlMod",
        description=long_desc,
    )
    panel = ModDetailPanel()
    _show(panel, folder, "88002")

    assert panel.meta_desc_frame.isVisible()
    assert panel.meta_desc_label.maximumHeight() == META_DESC_MAX_HEIGHT_PX
    assert panel.meta_desc_label.height() <= META_DESC_MAX_HEIGHT_PX
    assert panel.meta_desc_frame.height() <= META_DESC_FRAME_MAX_HEIGHT_PX
    assert panel.meta_source_line.isVisible()
    assert panel.meta_workspace_line.isVisible()
    assert "88002" in panel.meta_workspace_line.text()
    assert panel.meta_source_line.y() >= panel.meta_desc_frame.y()
    assert panel.meta_workspace_line.y() >= panel.meta_source_line.y()
    panel.close()


def test_many_blank_br_do_not_inflate_layout(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "library"
    description = (
        "<p></p>" * 40
        + "真实内容"
        + "<br/><br/><br/><br/><br/><br/><br/><br/>" * 20
        + "<p>&nbsp;</p>" * 30
        + "结尾"
    )
    folder = _seed(
        library,
        db,
        mid="88011",
        title="BlankHeavy",
        description=description,
    )
    panel = ModDetailPanel()
    _show(panel, folder, "88011")

    assert panel.meta_desc_frame.isVisible()
    assert panel.meta_desc_label.height() <= META_DESC_MAX_HEIGHT_PX
    assert panel.meta_desc_frame.height() <= META_DESC_FRAME_MAX_HEIGHT_PX
    assert "真实内容" in panel.meta_desc_label.text() or "结尾" in panel.meta_desc_label.text()
    assert panel.meta_source_line.isVisible()
    assert panel.meta_workspace_line.isVisible()
    panel.close()


def test_varied_description_lengths_share_height_rules(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "library"
    cases = [
        ("88021", "短。"),
        ("88022", "中等长度介绍，" * 20),
        ("88023", ("超长介绍段落。" * 500)),
    ]
    heights: list[int] = []
    gaps: list[tuple[int, int]] = []
    for mid, desc in cases:
        folder = _seed(
            library,
            db,
            mid=mid,
            title=f"Mod{mid}",
            description=desc,
        )
        panel = ModDetailPanel()
        _show(panel, folder, mid)
        assert panel.meta_desc_label.maximumHeight() == META_DESC_MAX_HEIGHT_PX
        assert panel.meta_desc_label.height() <= META_DESC_MAX_HEIGHT_PX
        assert panel.meta_desc_frame.height() <= META_DESC_FRAME_MAX_HEIGHT_PX
        assert panel.meta_source_line.isVisible()
        assert panel.meta_workspace_line.isVisible()
        heights.append(panel.meta_desc_label.height())
        gaps.append(
            (
                panel.meta_desc_frame.y()
                - (panel.meta_rich_label.y() + panel.meta_rich_label.height()),
                panel.meta_source_line.y()
                - (panel.meta_desc_frame.y() + panel.meta_desc_frame.height()),
            )
        )
        panel.close()
    # Gaps stay in the same fixed-spacing band across lengths.
    for g_name, g_src in gaps:
        assert 4 <= g_name <= META_SECTION_SPACING_PX + 6
        assert 4 <= g_src <= META_SECTION_SPACING_PX + 6
    assert max(heights) <= META_DESC_MAX_HEIGHT_PX


def test_short_description_shows_source_and_workspace(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "library"
    folder = _seed(
        library,
        db,
        mid="88004",
        title="FooterOnly",
        description="ok",
    )
    panel = ModDetailPanel()
    _show(panel, folder, "88004")
    assert panel.meta_source_line.isVisible()
    assert panel.meta_workspace_line.isVisible()
    assert "88004" in panel.meta_workspace_line.text()
    panel.close()
