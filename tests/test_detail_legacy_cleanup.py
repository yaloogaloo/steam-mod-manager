"""Detail panel legacy surface cleanup — no layout / style-system rewrite."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QWidget

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME
from tests.helpers.identity import create_steam_test_mod
from services.metadata_refresh import MetadataRefreshResult
from ui.mod_detail_panel import ModDetailPanel
from ui import styles as ui_styles
import ui.mod_detail_panel as mdp


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "detail_legacy_cleanup.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed_mod(db: DatabaseManager, lib: Path, *, mid: str = "3413520661") -> Path:
    folder = lib / "Game" / f"Mod_{mid}"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        f'{{"published_file_id":"{mid}","title":"CleanupMod","internal_id":"{mid}"}}',
        encoding="utf-8",
    )
    (folder / "payload.txt").write_text("x", encoding="utf-8")
    create_steam_test_mod(db, external_id=mid, title="CleanupMod")
    return folder


def test_detail_has_no_legacy_host_in_scroll_composition(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder = _seed_mod(db, tmp_path / "lib")
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id="3413520661")
    qapp.processEvents()

    assert not hasattr(panel, "_legacy_host")
    assert not hasattr(panel, "_header_actions")
    assert not hasattr(mdp, "_ElidedLabel")

    host = getattr(panel, "_offscreen_host", None)
    assert host is not None
    assert host.isHidden()
    assert host.objectName() == "detailOffscreenHost"

    body = panel._view_scroll.widget()
    assert body is not None
    assert host.parentWidget() is panel
    # Off-screen host must not sit inside the scroll body composition.
    assert host.parentWidget() is not body
    assert body.findChild(QWidget, "detailOffscreenHost") is None


def test_refresh_success_uses_op_status_not_status_banner(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    folder = _seed_mod(db, tmp_path / "lib")
    panel = ModDetailPanel()
    panel.show()
    panel.show_mod(folder, mod_id="3413520661")
    monkeypatch.setattr(panel, "show_mod", lambda *a, **k: None)
    qapp.processEvents()

    assert panel._status_banner.isHidden()

    panel._on_metadata_refresh_finished(
        MetadataRefreshResult(
            mod_id="3413520661",
            success=True,
            managed_path=folder,
            old_path=folder,
            title="CleanupMod",
            message="refresh ok",
        )
    )
    qapp.processEvents()

    assert "刷新完成" in (panel.op_status_label.text() or "")
    assert panel._status_banner.isHidden()
    assert panel._status_banner.isVisible() is False

    panel._show_status_banner("刷新成功", tone="success")
    assert panel._status_banner.isHidden()


def test_status_banner_error_path_still_available(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder = _seed_mod(db, tmp_path / "lib")
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id="3413520661")
    qapp.processEvents()

    panel._show_error_banner("部署失败：disk full")
    assert not panel._status_banner.isHidden()
    assert "disk full" in (panel._status_banner_body.text() or "")
    assert panel._status_banner.property("tone") == "error"


def test_metadata_layout_order_unchanged(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    folder = _seed_mod(db, tmp_path / "lib")
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id="3413520661")
    qapp.processEvents()

    # Canonical visible metadata stack (Name → Description Frame → Source → Workspace).
    assert hasattr(panel, "meta_rich_label")
    assert hasattr(panel, "meta_desc_frame")
    assert hasattr(panel, "meta_source_line")
    assert hasattr(panel, "meta_workspace_line")
    assert hasattr(panel, "op_status_label")

    body = panel.meta_rich_label.parentWidget()
    assert body is not None
    layout = body.layout()
    assert layout is not None

    ordered: list[QWidget] = []
    for i in range(layout.count()):
        item = layout.itemAt(i)
        w = item.widget() if item is not None else None
        if w is not None:
            ordered.append(w)

    assert panel.meta_rich_label in ordered
    assert panel.meta_desc_frame in ordered
    assert panel.meta_source_line in ordered
    assert panel.meta_workspace_line in ordered

    i_name = ordered.index(panel.meta_rich_label)
    i_desc = ordered.index(panel.meta_desc_frame)
    i_src = ordered.index(panel.meta_source_line)
    i_ws = ordered.index(panel.meta_workspace_line)
    assert i_name < i_desc < i_src < i_ws


def test_orphan_detail_css_removed() -> None:
    css = ui_styles.APP_STYLE + "\n" + ui_styles.PANEL_STYLE
    assert "QPushButton#detailButton" not in css
    assert "QWidget#detailHeaderActions" not in css
    # Error banner styles remain for deploy failures.
    assert "QFrame#detailStatusBanner" in css
    assert "QLabel#detailOpStatus" in css
