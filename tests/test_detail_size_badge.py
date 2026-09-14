"""Header size badge mirrors platform badge; size removed from rich metadata."""

from __future__ import annotations

from pathlib import Path
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from services.size_observation import (
    reset_size_observation,
    wait_for_size_idle,
)
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
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
    manager = DatabaseManager.instance(tmp_path / "size_badge.db")
    manager.upsert_game(GameInfo(app_id=1, name="Game", folder_name="Game"))
    reset_size_observation()
    yield manager
    reset_size_observation()
    DatabaseManager.reset_instance()


def _seed_sized(
    db: DatabaseManager,
    tmp_path: Path,
    *,
    workshop_id: str,
    title: str,
    payload: bytes | None = b"x" * 4096,
) -> tuple[Path, str]:
    """Create Steam entity + folder. Returns (folder, mods.mod_id PK)."""
    created = create_steam_test_mod(
        db, external_id=workshop_id, title=title, app_id=1, game_name="Game"
    )
    pk = str(created.mod_id)
    folder = tmp_path / "Game" / title
    folder.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        (folder / "payload.pak").write_bytes(payload)
    prove_managed_folder(
        db, folder, handle=pk, title=title, app_id=1, game_name="Game"
    )
    return folder, pk


def _wait_size_badge(qapp: QApplication, panel: ModDetailPanel) -> None:
    wait_for_size_idle(3.0)
    for _ in range(40):
        qapp.processEvents()
        text = panel.size_badge.text() or ""
        if text and text != "计算中…" and ("B" in text or "KB" in text or "MB" in text):
            return
        time.sleep(0.02)


def test_size_badge_next_to_platform_and_not_in_rich_html(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed_sized(db, tmp_path, workshop_id="88", title="SizedMod")

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=pk)
    _wait_size_badge(qapp, panel)

    assert hasattr(panel, "size_badge")
    assert not panel.size_badge.isHidden()
    assert "KB" in panel.size_badge.text() or "B" in panel.size_badge.text()
    assert panel.size_badge.objectName() == panel.header_platform_badge.objectName()
    assert "background-color" in panel.size_badge.styleSheet()
    assert panel.size_badge.styleSheet() == panel.header_platform_badge.styleSheet()
    assert "大小" not in (panel.meta_rich_label.text() or "")
    assert not hasattr(panel, "content_status_badge")


def _metadata_row_widgets(panel: ModDetailPanel) -> list:
    from PySide6.QtWidgets import QHBoxLayout

    host = panel.header_platform_badge.parentWidget()
    vbox = host.layout()
    row = None
    for i in range(vbox.count()):
        item = vbox.itemAt(i)
        lay = item.layout() if item is not None else None
        if isinstance(lay, QHBoxLayout):
            row = lay
            break
    assert row is not None
    widgets = []
    for i in range(row.count()):
        item = row.itemAt(i)
        widget = item.widget() if item is not None else None
        if widget is not None:
            widgets.append(widget)
    return widgets


def _metadata_row_visible_texts(panel: ModDetailPanel) -> list[str]:
    from PySide6.QtWidgets import QHBoxLayout, QLabel

    host = panel.header_platform_badge.parentWidget()
    vbox = host.layout()
    row = None
    for i in range(vbox.count()):
        item = vbox.itemAt(i)
        lay = item.layout() if item is not None else None
        if isinstance(lay, QHBoxLayout):
            row = lay
            break
    assert row is not None
    texts: list[str] = []
    for i in range(row.count()):
        item = row.itemAt(i)
        widget = item.widget() if item is not None else None
        if isinstance(widget, QLabel) and not widget.isHidden():
            texts.append(widget.text() or "")
    return texts


def test_detail_header_metadata_row_contains_source_and_size_only(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed_sized(db, tmp_path, workshop_id="88", title="SizedMod")
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=pk)
    _wait_size_badge(qapp, panel)
    assert not hasattr(panel, "content_status_badge")
    assert not panel.header_platform_badge.isHidden()
    assert not panel.size_badge.isHidden()
    joined = " ".join(_metadata_row_visible_texts(panel))
    assert "Backup" not in joined
    assert "✓ 正常" not in joined
    assert "文件缺失" not in joined
    assert "冲突" not in joined


def test_detail_header_healthy_has_no_content_status_badge(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed_sized(db, tmp_path, workshop_id="89", title="HealthyMod", payload=b"x" * 2048)
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=pk)
    _wait_size_badge(qapp, panel)
    assert not hasattr(panel, "content_status_badge")
    joined = " ".join(_metadata_row_visible_texts(panel))
    assert "✓" not in joined
    assert "正常" not in joined
    assert "文件缺失" not in joined
    assert "冲突" not in joined
    assert not panel.header_platform_badge.isHidden()
    assert not panel.size_badge.isHidden()


