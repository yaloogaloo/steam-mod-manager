"""Phase 2.1: UI / filter / cover / offline display must go through Metadata Resolver."""

from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, persist_unified_metadata_dict
from services.metadata_backup import backup_root, sync_metadata_backup
from services.mod_metadata_resolver import resolve_cover_path, resolve_offline_page
from ui.library_query import offline_page_exists
from ui.mod_detail_dialog import ModDetailDialog


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "entrypoints.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    return root


@pytest.fixture(autouse=True)
def _disable_dialog_offline_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ModDetailDialog, "_start_offline_refresh_if_needed", lambda self: None
    )


def _write_info(folder: Path, payload: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    persist_unified_metadata_dict(folder, payload)


def _write_backup(
    mod_id: str,
    payload: dict,
    *,
    cover: bool = False,
    offline: bool = False,
) -> Path:
    dest = backup_root(mod_id)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if cover:
        (dest / "cover.jpg").write_bytes(b"backup-cover")
    if offline:
        off = dest / "offline"
        off.mkdir(exist_ok=True)
        (off / "index.html").write_text("<html>backup-offline</html>", encoding="utf-8")
    return dest


def test_detail_dialog_existing_folder_prefers_info(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, data_root: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModA"
    _write_info(
        folder,
        {
            "published_file_id": "920201",
            "title": "A",
            "display_name": "A",
        },
    )
    db.upsert_mod(
        ModMetadata(published_file_id="920201", title="C", managed_path=str(folder))
    )
    db.update_mod_user_metadata("920201", {"display_name": "C"})
    _write_backup(
        "920201",
        {"published_file_id": "920201", "title": "B", "display_name": "B"},
    )

    dialog = ModDetailDialog(folder, mod_id="920201")
    try:
        assert dialog.title_label.text() == "A"
        assert dialog._resolved is not None
        assert dialog._resolved.display_name == "A"
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_detail_dialog_missing_folder_prefers_backup(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, data_root: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModB"
    _write_info(
        folder,
        {
            "published_file_id": "920202",
            "title": "FromInfo",
            "display_name": "FromInfo",
        },
    )
    db.upsert_mod(
        ModMetadata(published_file_id="920202", title="C", managed_path=str(folder))
    )
    db.update_mod_user_metadata("920202", {"display_name": "C"})
    sync_metadata_backup(folder)
    _write_backup(
        "920202",
        {
            "published_file_id": "920202",
            "title": "B",
            "display_name": "B",
            "description": "backup-desc",
        },
    )
    shutil.rmtree(folder)
    db.set_mod_folder_present("920202", present=False)

    dialog = ModDetailDialog(folder, mod_id="920202")
    try:
        assert dialog.title_label.text() == "B"
        assert dialog._resolved is not None
        assert dialog._resolved.folder_present is False
        assert dialog._resolved.description == "backup-desc"
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_missing_folder_opens_backup_offline_from_dialog(
    qapp: QApplication,
    tmp_path: Path,
    db: DatabaseManager,
    data_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModC"
    info = folder / INFO_DIR_NAME / "offline"
    info.mkdir(parents=True)
    persist_unified_metadata_dict(
        folder,
        {"published_file_id": "920203", "title": "Off", "display_name": "Off"},
    )
    (info / "index.html").write_text("<html>info</html>", encoding="utf-8")
    db.upsert_mod(
        ModMetadata(published_file_id="920203", title="Off", managed_path=str(folder))
    )
    sync_metadata_backup(folder)
    shutil.rmtree(folder)
    db.set_mod_folder_present("920203", present=False)

    page = resolve_offline_page("920203", folder)
    assert page is not None
    assert page.is_file()
    assert "mod_backup" in str(page).replace("\\", "/")
    assert offline_page_exists(folder, mod_id="920203") is True

    dialog = ModDetailDialog(folder, mod_id="920203")
    opened: list[str] = []
    monkeypatch.setattr(
        "ui.mod_detail_dialog.QDesktopServices.openUrl",
        lambda url: opened.append(url.toLocalFile()) or True,
    )
    try:
        dialog._open_offline()
        assert len(opened) == 1
        assert Path(opened[0]).resolve() == page.resolve()
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_existing_folder_uses_backup_cover_when_info_cover_deleted(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, data_root: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModD"
    _write_info(
        folder,
        {
            "published_file_id": "920204",
            "title": "CoverMod",
            "display_name": "CoverMod",
            "cover_path": ".info/cover.jpg",
        },
    )
    (folder / INFO_DIR_NAME / "cover.jpg").write_bytes(b"info-cover")
    db.upsert_mod(
        ModMetadata(
            published_file_id="920204", title="CoverMod", managed_path=str(folder)
        )
    )
    sync_metadata_backup(folder)
    (folder / INFO_DIR_NAME / "cover.jpg").unlink()

    cover = resolve_cover_path("920204", folder)
    assert cover is not None
    assert cover.is_file()
    assert "mod_backup" in str(cover).replace("\\", "/")

    dialog = ModDetailDialog(folder, mod_id="920204")
    try:
        pix = dialog.cover_label.pixmap()
        assert pix is not None and not pix.isNull()
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_ui_display_files_do_not_call_load_metadata() -> None:
    root = Path(__file__).resolve().parents[1]
    files = [
        root / "ui" / "mod_detail_dialog.py",
        root / "ui" / "mod_detail_panel.py",
        root / "ui" / "mod_card.py",
        root / "ui" / "library_view.py",
        root / "ui" / "library_query.py",
        root / "services" / "mod_library_cache.py",
        root / "services" / "cover_loader.py",
    ]
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = ""
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name == "load_metadata":
                hits.append(f"line {node.lineno}")
        assert not hits, f"{path.name} still calls load_metadata() for display: {hits}"

    cache_src = (root / "services" / "mod_library_cache.py").read_text(encoding="utf-8")
    assert "list_visible_mods" in cache_src
    dialog_src = (root / "ui" / "mod_detail_dialog.py").read_text(encoding="utf-8")
    assert "resolve_mod_metadata" in dialog_src
    assert "find_local_cover" not in dialog_src
    query_src = (root / "ui" / "library_query.py").read_text(encoding="utf-8")
    assert "resolve_offline_page" in query_src
    assert "offline_page_file_exists" not in query_src
