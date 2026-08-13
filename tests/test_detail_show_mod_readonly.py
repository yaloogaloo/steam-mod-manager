"""Opening detail must not copy offline trees or write sidecars to SQLite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from ui.mod_detail_panel import ModDetailPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_show_mod_does_not_copytree_or_apply_sidecar(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "detail.db")
    folder = tmp_path / "Game" / "ModA"
    info = folder / INFO_DIR_NAME
    offline = info / "offline"
    offline.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps({"published_file_id": "88001", "title": "ModA"}),
        encoding="utf-8",
    )
    (offline / "index.html").write_text("<html></html>", encoding="utf-8")
    (folder / "a.pak").write_bytes(b"x")
    db.upsert_mod(
        ModMetadata(
            published_file_id="88001",
            title="ModA",
            managed_path=str(folder),
        )
    )

    copies: list[str] = []
    applies: list[str] = []
    syncs: list[str] = []

    def boom_copytree(*_a, **_k):
        copies.append("copytree")
        raise AssertionError("show_mod must not copytree")

    def boom_apply(*_a, **_k):
        applies.append("apply")
        return True

    monkeypatch.setattr("shutil.copytree", boom_copytree)
    monkeypatch.setattr(
        "services.metadata_backup_sync.sync_after_metadata_change",
        lambda *_a, **_k: syncs.append("sync"),
    )
    monkeypatch.setattr(
        "services.info_sidecar.apply_sidecar_to_db", boom_apply, raising=False
    )

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id="88001")
    qapp.processEvents()

    assert copies == []
    assert applies == []
    assert syncs == []
    panel.close()
    panel.deleteLater()
    qapp.processEvents()
    DatabaseManager.reset_instance()
