"""Render Game Deploy settings page for visual verification."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from core.db_manager import DatabaseManager
from ui.game_deploy_view import GameDeployView
from ui.styles import APP_STYLE


def main() -> Path:
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)

    out_dir = ROOT / "tests" / "_artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "game_deploy_view.png"

    db_path = out_dir / "_deploy_ui_fixture.db"
    if db_path.exists():
        db_path.unlink()
    DatabaseManager.reset_instance()
    db = DatabaseManager(db_path)
    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=r"D:\SteamLibrary\steamapps\common\Palworld",
        mod_path=r"D:\SteamLibrary\steamapps\common\Palworld\Pal\Binaries\Win64\Mods",
    )

    host = QWidget()
    host.setStyleSheet("background: #121820;")
    layout = QHBoxLayout(host)
    layout.setContentsMargins(16, 16, 16, 16)

    # Fake nav strip
    nav = QVBoxLayout()
    brand = QLabel("Steam Mod\nManager")
    brand.setObjectName("titleLabel")
    nav.addWidget(brand)
    for name, active in (
        ("同步中心", False),
        ("Mod 库", False),
        ("游戏部署", True),
    ):
        item = QLabel(name)
        item.setStyleSheet(
            "padding: 12px 14px; border-radius: 8px; font-weight: 600; "
            + ("background:#2a475e; color:#66c0f4;" if active else "color:#c7d5e0;")
        )
        nav.addWidget(item)
    nav.addStretch()
    layout.addLayout(nav)

    view = GameDeployView(db=db)
    view.refresh()
    # Select Palworld in combo so picker matches form fields
    idx = view.game_combo.findData(1623730)
    if idx >= 0:
        view.game_combo.setCurrentIndex(idx)
    layout.addWidget(view, stretch=1)

    host.resize(980, 720)
    host.show()
    app.processEvents()
    host.grab().save(str(out_path))
    print(f"saved {out_path}")
    db.close()
    DatabaseManager.reset_instance()
    return out_path


if __name__ == "__main__":
    main()
