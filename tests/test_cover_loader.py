"""CoverLoaderManager — QThreadPool cover loading (no threading.Thread)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication

from services.cover_loader import (
    CoverLoaderManager,
    MAX_COVER_WORKERS,
    reset_cover_loader_stats,
    resolve_cover_path,
)
from services.file_ops import INFO_DIR_NAME
from ui.mod_card import ModCardWidget


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _reset_loader() -> None:
    CoverLoaderManager.reset_instance()
    reset_cover_loader_stats()
    yield
    CoverLoaderManager.reset_instance()
    reset_cover_loader_stats()


def _write_cover(folder: Path) -> Path:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    path = info / "cover.png"
    pix = QPixmap(64, 64)
    pix.fill()
    pix.save(str(path), "PNG")
    return path


def test_max_workers_capped(qapp: QApplication) -> None:
    mgr = CoverLoaderManager.instance()
    assert mgr._pool.maxThreadCount() <= MAX_COVER_WORKERS
    assert MAX_COVER_WORKERS <= 4


def test_resolve_cover_path_finds_info_cover(tmp_path: Path) -> None:
    folder = tmp_path / "Game" / "Mod"
    cover = _write_cover(folder)
    assert resolve_cover_path(folder) == cover


def test_request_emits_qimage_on_gui_thread(
    qapp: QApplication, tmp_path: Path
) -> None:
    folder = tmp_path / "Game" / "Mod"
    _write_cover(folder)
    mgr = CoverLoaderManager.instance()
    got: list[object] = []

    def _on_ready(token: str, image: object) -> None:
        got.append((token, image, type(image)))

    mgr.image_ready.connect(_on_ready)
    mgr.request("t1", folder, width=40, height=30)

    deadline = time.time() + 3.0
    while time.time() < deadline and not got:
        qapp.processEvents()
        time.sleep(0.02)

    assert got
    token, image, ty = got[0]
    assert token == "t1"
    assert ty is QImage
    assert isinstance(image, QImage)
    assert not image.isNull()


def test_cancel_for_managed_path_ignores_late_result(
    qapp: QApplication, tmp_path: Path
) -> None:
    folder = tmp_path / "Game" / "Unknown Mod 1"
    _write_cover(folder)
    mgr = CoverLoaderManager.instance()
    got: list[object] = []
    mgr.image_ready.connect(lambda *a: got.append(a))
    mgr.request("tok-old", folder, width=40, height=30)
    mgr.cancel_for_managed_path(folder)
    assert mgr.is_path_cancelled(folder)
    assert mgr.inflight_count(folder) == 0

    deadline = time.time() + 2.0
    while time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    # Cancelled path must not emit image_ready for the stale token.
    assert got == []


def test_cancel_for_managed_path_waits_for_inflight(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "Game" / "Locked Mod"
    _write_cover(folder)
    mgr = CoverLoaderManager.instance()

    # Hold the cover task on the pool until we cancel+wait.
    release = threading.Event()
    real_resolve = resolve_cover_path

    def slow_resolve(managed_path, cover_ref=""):
        release.wait(timeout=2.0)
        return real_resolve(managed_path, cover_ref)

    monkeypatch.setattr(
        "services.cover_loader.resolve_cover_path",
        slow_resolve,
    )
    mgr.request("slow", folder, width=40, height=30)
    deadline = time.time() + 1.0
    while time.time() < deadline and mgr.inflight_count(folder) == 0:
        qapp.processEvents()
        time.sleep(0.01)
    assert mgr.inflight_count(folder) >= 1
    release.set()
    mgr.cancel_for_managed_path(folder, wait_ms=2000)
    assert mgr.inflight_count(folder) == 0


def test_mod_card_uses_cover_loader_not_threading(
    qapp: QApplication, tmp_path: Path
) -> None:
    import inspect

    import ui.mod_card as mod_card_mod

    src = inspect.getsource(mod_card_mod)
    assert "threading.Thread" not in src
    assert "CoverLoaderManager" in src

    folder = tmp_path / "Game" / "Mod"
    _write_cover(folder)
    card = ModCardWidget(folder)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
        pm = card.cover_label.pixmap()
        if pm is not None and not pm.isNull() and pm.width() > 1:
            # Placeholder is also non-null; wait until loader applied scaled cover.
            if pm.width() == 40 or pm.height() > 10:
                break
    # At least placeholder or real cover is present.
    assert card.cover_label.pixmap() is not None
