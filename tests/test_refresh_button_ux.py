"""Detail panel refresh button UX — refresh must not use status banner."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME
from services.metadata_refresh import MetadataRefreshResult
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
    manager = DatabaseManager.instance(tmp_path / "refresh_ux.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(lib: Path, *, mid: str = "3413520661") -> Path:
    folder = lib / "Game" / f"Unknown_Mod_{mid}"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        '{"published_file_id":"%s","title":"Unknown_Mod_%s",'
        '"fetch_error":"timeout"}' % (mid, mid),
        encoding="utf-8",
    )
    return folder


def _banner_text(panel: ModDetailPanel) -> str:
    if not hasattr(panel, "_status_banner_body"):
        return ""
    return str(panel._status_banner_body.text() or "")


def test_refresh_button_is_labeled_push_button(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "lib"
    folder = _seed(lib)
    db.upsert_mod(
        ModMetadata(published_file_id="3413520661", title="Unknown_Mod_3413520661")
    )
    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()
    btn = panel.btn_refresh_mod
    assert isinstance(btn, QPushButton)
    assert "刷新信息" in (btn.text() or "")
    assert btn.objectName() == "detailRefreshButton"


def test_status_banner_hidden_when_opening_detail(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    """Opening Detail must not show the refresh status area."""
    lib = tmp_path / "lib"
    folder = _seed(lib)
    db.upsert_mod(
        ModMetadata(published_file_id="3413520661", title="Unknown_Mod_3413520661")
    )
    panel = ModDetailPanel()
    panel.show()
    panel.show_mod(folder)
    qapp.processEvents()
    assert hasattr(panel, "_status_banner")
    assert panel._status_banner.isHidden()
    body = _banner_text(panel)
    assert "已刷新" not in body
    assert "刷新完成" not in body
    assert "刷新失败" not in body


def test_refresh_does_not_show_status_banner(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    """Refresh finished must never write refresh copy into _status_banner."""
    lib = tmp_path / "lib"
    folder = _seed(lib)
    db.upsert_mod(
        ModMetadata(published_file_id="3413520661", title="Unknown_Mod_3413520661")
    )
    panel = ModDetailPanel()
    panel.show()
    panel.show_mod(folder)
    monkeypatch.setattr(panel, "show_mod", lambda *a, **k: None)
    qapp.processEvents()

    panel._set_refresh_button_state("running")
    assert "正在刷新" in panel.btn_refresh_mod.text()
    assert panel._status_banner.isHidden()

    panel._on_metadata_refresh_finished(
        MetadataRefreshResult(
            mod_id="3413520661",
            success=True,
            managed_path=folder,
            old_path=folder,
            title="Harborlife",
            message="已刷新本地状态",
        )
    )
    qapp.processEvents()
    assert "已更新" in panel.btn_refresh_mod.text()
    assert panel._status_banner.isHidden()
    body = _banner_text(panel)
    assert "已刷新" not in body
    assert "刷新完成" not in body
    assert "刷新失败" not in body

    panel._set_refresh_button_state("failure", detail="network timeout", restore_ms=50)
    qapp.processEvents()
    assert "刷新失败" in panel.btn_refresh_mod.text()
    assert panel._status_banner.isHidden()
    body = _banner_text(panel)
    assert "刷新失败" not in body
    assert "已刷新" not in body

    # Hard guard: even a direct refresh write is rejected.
    panel._show_status_banner("已刷新本地状态", tone="success")
    qapp.processEvents()
    assert panel._status_banner.isHidden()
    assert "已刷新" not in _banner_text(panel)


def test_refresh_button_state_machine(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    lib = tmp_path / "lib"
    folder = _seed(lib)
    db.upsert_mod(
        ModMetadata(published_file_id="3413520661", title="Unknown_Mod_3413520661")
    )
    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()

    panel._set_refresh_button_state("running")
    assert "正在刷新" in panel.btn_refresh_mod.text()
    assert panel.btn_refresh_mod.isEnabled() is False

    panel._set_refresh_button_state("success", restore_ms=50)
    assert "已更新" in panel.btn_refresh_mod.text()

    import time

    deadline = time.time() + 2.0
    while time.time() < deadline and "刷新信息" not in panel.btn_refresh_mod.text():
        qapp.processEvents()
        time.sleep(0.02)
    assert "刷新信息" in panel.btn_refresh_mod.text()
    assert panel.btn_refresh_mod.isEnabled() is True

    panel._set_refresh_button_state("failure", detail="network timeout", restore_ms=50)
    assert "刷新失败" in panel.btn_refresh_mod.text()
    assert "timeout" in (panel.btn_refresh_mod.toolTip() or "")
    assert panel._status_banner.isHidden()


def test_click_sets_running_immediately_and_blocks_duplicate(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    lib = tmp_path / "lib"
    folder = _seed(lib)
    db.upsert_mod(
        ModMetadata(published_file_id="3413520661", title="Unknown_Mod_3413520661")
    )
    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()

    monkeypatch.setattr(
        "services.info_sidecar.rescan_mod_folder",
        lambda *a, **k: None,
    )

    started = {"n": 0}

    class FakeWorker:
        def __init__(self, *a, **k):
            self.refresh_started = type("S", (), {"connect": lambda *x: None})()
            self.refresh_finished = type("S", (), {"connect": lambda *x: None})()
            self.refresh_failed = type("S", (), {"connect": lambda *x: None})()
            self.finished = type("S", (), {"connect": lambda *x: None})()

        def start(self):
            started["n"] += 1

        def isRunning(self):
            return True

    monkeypatch.setattr(
        "ui.metadata_refresh_thread.MetadataRefreshWorker",
        FakeWorker,
    )

    panel._on_refresh_mod()
    assert "正在刷新" in panel.btn_refresh_mod.text()
    assert started["n"] == 1
    panel._on_refresh_mod()
    assert started["n"] == 1