def test_detail_header_missing_does_not_add_content_status_badge(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    from services.file_ops import apply_missing_content_marker
    from services.library_status import CONTENT_CONTENT_MISSING

    folder, pk = _seed_sized(db, tmp_path, workshop_id="90", title="MissingMod", payload=None)
    apply_missing_content_marker(folder)
    db.update_mod_content_status(
        pk,
        content_status=CONTENT_CONTENT_MISSING,
        library_status="content_missing",
        folder_present=True,
        last_known_path=str(folder),
    )
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=pk)
    _wait_size_badge(qapp, panel)
    assert not hasattr(panel, "content_status_badge")
    joined = " ".join(_metadata_row_visible_texts(panel))
    assert "文件缺失" not in joined
    assert "✓ 正常" not in joined
    assert not panel.header_platform_badge.isHidden()


def test_detail_backup_badge_is_exception_only(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed_sized(db, tmp_path, workshop_id="91", title="BackupMod", payload=b"x" * 1024)
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=pk)
    _wait_size_badge(qapp, panel)
    assert hasattr(panel, "backup_status_badge")
    assert panel.backup_status_badge.isHidden()

    db.update_mod_backup_status(pk, status="invalid")
    panel.show_mod(folder, mod_id=pk)
    _wait_size_badge(qapp, panel)
    assert panel.backup_status_badge.isHidden()
    joined = " ".join(_metadata_row_visible_texts(panel))
    assert "Backup" not in joined
    assert panel.backup_status_badge not in _metadata_row_widgets(panel)
    assert not hasattr(panel, "content_status_badge")
    assert not panel.header_platform_badge.isHidden()
    assert not panel.size_badge.isHidden()


def test_detail_header_no_content_status_badge_when_healthy(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    test_detail_header_healthy_has_no_content_status_badge(qapp, tmp_path, db)


def test_detail_header_favorite_is_below_metadata_row(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed_sized(db, tmp_path, workshop_id="92", title="FavMod", payload=b"x" * 1024)
    db.update_mod_user_metadata(pk, {"favorite": True})
    panel = ModDetailPanel()
    panel.resize(480, 720)
    panel.show()
    panel.show_mod(folder, mod_id=pk)
    _wait_size_badge(qapp, panel)

    assert panel.view_favorite.objectName() == "detailFavoriteLabel"
    assert "收藏" in (panel.view_favorite.text() or "")
    assert not panel.view_favorite.isHidden()
    assert panel.view_favorite.parentWidget() is panel.header_platform_badge.parentWidget()
    assert "收藏" not in " ".join(_metadata_row_visible_texts(panel))
    assert panel.view_favorite.y() >= (
        panel.header_platform_badge.y() + panel.header_platform_badge.height()
    )
    assert not panel.header_platform_badge.isHidden()
    assert not panel.size_badge.isHidden()
    assert "✓" not in (panel.view_favorite.text() or "")
    from ui.styles import ACCENT_WARNING

    assert ACCENT_WARNING.lower() == "#d4a017"


def _assert_header_source_size_only(panel: ModDetailPanel) -> None:
    widgets = _metadata_row_widgets(panel)
    assert panel.backup_status_badge not in widgets
    assert panel.backup_status_badge.isHidden()
    assert "Backup" not in " ".join(_metadata_row_visible_texts(panel))
    assert not panel.header_platform_badge.isHidden()
    assert not panel.size_badge.isHidden()
    assert panel.header_platform_badge in widgets
    assert panel.size_badge in widgets


def test_detail_header_backup_invalid_stays_out_of_metadata_row(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed_sized(db, tmp_path, workshop_id="93", title="BakInvalid", payload=b"x" * 1024)
    db.update_mod_backup_status(pk, status="invalid")
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=pk)
    _wait_size_badge(qapp, panel)
    _assert_header_source_size_only(panel)


def test_detail_header_backup_partial_missing_complete_stay_out_of_row(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed_sized(db, tmp_path, workshop_id="94", title="BakOther", payload=b"x" * 1024)
    panel = ModDetailPanel()
    for status in ("partial", "missing", "complete", "invalid"):
        db.update_mod_backup_status(pk, status=status)
        panel.show_mod(folder, mod_id=pk)
        _wait_size_badge(qapp, panel)
        _assert_header_source_size_only(panel)


def test_detail_header_backup_layout_stable_across_refresh(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder, pk = _seed_sized(db, tmp_path, workshop_id="95", title="BakRefresh", payload=b"x" * 1024)
    db.update_mod_backup_status(pk, status="invalid")
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=pk)
    _wait_size_badge(qapp, panel)
    _assert_header_source_size_only(panel)
    db.update_mod_backup_status(pk, status="complete")
    panel.show_mod(folder, mod_id=pk)
    _wait_size_badge(qapp, panel)
    _assert_header_source_size_only(panel)
