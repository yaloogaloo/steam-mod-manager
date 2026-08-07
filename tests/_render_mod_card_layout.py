"""Render short vs long Mod cards for visual layout verification."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME
from ui.mod_card import ModCardWidget
from ui.styles import APP_STYLE


def main() -> Path:
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)

    out_dir = ROOT / "tests" / "_artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "mod_card_layout_compare.png"

    tmp = out_dir / "_card_fixture"
    short_path = tmp / "Game" / "Short"
    long_path = tmp / "Game" / "Long"
    for p in (short_path, long_path):
        info = p / INFO_DIR_NAME
        info.mkdir(parents=True, exist_ok=True)
        (info / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    short_info = MagicMock(
        steam_name="Compact Mod",
        display_name="Compact Mod",
        user_display_name="",
        favorite=False,
    )
    long_info = MagicMock(
        steam_name="Very Long Original Steam Workshop Title For Alignment Check",
        display_name=(
            "自定义超长显示名称验证两行省略与按钮底对齐"
            "——继续加长直到明显超出两行区域"
        ),
        user_display_name=(
            "自定义超长显示名称验证两行省略与按钮底对齐"
            "——继续加长直到明显超出两行区域"
        ),
        favorite=True,
    )

    def fake_get(mod_id: str):
        return {"100": short_info, "200": long_info}.get(str(mod_id))

    db = MagicMock()
    db.get_mod_display_info.side_effect = fake_get

    import ui.mod_card as mod_card_mod

    mod_card_mod.get_db = lambda: db  # type: ignore[assignment]

    host = QWidget()
    host.setStyleSheet("background: #1b2838;")
    root = QVBoxLayout(host)
    root.setContentsMargins(24, 24, 24, 24)
    root.setSpacing(16)

    caption = QLabel("左：未改名  |  右：自定义超长 display_name（高度应一致）")
    caption.setStyleSheet("color: #c7d5e0; font-size: 13px;")
    root.addWidget(caption)

    row = QHBoxLayout()
    row.setSpacing(20)
    card_short = ModCardWidget(
        short_path,
        ModMetadata(
            published_file_id="100",
            title="Compact Mod",
            managed_path=str(short_path),
        ),
    )
    card_long = ModCardWidget(
        long_path,
        ModMetadata(
            published_file_id="200",
            title="Very Long Original Steam Workshop Title For Alignment Check",
            managed_path=str(long_path),
        ),
    )
    row.addWidget(card_short)
    row.addWidget(card_long)
    row.addStretch()
    root.addLayout(row)

    meta = QLabel(
        f"heights: short={card_short.height()}px  long={card_long.height()}px  "
        f"match={card_short.height() == card_long.height()}"
    )
    meta.setStyleSheet("color: #66c0f4; font-size: 12px;")
    root.addWidget(meta)
    root.addStretch()

    host.resize(520, 420)
    host.show()
    app.processEvents()

    pix = host.grab()
    pix.save(str(out_path))
    print(f"saved {out_path}")
    print(f"height_short={card_short.height()} height_long={card_long.height()}")
    return out_path


if __name__ == "__main__":
    main()
