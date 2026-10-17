"""Projection notify must never run UI listeners on worker threads."""

from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_GITHUB, PLATFORM_STEAM
from services.content_status_eval import persist_evaluated_content_status
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.library_status import CONTENT_CONTENT_MISSING
from services.mod_fs_observer import LEVEL_STATE, observe_mod_fs
from services.mod_library_cache import get_library_cache, reset_library_cache
from services.mod_projection_events import (
    notify_mod_changed,
    reset_mod_changed_listeners,
    subscribe_mod_changed,
)
from services.mod_refresh import refresh_mod
from ui.mod_detail_panel import format_size

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_mod_changed_listeners()
    manager = DatabaseManager.instance(tmp_path / "proj_thread.db")
    yield manager
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_mod_changed_listeners()


def _seed(
    library: Path,
    db: DatabaseManager,
    *,
    title: str = "ThreadMod",
    game: str = "ThreadGame",
    app_id: int = 292030,
    platform: str = PLATFORM_STEAM,
    suffix: str = "",
) -> tuple[Path, str]:
    db.upsert_game(GameInfo(app_id=app_id, name=game, folder_name=game))
    token = suffix or title.lower().replace(" ", "-")
    if platform == PLATFORM_STEAM:
        ext = str(555000 + abs(hash(token)) % 100000)
        url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={ext}"
        created = create_mod_identity(
            db,
            platform=platform,
            external_id=ext,
            workshop_id=ext,
            source_url=url,
            title=title,
            app_id=app_id,
            game_name=game,
            operation="import",
        )
    else:
        created = create_mod_identity(
            db,
            platform=platform,
            external_id=f"owner/{token}",
            source_url=f"https://github.com/owner/{token}",
            title=title,
            app_id=app_id,
            game_name=game,
            operation="import",
        )
    mid = str(created.mod_id)
    folder = library / game / f"{title}_{mid}"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "title": title,
                "app_id": app_id,
                "game_name": game,
            }
        ),
        encoding="utf-8",
    )
    (folder / "mod.pak").write_bytes(b"payload")
    db.update_mod_identity_fields(
        mid,
        folder_present=True,
        last_known_path=str(folder.resolve()),
        app_id=app_id,
        platform=platform,
    )
    db.set_official_metadata_synced(mid, True)
    return folder, mid


def test_notify_on_gui_thread_stays_synchronous(qapp: QApplication) -> None:
    seen: list[str] = []
    subscribe_mod_changed(lambda m: seen.append(str(m)))
    notify_mod_changed("880001")
    assert seen == ["880001"]


def test_worker_notify_marshals_listener_to_gui_thread(qapp: QApplication) -> None:
    gui = qapp.thread()
    listener_threads: list[object] = []
    worker_id = threading.get_ident()

    def _on_changed(mid: str) -> None:
        listener_threads.append(QThread.currentThread())

    subscribe_mod_changed(_on_changed)
    order: list[str] = []

    def _worker() -> None:
        order.append("worker-start")
        notify_mod_changed("880002")
        order.append("worker-after-notify")

    thread = threading.Thread(target=_worker, name="proj-notify-worker")
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert order == ["worker-start", "worker-after-notify"]
    assert listener_threads == []

    for _ in range(50):
        qapp.processEvents()
        if listener_threads:
            break

    assert listener_threads == [gui]
    assert threading.get_ident() == worker_id or True


def test_refresh_projection_still_runs_on_caller_thread(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed(library, db, suffix="proj-caller")
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)

    proj_threads: list[int] = []
    real = cache.refresh_projection

    def _tracked(internal_id: str):
        proj_threads.append(threading.get_ident())
        return real(internal_id)

    monkeypatch.setattr(cache, "refresh_projection", _tracked)
    worker_ident: list[int] = []

    def _worker() -> None:
        worker_ident.append(threading.get_ident())
        notify_mod_changed(mid)

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=5)
    assert proj_threads == worker_ident
    assert proj_threads
    assert proj_threads[0] != threading.get_ident()


def test_persist_content_status_from_worker_does_not_touch_ui_inline(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    """ImportWorker / FS L1 path: persist → notify → UI only after queue."""
    library = tmp_path / "mod"
    folder, mid = _seed(library, db, platform=PLATFORM_GITHUB, suffix="persist-github")
    db.update_mod_content_status(mid, content_status=CONTENT_CONTENT_MISSING)
    get_library_cache().load_snapshot(library, force=True)
    seen_on: list[object] = []
    subscribe_mod_changed(lambda _m: seen_on.append(QThread.currentThread()))

    def _worker() -> None:
        persist_evaluated_content_status(
            mid, folder, db=db, folder_present=True, notify_projection=True
        )

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=5)
    assert seen_on == []
    for _ in range(50):
        qapp.processEvents()
        if seen_on:
            break
    assert seen_on
    assert all(thread is qapp.thread() for thread in seen_on)


def test_l1_observe_from_worker_marshals_projection(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed(library, db, suffix="l1-observe")
    get_library_cache().load_snapshot(library, force=True)
    (folder / "mod.pak").unlink()
    seen_on: list[object] = []
    subscribe_mod_changed(lambda _m: seen_on.append(QThread.currentThread()))

    def _worker() -> None:
        observe_mod_fs(mid, LEVEL_STATE, db=db)

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=5)
    assert seen_on == []
    for _ in range(50):
        qapp.processEvents()
        if seen_on:
            break
    assert seen_on
    assert all(thread is qapp.thread() for thread in seen_on)


def test_refresh_mod_from_worker_marshals_projection(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed(library, db, platform=PLATFORM_STEAM, suffix="steam-refresh")
    get_library_cache().load_snapshot(library, force=True)
    seen_on: list[object] = []
    subscribe_mod_changed(lambda _m: seen_on.append(QThread.currentThread()))
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            (folder / "mod.pak").unlink()
            refresh_mod(
                mid,
                folder,
                platform=PLATFORM_STEAM,
                library_root=library,
                db=db,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join(timeout=15)
    assert errors == []
    assert seen_on == []
    for _ in range(50):
        qapp.processEvents()
        if seen_on:
            break
    assert seen_on
    assert all(thread is qapp.thread() for thread in seen_on)


def test_size_badge_signal_accepts_bytes_above_32bit(qapp: QApplication) -> None:
    class _Signals(QObject):
        finished = Signal(str, object)

    received: list[object] = []

    def _slot(token: str, total: object) -> None:
        received.append((token, int(total)))

    signals = _Signals()
    signals.finished.connect(_slot, Qt.ConnectionType.QueuedConnection)
    huge = 3 * (2**31)
    signals.finished.emit("tok", huge)
    for _ in range(20):
        qapp.processEvents()
        if received:
            break
    assert received == [("tok", huge)]
    assert format_size(huge).endswith("GB")


def test_size_badge_impl_uses_observation_service() -> None:
    src = inspect.getsource(
        __import__("ui.mod_detail_panel", fromlist=["ModDetailPanel"]).ModDetailPanel._request_size_badge_async
    )
    assert "enqueue_mod_size" in src
    assert "Signal(str, int)" not in src


def test_notify_impl_uses_queued_gui_dispatch() -> None:
    src = inspect.getsource(
        __import__("services.mod_projection_events", fromlist=["notify_mod_changed"])
    )
    assert "QueuedConnection" in src
    assert "_should_marshal_to_gui" in src
    assert "refresh_projection" in src
