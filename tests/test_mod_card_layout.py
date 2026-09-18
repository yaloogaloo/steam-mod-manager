"""Mod card layout: cover + title + status strip → identical card heights."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME
from services.mod_library_cache import ModCardData
from ui.mod_card import OFFLINE_MISSING_LABEL, ModCardWidget, _elide_to_lines


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _mod_dir(tmp_path: Path, name: str, *, offline: bool = True) -> Path:
    mod = tmp_path / "Game" / name
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True)
    if offline:
        (info / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    return mod


def _card_data(
    *,
    mid: str,
    path: Path,
    title: str,
    has_offline: bool,
    offline_status: str = "none",
    json_display_name: str = "",
) -> ModCardData:
    return ModCardData(
        id=mid,
        title=title,
        platform="steam",
        cover="",
        description="",
        tags="",
        size=0,
        updated_time=0.0,
        managed_path=str(path),
        game_folder="Game",
        steam_name=title,
        json_display_name=json_display_name,
        has_offline=has_offline,
        offline_status=offline_status,
    )


def test_cards_same_height_short_vs_long_display_name(
    qapp: QApplication, tmp_path: Path
) -> None:
    short = _mod_dir(tmp_path, "ShortMod")
    long = _mod_dir(tmp_path, "LongMod")

    long_display = (
        "用户自定义超长显示名称用于验证两行截断与卡片高度一致"
        "再追加更多文字确保超出两行"
    )

    card_a = ModCardWidget(
        short,
        ModMetadata(published_file_id="1", internal_id="36834fcf-3cbb-4ffe-8b78-be1921638bd4", mod_pk="1", title="Short", managed_path=str(short)),
        card_data=_card_data(mid="1", path=short, title="Short", has_offline=True, offline_status="archived"),
    )
    card_b = ModCardWidget(
        long,
        ModMetadata(
            published_file_id="2",
            internal_id="22222222-2222-4222-8222-222222222222",
            title="Original Steam Workshop Title That Is Quite Long",
            managed_path=str(long),
            json_display_name=long_display,
        ),
        card_data=_card_data(
            mid="2",
            path=long,
            title=long_display,
            has_offline=True,
            offline_status="archived",
            json_display_name=long_display,
        ),
    )

    assert card_a.height() == card_b.height()
    assert card_a.title_label.height() == card_b.title_label.height()
    assert card_a.status_strip.height() == card_b.status_strip.height()
    # Phase B: no Steam / Workshop ID body labels
    assert not hasattr(card_a, "steam_label")
    assert not hasattr(card_a, "meta_label")
    # Hover panel removed — title tooltip only.
    assert card_b.toolTip() == long_display
    assert "…" in card_b.title_label.text() or "..." in card_b.title_label.text() or len(
        card_b.title_label.text()
    ) < len(long_display)


def test_elide_to_lines_caps_at_two() -> None:
    font = QFont("Segoe UI", 10)
    text = "ABCDEFGHIJKLMNOPQRSTUVWXYZ " * 8
    out = _elide_to_lines(text, font, width=160, max_lines=2)
    assert out.endswith("…") or out.endswith("...")
    assert len(out) < len(text)


def test_offline_missing_keeps_same_height(
    qapp: QApplication, tmp_path: Path
) -> None:
    with_page = _mod_dir(tmp_path, "WithPage", offline=True)
    without = _mod_dir(tmp_path, "NoPage", offline=False)
    a = ModCardWidget(
        with_page,
        card_data=_card_data(
            mid="10",
            path=with_page,
            title="WithPage",
            has_offline=True,
            offline_status="archived",
        ),
    )
    b = ModCardWidget(
        without,
        card_data=_card_data(
            mid="11",
            path=without,
            title="NoPage",
            has_offline=False,
            offline_status="none",
        ),
    )
    assert a.height() == b.height()
    assert a.status_strip.height() == b.status_strip.height()
    assert a.offline_badge.isHidden()
    assert not b.offline_badge.isHidden()
    assert b.offline_badge.text() == OFFLINE_MISSING_LABEL
