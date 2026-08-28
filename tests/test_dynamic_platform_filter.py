"""Dynamic platform filter chips derived from loaded Mod list."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from core.mod_platform import PLATFORM_MODIO, PLATFORM_NEXUS
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from ui.library_view import ModLibraryView
from ui.library_query import FILTER_PLATFORM_NEXUS


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "dyn_platform.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_dynamic_platform_filter_bar(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(916440, name="Anno 1800", mod_path="")
    for mid, title, plat in (
        ("9001", "NexusMod", PLATFORM_NEXUS),
        ("9002", "ModioMod", PLATFORM_MODIO),
    ):
        mod = library / "Anno 1800" / title
        info = mod / INFO_DIR_NAME
        info.mkdir(parents=True)
        (mod / "a.txt").write_text("x", encoding="utf-8")
        (info / METADATA_FILENAME).write_text(
            f'{{"published_file_id": "{mid}", "title": "{title}", "app_id": 916440}}',
            encoding="utf-8",
        )
        db.upsert_mod(ModMetadata(published_file_id=mid, title=title, app_id=916440))
        db.update_mod_platform_info(
            mid,
            platform=plat,
            source_url="",
            external_id=mid,
        )

    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()

    labels = [btn.text() for btn in view._platform_buttons.values()]
    assert "全部" in labels
    assert "Nexus" in labels
    assert "Mod.io" in labels or "mod.io" in labels
    assert "Steam" not in labels
    assert "GitHub" not in labels

    nexus_btn = view._platform_buttons.get(FILTER_PLATFORM_NEXUS) or view._platform_buttons.get(
        PLATFORM_NEXUS
    )
    assert nexus_btn is not None
    nexus_btn.setChecked(True)
    visible = view._visible_cards()
    assert len(visible) == 1
    assert visible[0]._mod_id() == "9001"
