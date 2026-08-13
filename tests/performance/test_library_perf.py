"""Phase 1 library performance baselines (isolated tmp DB, not the user library)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.mod_library_cache import (
    ModCardData,
    build_library_snapshot,
    reset_library_cache,
)
from ui.mod_card import ModCardWidget
from ui.mod_detail_panel import ModDetailPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _seed_library(root: Path, db: DatabaseManager, count: int) -> Path:
    game = root / "PerfGame"
    for i in range(count):
        mid = str(500000 + i)
        folder = game / f"Mod{i:04d}"
        info = folder / INFO_DIR_NAME
        info.mkdir(parents=True)
        (info / METADATA_FILENAME).write_text(
            json.dumps(
                {
                    "published_file_id": mid,
                    "title": f"Perf Mod {i}",
                    "game_name": "PerfGame",
                    "description": "short",
                }
            ),
            encoding="utf-8",
        )
        (folder / "payload.bin").write_bytes(b"x")
        db.upsert_mod(
            ModMetadata(
                published_file_id=mid,
                title=f"Perf Mod {i}",
                managed_path=str(folder),
                game_name="PerfGame",
            )
        )
    return game


def test_library_snapshot_500_under_two_seconds(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "perf.db")
    lib = tmp_path / "library"
    _seed_library(lib, db, 500)
    reset_library_cache()

    t0 = time.perf_counter()
    snap = build_library_snapshot(lib)
    elapsed = time.perf_counter() - t0

    assert snap.total_count == 500
    assert elapsed < 3.0, f"library snapshot took {elapsed:.3f}s"
    DatabaseManager.reset_instance()
    reset_library_cache()


def test_detail_open_under_300ms(
    qapp: QApplication, tmp_path: Path
) -> None:
    DatabaseManager.reset_instance()
    db = DatabaseManager.instance(tmp_path / "detail_perf.db")
    folder = tmp_path / "Game" / "FastMod"
    info = folder / INFO_DIR_NAME
    (info / "offline").mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps({"published_file_id": "500001", "title": "FastMod"}),
        encoding="utf-8",
    )
    (info / "offline" / "index.html").write_text("<html></html>", encoding="utf-8")
    (folder / "a.pak").write_bytes(b"x" * 64)
    db.upsert_mod(
        ModMetadata(
            published_file_id="500001",
            title="FastMod",
            managed_path=str(folder),
        )
    )

    panel = ModDetailPanel()
    t0 = time.perf_counter()
    panel.show_mod(folder, mod_id="500001")
    qapp.processEvents()
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.3, f"show_mod took {elapsed:.3f}s"
    DatabaseManager.reset_instance()


def test_card_render_100_with_batch_data(
    qapp: QApplication, tmp_path: Path
) -> None:
    host = tmp_path / "host"
    host.mkdir()
    cards = []
    t0 = time.perf_counter()
    for i in range(100):
        folder = tmp_path / "Game" / f"C{i}"
        folder.mkdir(parents=True)
        data = ModCardData(
            id=str(600000 + i),
            title=f"Card {i}",
            platform="steam",
            cover="",
            description="",
            tags="",
            size=0,
            updated_time=0.0,
            managed_path=str(folder),
            game_folder="Game",
            has_offline=True,
            folder_absent=False,
            missing_content=False,
        )
        card = ModCardWidget(folder, card_data=data)
        card.hide()
        cards.append(card)
    elapsed = time.perf_counter() - t0
    assert len(cards) == 100
    assert elapsed < 1.0, f"100 card widgets took {elapsed:.3f}s"
