"""Phase 4: deploy UI — DeployWorker + ModDetailPanel / library wiring."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QThread
from PySide6.QtWidgets import QApplication, QMessageBox

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager
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
    manager = DatabaseManager.instance(tmp_path / "deploy_ui.db")
    yield manager
    DatabaseManager.reset_instance()


def _ensure_game(db: DatabaseManager, app_id: int = 99, *, mod_path: str = "") -> None:
    db.update_game_deploy_config(app_id, name="TestGame", mod_path=mod_path)


def _make_mod(
    db: DatabaseManager, library: Path, *, mod_id: str = "8001", app_id: int = 99
) -> tuple[Path, str]:
    mod_dir = library / "TestGame" / "DeployMe"
    mod_dir.mkdir(parents=True)
    (mod_dir / "pak.txt").write_text("data", encoding="utf-8")
    created = create_steam_test_mod(
        db, external_id=mod_id, title="DeployMe", app_id=app_id, game_name="TestGame"
    )
    pk = prove_managed_folder(
        db,
        mod_dir,
        handle=created.mod_id,
        title="DeployMe",
        app_id=app_id,
        game_name="TestGame",
    )
    return mod_dir, pk


def _pump(ms: int = 50) -> None:
    QCoreApplication.processEvents()
    QThread.msleep(ms)
    QCoreApplication.processEvents()


def test_humanize_deploy_errors() -> None:
    assert humanize_deploy_error("请先配置游戏部署目录") == "请先配置游戏部署目录"
    # Source / custom / install lifecycle messages stay specific.
    assert (
        humanize_deploy_error("源 Mod 目录不存在（库：x）")
        == "源 Mod 目录不存在（库：x）"
    )
    assert (
        humanize_deploy_error("Mod自定义部署路径不存在: D:/old/path")
        == "Mod自定义部署路径不存在: D:/old/path"
    )
    assert (
        humanize_deploy_error("游戏安装目录不存在: F:/missing")
        == "游戏安装目录不存在: F:/missing"
    )
    assert humanize_deploy_error("复制失败：disk full") == "部署失败：文件复制错误"
    # Legacy English must not collapse into vague "请检查游戏设置".
    assert (
        humanize_deploy_error("Target mod directory does not exist")
        == "Target mod directory does not exist"
    )


def test_click_deploy_starts_worker(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    _ensure_game(db, mod_path=str(tmp_path / "GameMods"))
    mod_dir, pk = _make_mod(db, library)

    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db, raising=False)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    monkeypatch.setattr("services.mod_library_cache.get_db", lambda: db)

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
    view.detail_panel.show_mod(mod_dir, mod_id=pk)

    assert view.detail_panel.btn_deploy.isEnabled()
    assert view.detail_panel.btn_deploy.text() == "部署"
    assert not view.detail_panel.btn_redeploy.isEnabled()

    # Panel → signal → library starts worker (no UI-thread deploy_mod)
    view.detail_panel._request_deploy()

    assert started == [pk]
    assert constructed and constructed[0].mod_id == pk
    assert view._deploy_worker is constructed[0]
    assert view.detail_panel._deploy_busy is True
    assert "正在部署" in view.detail_panel.view_deploy.text()


def test_success_result_refreshes_panel_status(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "mod"
    _ensure_game(db)
    mod_dir, pk = _make_mod(db, library)

    db.update_mod_deploy_status(
        pk,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path=str(tmp_path / "Mods" / "DeployMe"),
        deploy_time="2026-01-01T00:00:00+00:00",
    )

    panel = ModDetailPanel()
    panel.show_mod(mod_dir, mod_id=pk)
    panel.set_deploy_busy(True)
    panel.apply_deploy_result(
        {
            "success": True,
            "mod_id": pk,
            "target": str(tmp_path / "Mods" / "DeployMe"),
            "copied_files": 1,
            "deploy_time": "2026-01-01T00:00:00+00:00",
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
    _ensure_game(db)
    mod_dir, pk = _make_mod(db, library)

    panel = ModDetailPanel()
    panel.show_mod(mod_dir, mod_id=pk)
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
    game_mods = tmp_path / "GameMods"
    game_mods.mkdir()
    db.update_game_deploy_config(99, name="TestGame", mod_path=str(game_mods))
    _mod_dir, pk = _make_mod(db, library)

    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    monkeypatch.setattr("services.mod_library_cache.get_db", lambda: db)
    monkeypatch.setattr("services.deploy.get_db", lambda: db)

    ui_thread = threading.get_ident()
    seen: dict[str, int] = {}

    results: list[dict] = []

    def tracking_deploy(self, mod_id):  # noqa: ANN001
        seen["thread"] = threading.get_ident()
        return {
            "success": True,
            "mod_id": str(mod_id),
            "target": str(game_mods / "DeployMe"),
            "copied_files": 1,
        }

    monkeypatch.setattr(
        "services.deploy.ModDeployer.deploy_mod",
        tracking_deploy,
    )

    worker = DeployWorker(pk, library_root=library)
    worker.deploy_finished.connect(lambda r: results.append(r))
    worker.deploy_failed.connect(
        lambda r: results.append(r if isinstance(r, dict) else {"success": False, "error": r})
    )
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
    game_mods = tmp_path / "Mods"
    game_mods.mkdir()
    db.update_game_deploy_config(99, name="TestGame", mod_path=str(game_mods))
    mod_dir, pk = _make_mod(db, library)

    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db, raising=False)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    monkeypatch.setattr("services.mod_library_cache.get_db", lambda: db)
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
    view.detail_panel.show_mod(mod_dir, mod_id=pk)

    refresh_calls: list[int] = []
    original = view.refresh

    def counting_refresh() -> None:
        refresh_calls.append(1)
        original()

    monkeypatch.setattr(view, "refresh", counting_refresh)

    view._deploy_mod_id = pk
    view._on_deploy_finished(
        {
            "success": True,
            "mod_id": pk,
            "target": str(game_mods / "DeployMe"),
            "copied_files": 1,
        }
    )
    _pump()

    assert refresh_calls == []
    card = view._card_for_path(mod_dir)
    assert card is not None
