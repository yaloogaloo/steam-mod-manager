"""Generate UI acceptance screenshots for toolbar / detail layout fixes.

Run:
  QT_QPA_PLATFORM=offscreen python3 scripts/ui_layout_screenshots.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from ui.library_view import ModLibraryView
from ui.styles import APP_STYLE


OUT = Path("/opt/cursor/artifacts/screenshots")
LONG_NAME = (
    "Super Long Display Name For Crowding Test — "
    "Optional Pack Plus Hotfix Patch Edition 2026"
)


def _write_mod(library: Path, *, mid: str, title: str, game: str = "Palworld") -> Path:
    mod = library / game / title
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (mod / "mod.dll").write_bytes(b"MZ")
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "{title}",\n'
        '  "app_id": 1623730,\n'
        f'  "game_name": "{game}"\n'
        "}\n",
        encoding="utf-8",
    )
    return mod


def _grab(widget, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pix = widget.grab()
    ok = pix.save(str(path), "PNG")
    if not ok:
        raise RuntimeError(f"failed to save {path}")
    print(f"wrote {path} ({path.stat().st_size} bytes)")


def main() -> int:
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(APP_STYLE)

    work = ROOT / "data" / "_ui_shot_lib"
    if work.exists():
        import shutil

        shutil.rmtree(work)
    library = work / "library"
    library.mkdir(parents=True)

    db_path = work / "shot.db"
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(db_path)
    db.upsert_game(GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld"))
    db.upsert_game(GameInfo(app_id=72850, name="Skyrim", folder_name="Skyrim"))

    # 100+ mods for dense library shot
    for i in range(110):
        mid = str(50000 + i)
        title = f"ModPack_{i:03d}"
        if i == 7:
            title = LONG_NAME
        game = "Palworld" if i % 5 else "Skyrim"
        _write_mod(library, mid=mid, title=title, game=game)
        db.upsert_mod(
            ModMetadata(
                published_file_id=mid,
                title=title if i != 7 else "Steam Original Short",
                app_id=1623730 if game == "Palworld" else 72850,
                game_name=game,
            )
        )
        if i == 7:
            db.update_mod_user_metadata(mid, {"display_name": LONG_NAME})

    view = ModLibraryView()
    view.setStyleSheet(APP_STYLE)
    view.set_target_root(str(library))
    # Patch get_db used by cards/panel
    import ui.library_view as lv
    import ui.mod_card as mc
    import ui.mod_detail_panel as mdp

    lv.get_db = lambda: db  # type: ignore[assignment]
    mc.get_db = lambda: db  # type: ignore[assignment]
    mdp.get_db = lambda: db  # type: ignore[assignment]

    view.refresh()
    app.processEvents()

    # 1) Wide window — 100+ mods + two-row toolbar
    view.resize(1280, 820)
    view.show()
    app.processEvents()
    QTimer.singleShot(0, lambda: None)
    app.processEvents()
    _grab(view, OUT / "ui_toolbar_two_rows_wide.png")

    # 2) Narrow window — chips/combos must not crush
    view.resize(980, 720)
    app.processEvents()
    _grab(view, OUT / "ui_toolbar_narrow.png")

    # 3) Select long-name mod — detail footer actions readable
    long_card = None
    for card in view._cards:
        if LONG_NAME[:20] in (card.title_label.text() or "") or card._mod_id() == "50007":
            long_card = card
            break
    if long_card is None and view._cards:
        # fallback: pick by id via metadata
        for card in view._cards:
            if card._mod_id() == "50007":
                long_card = card
                break
    if long_card is not None:
        view._select_card(long_card, show_panel=True)
        app.processEvents()
    _grab(view, OUT / "ui_detail_long_name.png")

    # Crop-ish focus: detail panel alone
    _grab(view.detail_panel, OUT / "ui_detail_panel_actions.png")

    # 4) Explicit toolbar region via center column (full view already covers)
    _grab(view, OUT / "ui_toolbar_two_rows.png")

    # Assert layout invariants for the script itself
    assert view.category_combo is not None
    assert view.sort_combo is not None
    assert view.detail_panel.btn_folder.minimumWidth() >= 96
    assert view.detail_panel.btn_download_offline.minimumWidth() >= 120
    for btn in view._platform_buttons.values():
        pol = btn.sizePolicy().horizontalPolicy()
        from PySide6.QtWidgets import QSizePolicy

        assert pol == QSizePolicy.Policy.Minimum

    print("game_list rebuild count:", getattr(view, "_game_list_rebuild_count", 0))
    print("game_list items:", view.game_list.count())
    DatabaseManager.reset_instance()
    view.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
