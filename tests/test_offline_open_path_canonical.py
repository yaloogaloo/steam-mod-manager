"""Canonical offline-page OPEN path resolver — global regression suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    write_info_sidecar,
)

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME
from services.offline.paths import (
    resolve_offline_page,
    resolve_offline_page_path,
)
from ui.mod_detail_dialog import ModDetailDialog
from ui.mod_detail_panel import ModDetailPanel


def _wait_open_worker(widget, qapp: QApplication, timeout_ms: int = 8000) -> None:
    from PySide6.QtCore import QEventLoop, QTimer

    qapp.processEvents()
    worker = getattr(widget, "_offline_open_worker", None)
    if worker is None or not worker.isRunning():
        qapp.processEvents()
        return
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)
    loop.exec()
    qapp.processEvents()


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "offline_resolve.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed(db: DatabaseManager, lib: Path, *, mid: str, title: str) -> tuple[str, Path]:
    created = create_steam_test_mod(db, external_id=mid, title=title)
    internal_id = str(created.mod_id)
    folder = lib / "Anno 1800" / title
    folder.mkdir(parents=True, exist_ok=True)
    write_info_sidecar(
        folder,
        internal_id=internal_id,
        title=title,
        external_id=mid,
        workspace_id=str(created.workspace_id or mid),
        game_name="Anno 1800",
        extra={"offline_page_path": str(folder / INFO_DIR_NAME / "index.html")},
    )
    bind_managed_path(db, internal_id, folder, title=title)
    return internal_id, folder


def test_resolver_prefers_offline_when_both_exist(tmp_path: Path) -> None:
    folder = tmp_path / "lib" / "Anno 1800" / "BothLayouts"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "mod.json").write_text("{}", encoding="utf-8")
    steam = folder / INFO_DIR_NAME / "index.html"
    steam.write_text("<html>steam legacy</html>", encoding="utf-8")
    preferred = folder / INFO_DIR_NAME / "offline" / "index.html"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("<html>modio offline</html>", encoding="utf-8")

    assert resolve_offline_page(folder) == preferred.resolve()
    # Stale metadata must not win.
    assert (
        resolve_offline_page_path(
            folder, offline_page_path=str(steam)
        )
        == preferred.resolve()
    )


def test_resolver_falls_back_to_steam_index(tmp_path: Path) -> None:
    folder = tmp_path / "lib" / "Anno 1800" / "SteamOnly"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    steam = folder / INFO_DIR_NAME / "index.html"
    steam.write_text("<html>steam only</html>", encoding="utf-8")
    assert resolve_offline_page(folder) == steam.resolve()


def test_resolver_offline_only(tmp_path: Path) -> None:
    folder = tmp_path / "lib" / "Anno 1800" / "OfflineOnly"
    preferred = folder / INFO_DIR_NAME / "offline" / "index.html"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("<html>offline only</html>", encoding="utf-8")
    assert resolve_offline_page(folder) == preferred.resolve()


def test_resolver_neither_exists(tmp_path: Path) -> None:
    folder = tmp_path / "lib" / "Anno 1800" / "NoOffline"
    folder.mkdir(parents=True)
    (folder / INFO_DIR_NAME).mkdir(parents=True)
    assert resolve_offline_page(folder) is None


def test_workspace_equivalent_fixture_prefers_offline(tmp_path: Path) -> None:
    """Workspace 17863499569189047 equivalent: both layouts + stale Steam path."""
    folder = tmp_path / "lib" / "Anno 1800" / "更大的油泵半径"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    steam = folder / INFO_DIR_NAME / "index.html"
    steam.write_text(
        "<!DOCTYPE html><html><head><title>Steam 社区 :: 错误</title></head>"
        "<body>error</body></html>",
        encoding="utf-8",
    )
    preferred = folder / INFO_DIR_NAME / "offline" / "index.html"
    preferred.parent.mkdir(parents=True)
    preferred.write_text(
        "<!DOCTYPE html><html><head>"
        "<title>Bigger Oil Pump Radius - mod.io</title></head>"
        "<body><h1>Bigger Oil Pump Radius</h1></body></html>",
        encoding="utf-8",
    )
    (folder / INFO_DIR_NAME / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": "17808",
                "title": "更大的油泵半径",
                "workspace_id": "17863499569189047",
                "offline_page_path": str(steam),
                "source_type": "modio",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    resolved = resolve_offline_page(folder)
    assert resolved == preferred.resolve()
    assert resolved.as_posix().endswith(".info/offline/index.html")


def test_detail_panel_open_uses_canonical_resolver(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    mid, folder = _seed(db, tmp_path / "lib", mid="17805", title="DetailOpen")
    steam = folder / INFO_DIR_NAME / "index.html"
    steam.write_text("<html>wrong steam</html>", encoding="utf-8")
    preferred = folder / INFO_DIR_NAME / "offline" / "index.html"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("<html>correct offline</html>", encoding="utf-8")
    from core.paths import asset_store_dir
    from services.asset_store import AssetStore
    from services.info_asset_runtime import finalize_live_offline_to_cas
    from services.offline_view_cache import is_offline_view_path

    assert finalize_live_offline_to_cas(
        folder, store=AssetStore(root=asset_store_dir())
    ).ok

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid)
    panel._metadata.offline_page_path = str(steam)

    opened: list[str] = []
    monkeypatch.setattr(
        "ui.mod_detail_panel.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    panel._open_offline()
    _wait_open_worker(panel, qapp)
    assert len(opened) == 1
    opened_path = Path(opened[0]).resolve()
    assert is_offline_view_path(opened_path)
    assert opened_path != steam.resolve()
    assert panel._metadata.offline_page_path == str(opened_path)


def test_detail_dialog_open_uses_canonical_resolver(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    mid, folder = _seed(db, tmp_path / "lib", mid="17806", title="DialogOpen")
    steam = folder / INFO_DIR_NAME / "index.html"
    steam.write_text("<html>wrong</html>", encoding="utf-8")
    preferred = folder / INFO_DIR_NAME / "offline" / "index.html"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("<html>ok</html>", encoding="utf-8")
    from core.paths import asset_store_dir
    from services.asset_store import AssetStore
    from services.info_asset_runtime import finalize_live_offline_to_cas
    from services.offline_view_cache import is_offline_view_path

    assert finalize_live_offline_to_cas(
        folder, store=AssetStore(root=asset_store_dir())
    ).ok

    dialog = ModDetailDialog(folder, mod_id=mid)
    dialog.metadata.offline_page_path = str(steam)
    opened: list[str] = []
    monkeypatch.setattr(
        "ui.mod_detail_dialog.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    dialog._open_offline()
    _wait_open_worker(dialog, qapp)
    assert len(opened) == 1
    assert is_offline_view_path(Path(opened[0]))
    assert Path(opened[0]).resolve() != steam.resolve()


def test_detail_panel_missing_shows_tooltip(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    mid, folder = _seed(db, tmp_path / "lib", mid="17807", title="Missing")

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid)

    opened: list[str] = []
    tips: list[str] = []
    monkeypatch.setattr(
        "ui.mod_detail_panel.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    monkeypatch.setattr(
        "ui.mod_detail_panel.QToolTip.showText",
        lambda *args, **kwargs: tips.append(str(args[1] if len(args) > 1 else "")),
    )
    panel._open_offline()
    assert opened == []
    assert tips == []
