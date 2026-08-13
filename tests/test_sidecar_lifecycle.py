"""Import / detail lifecycle still syncs sidecar; library render does not."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.info_sidecar import apply_sidecar_to_db, load_info_sidecar, write_sidecar_for_mod
from services.importers import materialize as materialize_mod
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
    manager = DatabaseManager.instance(tmp_path / "sidecar_lifecycle.db")
    yield manager
    DatabaseManager.reset_instance()


def test_import_materialize_still_writes_sidecar() -> None:
    src = inspect.getsource(materialize_mod)
    assert "write_sidecar_for_mod" in src


def test_write_sidecar_for_mod_updates_json(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "Game" / "Mod"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps({"published_file_id": "92001", "title": "Old"}),
        encoding="utf-8",
    )
    db.upsert_mod(
        ModMetadata(published_file_id="92001", title="Old", managed_path=str(folder))
    )
    db.update_mod_user_metadata(
        "92001",
        {
            "display_name": "Imported Name",
            "custom_description": "From import",
            "user_notes": "",
            "favorite": False,
            "platform": "github",
            "source_url": "https://github.com/a/b",
        },
    )
    path = write_sidecar_for_mod(folder, "92001", db=db)
    assert path is not None
    side = load_info_sidecar(folder)
    assert side is not None
    assert side.display_name == "Imported Name"
    assert side.url == "https://github.com/a/b"


def test_detail_show_mod_is_readonly_no_sidecar_apply(
    qapp: QApplication, db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    folder = tmp_path / "Game" / "Mod"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": "92002",
                "title": "T",
                "display_name": "Sidecar Title",
                "url": "https://example.com/m",
                "source_type": "nexus",
            }
        ),
        encoding="utf-8",
    )
    db.upsert_mod(
        ModMetadata(published_file_id="92002", title="T", managed_path=str(folder))
    )

    calls: list[str] = []
    import services.info_sidecar as side_mod

    real = side_mod.apply_sidecar_to_db

    def tracking(managed_path, **kwargs):
        calls.append(str(managed_path))
        return real(managed_path, **kwargs)

    monkeypatch.setattr(side_mod, "apply_sidecar_to_db", tracking)
    sync_calls: list[str] = []
    monkeypatch.setattr(
        "services.metadata_backup_sync.sync_after_metadata_change",
        lambda *_a, **_k: sync_calls.append("sync"),
    )

    panel = ModDetailPanel()
    panel.show_mod(folder)
    qapp.processEvents()
    # Phase 3-B: Detail open is pure-read — no sidecar→DB write, no backup sync.
    assert calls == [], "detail show_mod must not apply sidecar (read-only)"
    assert sync_calls == [], "detail show_mod must not sync backup"
    resolved_title = (panel.view_title.text() or "").replace("\u200b", "")
    assert "Sidecar Title" in resolved_title or panel._resolved is not None
    panel.close()
    panel.deleteLater()
    qapp.processEvents()

