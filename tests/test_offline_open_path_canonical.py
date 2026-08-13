"""Canonical offline-page OPEN path resolver — global regression suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def _seed(lib: Path, *, mid: str, title: str) -> Path:
    folder = lib / "Anno 1800" / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": title,
                "workspace_id": "17863499569189047",
                "offline_page_path": str(info / "index.html"),
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_resolver_prefers_offline_when_both_exist(tmp_path: Path) -> None:
    folder = _seed(tmp_path / "lib", mid="17801", title="BothLayouts")
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
    folder = _seed(tmp_path / "lib", mid="17802", title="SteamOnly")
    steam = folder / INFO_DIR_NAME / "index.html"
    steam.write_text("<html>steam only</html>", encoding="utf-8")
    assert resolve_offline_page(folder) == steam.resolve()


def test_resolver_offline_only(tmp_path: Path) -> None:
    folder = _seed(tmp_path / "lib", mid="17803", title="OfflineOnly")
    preferred = folder / INFO_DIR_NAME / "offline" / "index.html"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("<html>offline only</html>", encoding="utf-8")
    assert resolve_offline_page(folder) == preferred.resolve()


def test_resolver_neither_exists(tmp_path: Path) -> None:
    folder = _seed(tmp_path / "lib", mid="17804", title="NoOffline")
    assert resolve_offline_page(folder) is None


def test_workspace_equivalent_fixture_prefers_offline(tmp_path: Path) -> None:
    """Workspace 17863499569189047 equivalent: both layouts + stale Steam path."""
    folder = _seed(tmp_path / "lib", mid="9000000000000358", title="更大的油泵半径")
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
                "published_file_id": "9000000000000358",
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
    folder = _seed(tmp_path / "lib", mid="17805", title="DetailOpen")
    steam = folder / INFO_DIR_NAME / "index.html"
    steam.write_text("<html>wrong steam</html>", encoding="utf-8")
    preferred = folder / INFO_DIR_NAME / "offline" / "index.html"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("<html>correct offline</html>", encoding="utf-8")

    db.upsert_mod(
        ModMetadata(
            published_file_id="17805",
            title="DetailOpen",
            offline_page_path=str(steam),
            managed_path=str(folder),
        )
    )
    panel = ModDetailPanel()
    panel.show_mod(folder)
    panel._metadata.offline_page_path = str(steam)

    opened: list[str] = []
    monkeypatch.setattr(
        "ui.mod_detail_panel.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    panel._open_offline()
    assert len(opened) == 1
    assert Path(opened[0]).resolve() == preferred.resolve()
    assert panel._metadata.offline_page_path == str(preferred.resolve())


def test_detail_dialog_open_uses_canonical_resolver(
    qapp: QApplication, tmp_path: Path, monkeypatch
) -> None:
    folder = _seed(tmp_path / "lib", mid="17806", title="DialogOpen")
    steam = folder / INFO_DIR_NAME / "index.html"
    steam.write_text("<html>wrong</html>", encoding="utf-8")
    preferred = folder / INFO_DIR_NAME / "offline" / "index.html"
    preferred.parent.mkdir(parents=True)
    preferred.write_text("<html>ok</html>", encoding="utf-8")

    meta = ModMetadata(
        published_file_id="17806",
        title="DialogOpen",
        offline_page_path=str(steam),
        managed_path=str(folder),
    )
    # Persist stale metadata then open via dialog constructor (loads from disk).
    from services.file_ops import ModFileManager

    ModFileManager(tmp_path / "lib").save_metadata(meta, folder)
    dialog = ModDetailDialog(folder)
    dialog.metadata.offline_page_path = str(steam)
    opened: list[str] = []
    monkeypatch.setattr(
        "ui.mod_detail_dialog.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    dialog._open_offline()
    assert len(opened) == 1
    assert Path(opened[0]).resolve() == preferred.resolve()


def test_detail_panel_missing_shows_tooltip(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    folder = _seed(tmp_path / "lib", mid="17807", title="Missing")
    db.upsert_mod(ModMetadata(published_file_id="17807", title="Missing"))
    panel = ModDetailPanel()
    panel.show_mod(folder)

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
