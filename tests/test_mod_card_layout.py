"""Mod card layout: fixed bands → identical card heights."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME
from ui.mod_card import ModCardWidget, _elide_to_lines


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


def test_cards_same_height_short_vs_long_display_name(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    short = _mod_dir(tmp_path, "ShortMod")
    long = _mod_dir(tmp_path, "LongMod")

    short_info = MagicMock(
        steam_name="Short",
        display_name="Short",
        user_display_name="",
        favorite=False,
    )
    long_info = MagicMock(
        steam_name="Original Steam Workshop Title That Is Quite Long",
        display_name=(
            "用户自定义超长显示名称用于验证两行截断与卡片高度一致"
            "再追加更多文字确保超出两行"
        ),
        user_display_name="用户自定义超长显示名称用于验证两行截断与卡片高度一致再追加更多文字确保超出两行",
        favorite=True,
    )

    def fake_get_info(mod_id: str):
        return {"1": short_info, "2": long_info}.get(str(mod_id))

    db = MagicMock()
    db.get_mod_display_info.side_effect = fake_get_info
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)

    card_a = ModCardWidget(
        short,
        ModMetadata(published_file_id="1", title="Short", managed_path=str(short)),
    )
    card_b = ModCardWidget(
        long,
        ModMetadata(
            published_file_id="2",
            title="Original Steam Workshop Title That Is Quite Long",
            managed_path=str(long),
        ),
    )

    assert card_a.height() == card_b.height()
    assert card_a.title_label.height() == card_b.title_label.height()
    assert card_a.steam_label.height() == card_b.steam_label.height()
    assert card_a.meta_label.height() == card_b.meta_label.height()
    assert card_a.offline_label.height() == card_b.offline_label.height()
    # Steam Name row always present (elided + tooltip)
    assert card_a.steam_label.text().startswith("Steam:")
    assert card_b.steam_label.text().startswith("Steam:")
    assert card_a.steam_label.toolTip() == "Short"
    assert "…" in card_b.title_label.text() or "..." in card_b.title_label.text() or len(
        card_b.title_label.text()
    ) < len(long_info.display_name)


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
    a = ModCardWidget(with_page)
    b = ModCardWidget(without)
    assert a.height() == b.height()
    assert a.offline_label.height() == b.offline_label.height()
