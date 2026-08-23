"""Phase 11.5 — Mod card status badge is silent when healthy; conflict is unique."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.models import ModMetadata
from services.library_status import (
    CONTENT_BACKUP_INVALID,
    CONTENT_CONTENT_MISSING,
    CONTENT_FOLDER_MISSING,
    CONTENT_HEALTHY,
    CONTENT_IDENTITY_CONFLICT,
)
from ui.mod_card import ModCardWidget


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _folder(tmp_path: Path, name: str = "Mod") -> Path:
    folder = tmp_path / "Game" / name
    folder.mkdir(parents=True)
    return folder


def _data(**kwargs):
    base = dict(
        folder_absent=False,
        missing_content=False,
        content_status=CONTENT_HEALTHY,
        library_status="healthy",
        cover="",
        source_type="steam",
        steam_name="",
        display_name="",
        title="",
        json_display_name="",
        favorite=False,
        abandoned=False,
        offline_status="",
        deploy_status="",
        game_status="",
        platform="steam",
        conflict=False,
        invalid=False,
        is_invalid=False,
        enabled=True,
        category_tags="",
        relation_conflicts=0,
        relation_deps=0,
        has_offline=False,
        id="",
    )
    base.update(kwargs)
    if "source_type" in kwargs and "platform" not in kwargs:
        base["platform"] = kwargs["source_type"]
    if not base.get("id"):
        base["id"] = "11"
    return SimpleNamespace(**base)


def _card(tmp_path: Path, *, mid: str = "11", name: str = "Mod", data=None) -> ModCardWidget:
    folder = _folder(tmp_path, name)
    meta = ModMetadata(
        published_file_id=mid,
        title=name,
        managed_path=str(folder),
        source_type=getattr(data, "source_type", "steam") if data else "steam",
    )
    if data is not None and not getattr(data, "id", ""):
        data.id = mid
    return ModCardWidget(folder, meta, card_data=data)


def test_healthy_has_no_status_badge(qapp: QApplication, tmp_path: Path) -> None:
    card = _card(tmp_path, data=_data(content_status=CONTENT_HEALTHY, source_type="steam"))
    card.refresh_display()
    qapp.processEvents()
    assert card.missing_badge.isHidden()
    assert "✓" not in (card.missing_badge.text() or "")
    assert "正常" not in (card.missing_badge.text() or "")
    assert card.state_badge.isHidden() or "Conflict" not in (card.state_badge.text() or "")


def test_content_missing_shows_anomaly_not_healthy(
    qapp: QApplication, tmp_path: Path
) -> None:
    card = _card(
        tmp_path,
        data=_data(content_status=CONTENT_CONTENT_MISSING, missing_content=True),
    )
    card.refresh_display()
    qapp.processEvents()
    assert not card.missing_badge.isHidden()
    assert "文件缺失" in (card.missing_badge.text() or "")
    assert "✓" not in (card.missing_badge.text() or "")
    assert "正常" not in (card.missing_badge.text() or "")


def test_conflict_only_on_unified_status_badge(
    qapp: QApplication, tmp_path: Path
) -> None:
    card = _card(
        tmp_path,
        data=_data(
            content_status=CONTENT_IDENTITY_CONFLICT,
            conflict=True,
            source_type="steam",
        ),
    )
    card.refresh_display()
    qapp.processEvents()
    assert not card.missing_badge.isHidden()
    assert "冲突" in (card.missing_badge.text() or "")
    assert card.state_badge.isHidden() or not (card.state_badge.text() or "").strip()
    assert "Conflict" not in (card.state_badge.text() or "")


def test_conflict_does_not_cover_steam_source(
    qapp: QApplication, tmp_path: Path
) -> None:
    card = _card(
        tmp_path,
        data=_data(
            content_status=CONTENT_IDENTITY_CONFLICT,
            conflict=True,
            source_type="steam",
        ),
    )
    card.show()
    card.refresh_display()
    qapp.processEvents()
    assert "Steam" in (card.platform_badge.text() or "")
    assert not card.platform_badge.isHidden()
    plat = card.platform_badge.geometry()
    cover = card.cover_label.geometry()
    st = card.missing_badge.mapTo(card, card.missing_badge.rect().topLeft())
    assert plat.y() <= 8
    assert st.y() >= cover.bottom()
    assert plat.intersects(card.missing_badge.geometry()) is False


def test_category_left_source_right_status_on_bottom_strip(
    qapp: QApplication, tmp_path: Path
) -> None:
    card = _card(
        tmp_path,
        data=_data(
            content_status=CONTENT_CONTENT_MISSING,
            missing_content=True,
            source_type="steam",
            category_tags="地图",
        ),
    )
    card.show()
    card.refresh_display()
    qapp.processEvents()
    cover = card.cover_label.geometry()
    cat = card.category_badge.geometry()
    src = card.platform_badge.geometry()
    title = card.title_label.geometry()
    strip = card.status_strip.geometry()
    st = card.missing_badge.mapTo(card, card.missing_badge.rect().topLeft())
    assert not card.category_badge.isHidden()
    assert not card.platform_badge.isHidden()
    assert not card.missing_badge.isHidden()
    assert cat.x() <= 8
    assert cat.y() <= 8
    assert src.x() > cat.right()
    assert src.y() <= 8
    assert src.right() >= cover.width() - 12
    assert st.y() >= title.bottom()
    assert st.y() >= strip.y()
    assert card.status_container.parentWidget() is card.status_strip
    assert card.title_label.parentWidget() is card
    assert cat.intersects(src) is False


def test_layout_contract_source_right_category_left_status_under_source(
    qapp: QApplication, tmp_path: Path
) -> None:
    test_category_left_source_right_status_on_bottom_strip(qapp, tmp_path)


def test_healthy_keeps_source_top_right_no_status(
    qapp: QApplication, tmp_path: Path
) -> None:
    card = _card(
        tmp_path,
        data=_data(source_type="steam", category_tags="地图"),
    )
    card.refresh_display()
    qapp.processEvents()
    cover = card.cover_label.geometry()
    src = card.platform_badge.geometry()
    cat = card.category_badge.geometry()
    assert card.missing_badge.isHidden()
    assert src.x() > cat.right()
    assert src.y() <= 8
    assert src.right() >= cover.width() - 12
    assert cat.x() <= 8
    assert cat.y() <= 8


def test_healthy_steam_only_source(qapp: QApplication, tmp_path: Path) -> None:
    card = _card(tmp_path, data=_data(source_type="steam"))
    card.refresh_display()
    qapp.processEvents()
    assert "Steam" in (card.platform_badge.text() or "")
    assert card.missing_badge.isHidden()


def test_healthy_nexus_only_source(qapp: QApplication, tmp_path: Path) -> None:
    card = _card(
        tmp_path, name="NexusMod", data=_data(source_type="nexus")
    )
    card.refresh_display()
    qapp.processEvents()
    assert "Nexus" in (card.platform_badge.text() or "")
    assert card.missing_badge.isHidden()
    assert "✓" not in (card.missing_badge.text() or "")


def test_other_anomalies_keep_existing_labels(
    qapp: QApplication, tmp_path: Path
) -> None:
    folder = _card(
        tmp_path,
        name="Gone",
        data=_data(content_status=CONTENT_FOLDER_MISSING, folder_absent=True),
    )
    folder.refresh_display()
    qapp.processEvents()
    assert "目录缺失" in (folder.missing_badge.text() or "")
    assert not folder.missing_badge.isHidden()

    backup = _card(
        tmp_path,
        name="BadBackup",
        mid="12",
        data=_data(content_status=CONTENT_BACKUP_INVALID),
    )
    backup.refresh_display()
    qapp.processEvents()
    assert "备份损坏" in (backup.missing_badge.text() or "")
    assert not backup.missing_badge.isHidden()


def test_invalid_and_favorite_render_together_in_footer(
    qapp: QApplication, tmp_path: Path
) -> None:
    card = _card(
        tmp_path,
        data=_data(invalid=True, is_invalid=True, favorite=True, source_type="steam"),
    )
    card.show()
    card.refresh_display()
    qapp.processEvents()
    assert card.state_badge.isHidden()
    assert not card.invalid_badge.isHidden()
    assert "失效" in (card.invalid_badge.text() or "")
    assert not card.favorite_badge.isHidden()
    assert "★" in (card.favorite_badge.text() or "")
    assert "★" not in (card.title_label.text() or "")
    cover = card.cover_label.geometry()
    inv = card.invalid_badge.mapTo(card, card.invalid_badge.rect().topLeft())
    fav = card.favorite_badge.mapTo(card, card.favorite_badge.rect().topLeft())
    assert inv.y() >= cover.bottom()
    assert fav.y() >= cover.bottom()


def test_favorite_abandoned_conflict_do_not_override(
    qapp: QApplication, tmp_path: Path
) -> None:
    card = _card(
        tmp_path,
        data=_data(
            favorite=True,
            abandoned=True,
            conflict=True,
            content_status=CONTENT_IDENTITY_CONFLICT,
            source_type="steam",
        ),
    )
    card.show()
    card.refresh_display()
    qapp.processEvents()
    assert not card.missing_badge.isHidden()
    assert "冲突" in (card.missing_badge.text() or "")
    assert not card.abandoned_badge.isHidden()
    assert "停更" in (card.abandoned_badge.text() or "")
    assert not card.favorite_badge.isHidden()
    assert card.state_badge.isHidden()


def test_disabled_renders_in_footer_not_cover(
    qapp: QApplication, tmp_path: Path
) -> None:
    card = _card(
        tmp_path,
        data=_data(enabled=False, source_type="steam", category_tags="地图"),
    )
    card.show()
    card.refresh_display()
    qapp.processEvents()
    assert card.state_badge.isHidden()
    assert not card.disabled_badge.isHidden()
    assert "停用" in (card.disabled_badge.text() or "")
    cover = card.cover_label.geometry()
    pos = card.disabled_badge.mapTo(card, card.disabled_badge.rect().topLeft())
    assert pos.y() >= cover.bottom()
    cat = card.category_badge.geometry()
    src = card.platform_badge.geometry()
    assert cat.x() <= 8
    assert src.y() <= 8


def test_status_is_not_in_title_layout(
    qapp: QApplication, tmp_path: Path
) -> None:
    card = _card(
        tmp_path,
        data=_data(content_status=CONTENT_CONTENT_MISSING, missing_content=True),
    )
    card.show()
    card.refresh_display()
    qapp.processEvents()
    title_parent = card.title_label.parentWidget()
    assert title_parent is card
    title_layout = card.title_label.parentWidget().layout()
    managed = []
    for i in range(title_layout.count()):
        item = title_layout.itemAt(i)
        w = item.widget() if item is not None else None
        if w is not None:
            managed.append(w)
    assert card.status_container not in managed
    assert card.status_container.parentWidget() is card.status_strip
    strip = card.status_strip.layout()
    strip_widgets = []
    for i in range(strip.count()):
        item = strip.itemAt(i)
        w = item.widget() if item is not None else None
        if w is not None:
            strip_widgets.append(w)
    assert card.offline_badge in strip_widgets
    assert card.status_container in strip_widgets


def test_status_aligns_with_offline_on_bottom_right(
    qapp: QApplication, tmp_path: Path
) -> None:
    card = _card(
        tmp_path,
        data=_data(
            content_status=CONTENT_IDENTITY_CONFLICT,
            conflict=True,
            has_offline=False,
            source_type="steam",
        ),
    )
    card.show()
    card.refresh_display()
    qapp.processEvents()
    assert not card.offline_badge.isHidden()
    assert not card.missing_badge.isHidden()
    off = card.offline_badge.mapTo(card, card.offline_badge.rect().center())
    st = card.status_container.mapTo(card, card.status_container.rect().center())
    assert abs(off.y() - st.y()) <= 4
    strip_right = card.status_strip.mapTo(
        card, card.status_strip.rect().topRight()
    ).x()
    cont_right = card.status_container.mapTo(
        card, card.status_container.rect().topRight()
    ).x()
    assert abs(strip_right - cont_right) <= 4
    off_right = card.offline_badge.mapTo(
        card, card.offline_badge.rect().topRight()
    ).x()
    cont_left = card.status_container.mapTo(
        card, card.status_container.rect().topLeft()
    ).x()
    assert off_right <= cont_left


def test_status_does_not_shrink_title_width(
    qapp: QApplication, tmp_path: Path
) -> None:
    plain = _card(tmp_path, name="Plain", mid="21", data=_data(id="21"))
    busy = _card(
        tmp_path,
        name="Busy",
        mid="22",
        data=_data(
            id="22",
            content_status=CONTENT_IDENTITY_CONFLICT,
            conflict=True,
            invalid=True,
            is_invalid=True,
            favorite=True,
            abandoned=True,
        ),
    )
    plain.show()
    busy.show()
    plain.refresh_display()
    busy.refresh_display()
    qapp.processEvents()
    assert plain.title_label.width() == busy.title_label.width()
    assert busy.title_label.width() >= 150
    assert "★" not in (busy.title_label.text() or "")
