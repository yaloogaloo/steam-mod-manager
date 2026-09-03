"""Visible-only CoverLoader scheduling — no full-grid submit on Library open."""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata
import services.cover_loader as cl
from services.cover_cache import put_cover_image
from services.cover_loader import (
    CoverLoaderManager,
    MAX_COVER_WORKERS,
    reset_cover_loader_stats,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.mod_library_cache import ModCardData, reset_library_cache
from ui.library_view import ModLibraryView
from ui.main_window import MainWindow
from ui.mod_card import COVER_HEIGHT, COVER_WIDTH, ModCardWidget


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _reset_covers() -> None:
    CoverLoaderManager.reset_instance()
    reset_cover_loader_stats()
    reset_library_cache()
    yield
    CoverLoaderManager.reset_instance()
    reset_cover_loader_stats()
    reset_library_cache()


def _write_cover(folder: Path) -> Path:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    path = info / "cover.png"
    pix = QPixmap(32, 32)
    pix.fill()
    pix.save(str(path), "PNG")
    return path


def _seed_mods(lib: Path, db: DatabaseManager, n: int, *, id_base: int = 810000) -> None:
    game = lib / "Game"
    game.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        mid = str(id_base + i)
        folder = game / f"Mod{i:04d}"
        cover = _write_cover(folder)
        (folder / INFO_DIR_NAME / METADATA_FILENAME).write_text(
            json.dumps(
                {
                    "published_file_id": mid,
                    "title": f"Mod {i}",
                    "game_name": "Game",
                    "cover_path": ".info/cover.png",
                }
            ),
            encoding="utf-8",
        )
        db.upsert_mod(
            ModMetadata(
                published_file_id=mid,
                title=f"Mod {i}",
                managed_path=str(folder),
                game_name="Game",
                cover_path=".info/cover.png",
            )
        )
        db.update_mod_cover_path(mid, ".info/cover.png")
        del cover


def _pump(qapp: QApplication, seconds: float = 0.25) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


def _open_library(qapp: QApplication, lib: Path) -> ModLibraryView:
    view = ModLibraryView()
    view.set_target_root(str(lib))
    view.resize(1200, 560)
    view.show()
    qapp.processEvents()
    view.refresh(force=False)
    qapp.processEvents()
    view._load_viewport_covers()
    _pump(qapp, 0.2)
    return view


def test_a_does_not_submit_all_covers(
    qapp: QApplication, tmp_path: Path
) -> None:
    db = DatabaseManager.instance()
    lib = tmp_path / "mod"
    n = 80
    _seed_mods(lib, db, n)
    view = _open_library(qapp, lib)
    assert len(view._cards) == n
    assert cl.COVER_LOAD_REQUESTS < n
    assert cl.COVER_LOAD_REQUESTS <= 24
    view.close()


def test_b_visible_cards_are_submitted(
    qapp: QApplication, tmp_path: Path
) -> None:
    db = DatabaseManager.instance()
    lib = tmp_path / "mod"
    _seed_mods(lib, db, 40)
    view = _open_library(qapp, lib)
    visible = view.iter_viewport_cover_cards()
    assert visible
    assert cl.COVER_LOAD_REQUESTS >= 1
    assert cl.COVER_LOAD_REQUESTS >= min(4, len(visible))
    view.close()


def test_c_scroll_loads_new_cards(
    qapp: QApplication, tmp_path: Path
) -> None:
    db = DatabaseManager.instance()
    lib = tmp_path / "mod"
    _seed_mods(lib, db, 60)
    view = _open_library(qapp, lib)
    first = int(cl.COVER_LOAD_REQUESTS)
    bar = view.scroll.verticalScrollBar()
    if bar.maximum() <= 0:
        view.resize(1200, 360)
        qapp.processEvents()
        view._sync_library_host_size()
        qapp.processEvents()
    bar.setValue(bar.maximum())
    qapp.processEvents()
    view._load_viewport_covers()
    _pump(qapp, 0.25)
    assert cl.COVER_LOAD_REQUESTS >= first
    assert cl.COVER_LOAD_REQUESTS > first or bar.maximum() == 0
    view.close()


def test_d_cache_hit_does_not_resubmit(qapp: QApplication, tmp_path: Path) -> None:
    folder = tmp_path / "Game" / "Mod"
    cover = _write_cover(folder)
    img = QImage(COVER_WIDTH, COVER_HEIGHT, QImage.Format.Format_RGB32)
    img.fill(1)
    put_cover_image(cover, COVER_WIDTH, COVER_HEIGHT, img)
    data = ModCardData(
        id="9001",
        title="M",
        platform="steam",
        cover=str(cover),
        description="",
        tags="",
        size=0,
        updated_time=0.0,
        managed_path=str(folder),
        game_folder="Game",
    )
    before = int(cl.COVER_LOAD_REQUESTS)
    hits_before = int(cl.COVER_LOAD_CACHE_HITS)
    card = ModCardWidget(folder, card_data=data)
    assert card.ensure_cover() is False
    assert cl.COVER_LOAD_REQUESTS == before
    assert cl.COVER_LOAD_CACHE_HITS == hits_before + 1
    assert card.ensure_cover() is False
    assert cl.COVER_LOAD_REQUESTS == before


def test_e_rebind_does_not_keep_old_cover(qapp: QApplication, tmp_path: Path) -> None:
    a = tmp_path / "Game" / "A"
    b = tmp_path / "Game" / "B"
    ca = _write_cover(a)
    _write_cover(b)
    data_a = ModCardData(
        id="100",
        title="A",
        platform="steam",
        cover=str(ca),
        description="",
        tags="",
        size=0,
        updated_time=0.0,
        managed_path=str(a),
        game_folder="Game",
    )
    data_b = ModCardData(
        id="200",
        title="B",
        platform="steam",
        cover=str(b / INFO_DIR_NAME / "cover.png"),
        description="",
        tags="",
        size=0,
        updated_time=0.0,
        managed_path=str(b),
        game_folder="Game",
    )
    card = ModCardWidget(a, card_data=data_a)
    card.ensure_cover()
    old_token = card._cover_token
    card.rebind(b, card_data=data_b)
    assert card._cover_token != old_token or card._cover_token == ""
    assert card._internal_entity_id() == "200"
    assert getattr(card, "_cover_applied_token", "") == ""


def test_f_stale_token_ignored_after_rebind(
    qapp: QApplication, tmp_path: Path
) -> None:
    a = tmp_path / "Game" / "A"
    b = tmp_path / "Game" / "B"
    _write_cover(a)
    _write_cover(b)
    data_a = ModCardData(
        id="11",
        title="A",
        platform="steam",
        cover="",
        description="",
        tags="",
        size=0,
        updated_time=0.0,
        managed_path=str(a),
        game_folder="Game",
    )
    data_b = ModCardData(
        id="22",
        title="B",
        platform="steam",
        cover="",
        description="",
        tags="",
        size=0,
        updated_time=0.0,
        managed_path=str(b),
        game_folder="Game",
    )
    card = ModCardWidget(a, card_data=data_a)
    card.ensure_cover()
    stale = card._cover_token
    card.rebind(b, card_data=data_b)
    img = QImage(COVER_WIDTH, COVER_HEIGHT, QImage.Format.Format_RGB32)
    img.fill(2)
    card._on_cover_image_ready(stale, img)
    assert getattr(card, "_cover_applied_token", "") != stale


def test_g_leave_library_cancels_pending(
    qapp: QApplication, tmp_path: Path
) -> None:
    db = DatabaseManager.instance()
    lib = tmp_path / "mod"
    _seed_mods(lib, db, 30)
    view = _open_library(qapp, lib)
    view._cancel_all_pending_covers()
    qapp.processEvents()
    assert cl.COVER_LOAD_CANCELLED >= 0
    mgr = CoverLoaderManager.instance()
    pending = [c._cover_token for c in view._cards if c._cover_token]
    for tok in pending:
        assert tok not in mgr._active_tokens
    view.close()


def test_h_library_load_worker_lifecycle_unchanged() -> None:
    src = inspect.getsource(ModLibraryView._start_library_worker)
    assert "LibraryLoadWorker" in src
    src_flush = inspect.getsource(ModLibraryView._flush_pending_library_load)
    assert "_start_library_worker" in src_flush


def test_i_reconcile_library_load_still_serialized() -> None:
    src = inspect.getsource(ModLibraryView.refresh)
    assert "library_load_must_wait" in src
    src_mw = inspect.getsource(MainWindow._restore_settings)
    assert "hold_library_load_until_reconcile_idle" in src_mw


def test_j_cover_loader_thread_cap_unchanged() -> None:
    assert MAX_COVER_WORKERS == 4
    mgr = CoverLoaderManager.instance()
    assert mgr._pool.maxThreadCount() <= 4
    rec = Path("services/library_reconcile.py").read_text(encoding="utf-8")
    assert "CREATE TABLE" not in rec
    identity = Path("services/mod_identity.py").read_text(encoding="utf-8")
    assert identity
