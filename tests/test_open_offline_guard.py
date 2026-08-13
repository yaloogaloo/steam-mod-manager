"""Open-offline button must not launch the browser with empty / missing paths."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME
from services.importers.materialize import materialize_imported_mod
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
    manager = DatabaseManager.instance(tmp_path / "offline_open.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed_mod(lib: Path, *, mid: str, title: str, game: str = "Anno 1800") -> Path:
    folder = lib / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "mod.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": title,
                "game_name": game,
                "offline_page_path": "",
            }
        ),
        encoding="utf-8",
    )
    return folder


def test_open_offline_blocks_empty_path(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    folder = _seed_mod(tmp_path / "lib", mid="92001", title="No Offline")
    db.upsert_mod(ModMetadata(published_file_id="92001", title="No Offline"))
    panel = ModDetailPanel()
    panel.show_mod(folder)

    opened: list[str] = []
    monkeypatch.setattr(
        "ui.mod_detail_panel.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    tips: list[str] = []
    monkeypatch.setattr(
        "ui.mod_detail_panel.QToolTip.showText",
        lambda *args, **kwargs: tips.append(str(args[1] if len(args) > 1 else "")),
    )

    panel._open_offline()
    assert opened == []
    assert tips == []


def test_open_offline_uses_from_local_file(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    folder = _seed_mod(tmp_path / "lib", mid="92002", title="Has Offline")
    index = folder / INFO_DIR_NAME / "offline" / "index.html"
    index.parent.mkdir(parents=True)
    index.write_text("<html><body>ok</body></html>", encoding="utf-8")
    meta = ModMetadata(
        published_file_id="92002",
        title="Has Offline",
        offline_page_path=str(index),
        managed_path=str(folder),
    )
    db.upsert_mod(meta)

    panel = ModDetailPanel()
    panel.show_mod(folder)
    panel._metadata.offline_page_path = str(index)

    opened: list[QUrl] = []
    monkeypatch.setattr(
        "ui.mod_detail_panel.QDesktopServices.openUrl",
        lambda url: opened.append(url) or True,
    )

    panel._open_offline()
    assert len(opened) == 1
    assert opened[0].isLocalFile()
    assert Path(opened[0].toLocalFile()).resolve() == index.resolve()


def test_materialize_does_not_import_offline_mhtml(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """materialize only excludes sidecar MHTML; attach happens once in ImportWorker."""
    src = tmp_path / "src" / "MyMod"
    src.mkdir(parents=True)
    (src / "pak.bin").write_bytes(b"mod")
    mhtml = src / "page.mhtml"
    mhtml.write_bytes(
        b"From: <saved@localhost>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/related; boundary="----=_Next"\r\n'
        b"\r\n"
        b"------=_Next\r\n"
        b"Content-Type: text/html; charset=\"utf-8\"\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n"
        b"\r\n"
        b"<html><body>Anno offline</body></html>\r\n"
        b"------=_Next--\r\n"
    )

    lib = tmp_path / "lib"
    from core.mod_platform import PLATFORM_NEXUS

    info = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="batch-mhtml-1",
        source_url="",
        title="MyMod",
        app_id=916440,
        game_name="纪元1800",
    )

    dest = materialize_imported_mod(
        library_root=lib,
        mod_id=info.mod_id,
        title="MyMod",
        game_name="纪元1800",
        source_folder=src,
        context={"game_id": 916440, "game_name": "纪元1800"},
    )
    from services.file_ops import ModFileManager

    loaded = ModFileManager(lib).load_metadata(dest)
    assert loaded is not None
    assert not (loaded.offline_page_path or "").strip()
    assert not (dest / INFO_DIR_NAME / "offline" / "index.html").is_file()
    assert not (dest / "page.mhtml").exists()
