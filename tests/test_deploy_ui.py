"""Phase 4: deploy UI — DeployWorker + ModDetailPanel / library wiring."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QThread
from PySide6.QtWidgets import QApplication, QMessageBox

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from ui.deploy_thread import DeployWorker
from ui.library_view import ModLibraryView
from ui.mod_detail_panel import ModDetailPanel, humanize_deploy_error


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_ui.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _ensure_game(db: DatabaseManager, app_id: int = 99) -> None:
    db.update_game_deploy_config(app_id, name="TestGame", mod_path="")


def _make_mod(library: Path, *, mod_id: str = "8001", app_id: int = 99) -> Path:
    mod_dir = library / "TestGame" / "DeployMe"
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True)
    (mod_dir / "pak.txt").write_text("data", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mod_id}",\n'
        '  "title": "DeployMe",\n'
        f'  "app_id": {app_id},\n'
        '  "game_name": "TestGame"\n'
        "}\n",
        encoding="utf-8",
    )
    return mod_dir


def _pump(ms: int = 50) -> None:
    QCoreApplication.processEvents()
    QThread.msleep(ms)
    QCoreApplication.processEvents()


def test_humanize_deploy_errors() -> None:
    assert humanize_deploy_error("请先配置游戏部署目录") == "请先配置游戏部署目录"
    assert humanize_deploy_error("源 Mod 目录不存在（库：x）") == "内容目录不存在，无法部署"
    assert humanize_deploy_error("复制失败：disk full") == "部署失败：文件复制错误"
    assert (
        humanize_deploy_error("Target mod directory does not exist")
        == "Mod 安装目录不存在，请检查游戏设置"
    )


def test_click_deploy_starts_worker(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    mod_dir = _make_mod(library)
    _ensure_game(db)
    db.upsert_mod(
        ModMetadata(published_file_id="8001", title="DeployMe", app_id=99)
    )
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)

    started: list[str] = []
    constructed: list[DeployWorker] = []

    class FakeWorker(DeployWorker):
        def __init__(self, *args, **kwargs):  # noqa: ANN002
            super().__init__(*args, **kwargs)
            constructed.append(self)

        def start(self, *args, **kwargs):  # noqa: ANN002
            started.append(self.mod_id)
            # Simulate deploy_started without running a real OS thread.
            self.deploy_started.emit()

        def isRunning(self) -> bool:  # noqa: N802
            return self.mod_id in started and self.mod_id not in ("done",)

    monkeypatch.setattr("ui.library_view.DeployWorker", FakeWorker)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    view.detail_panel.show_mod(mod_dir)

    assert view.detail_panel.btn_deploy.isEnabled()
    assert view.detail_panel.btn_deploy.text() == "部署"
    assert not view.detail_panel.btn_redeploy.isEnabled()

    # Panel → signal → library starts worker (no UI-thread deploy_mod)
    view.detail_panel._request_deploy()

    assert started == ["8001"]
    assert constructed and constructed[0].mod_id == "8001"
    assert view._deploy_worker is constructed[0]
    assert view.detail_panel._deploy_busy is True
    assert "正在部署" in view.detail_panel.view_deploy.text()


def test_success_result_refreshes_panel_status(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "mod"
    mod_dir = _make_mod(library)
    _ensure_game(db)
    db.upsert_mod(
        ModMetadata(published_file_id="8001", title="DeployMe", app_id=99)
    )
    db.update_mod_deploy_status(
        8001,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path=str(tmp_path / "Mods" / "DeployMe"),
        deploy_time="2026-01-01T00:00:00+00:00",
    )

    panel = ModDetailPanel()
    panel.show_mod(mod_dir)
    panel.set_deploy_busy(True)
    panel.apply_deploy_result(
        {
            "success": True,
            "mod_id": "8001",
            "target": str(tmp_path / "Mods" / "DeployMe"),
            "copied_files": 1,
        }
    )

    assert "已部署" in panel.view_deploy.text()
    assert panel.view_deploy_path.text().startswith("目标路径")
    assert "DeployMe" in panel.view_deploy_path.text()
    assert "2026-01-01" in panel.view_deploy_time.text()
    assert panel._deploy_busy is False
    assert panel.btn_redeploy.isEnabled()
    assert panel.btn_undeploy.isEnabled()
    assert not panel.btn_deploy.isEnabled()


def test_failure_shows_error(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "mod"
    mod_dir = _make_mod(library)
    _ensure_game(db)
    db.upsert_mod(
        ModMetadata(published_file_id="8001", title="DeployMe", app_id=99)
    )

    panel = ModDetailPanel()
    panel.show_mod(mod_dir)
    panel.set_deploy_busy(True)
    panel.apply_deploy_result(
        {"success": False, "error": "请先配置游戏部署目录"}
    )

    assert "部署失败" in panel.view_deploy.text()
    assert "请先配置游戏部署目录" in panel.view_deploy_error.text()
    assert panel.btn_deploy.isEnabled()


def test_deploy_mod_runs_off_ui_thread(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    """ModDeployer.deploy_mod must not execute on the Qt GUI thread."""
    library = tmp_path / "mod"
    _make_mod(library)
    game_mods = tmp_path / "GameMods"
    game_mods.mkdir()
    db.update_game_deploy_config(99, name="TestGame", mod_path=str(game_mods))
    db.upsert_mod(
        ModMetadata(published_file_id="8001", title="DeployMe", app_id=99)
    )
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    monkeypatch.setattr("services.deploy.get_db", lambda: db)

    ui_thread = threading.get_ident()
    seen: dict[str, int] = {}

    real_deploy = __import__("services.deploy", fromlist=["ModDeployer"]).ModDeployer.deploy_mod

    def tracking_deploy(self, mod_id):  # noqa: ANN001
        seen["thread"] = threading.get_ident()
        return real_deploy(self, mod_id)

    monkeypatch.setattr(
        "services.deploy.ModDeployer.deploy_mod",
        tracking_deploy,
    )

    results: list[dict] = []
    worker = DeployWorker("8001", library_root=library)
    worker.deploy_finished.connect(lambda r: results.append(r))
    worker.start()
    assert worker.wait(10_000)
    _pump(20)

    assert "thread" in seen
    assert seen["thread"] != ui_thread
    assert results and results[0].get("success") is True


def test_library_deploy_finished_does_not_call_refresh(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    mod_dir = _make_mod(library)
    game_mods = tmp_path / "Mods"
    game_mods.mkdir()
    db.update_game_deploy_config(99, name="TestGame", mod_path=str(game_mods))
    db.upsert_mod(
        ModMetadata(published_file_id="8001", title="DeployMe", app_id=99)
    )
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
    )

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.refresh()
    view.detail_panel.show_mod(mod_dir)

    refresh_calls: list[int] = []
    original = view.refresh

    def counting_refresh() -> None:
        refresh_calls.append(1)
        original()

    monkeypatch.setattr(view, "refresh", counting_refresh)

    view._deploy_mod_id = "8001"
    view._on_deploy_finished(
        {
            "success": True,
            "mod_id": "8001",
            "target": str(game_mods / "DeployMe"),
            "copied_files": 1,
        }
    )
    _pump()

    assert refresh_calls == []
    card = view._card_for_path(mod_dir)
    assert card is not None
