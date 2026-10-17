"""Persistent local size observation — worker, lifecycle, Layer-1 projection."""

from __future__ import annotations

import inspect
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from services.dir_size import reset_directory_size_cache
from services.mod_library_cache import (
    build_library_snapshot,
    get_library_cache,
    list_item_to_card_data,
    mod_list_item_from_row,
    reset_library_cache,
)
from services.mod_list_item import assert_mod_list_item_layer1
from services.size_observation import (
    MAX_SIZE_WORKERS,
    SIZE_STATUS_FAILED,
    SIZE_STATUS_MISSING,
    SIZE_STATUS_OK,
    SIZE_STATUS_UNKNOWN,
    enqueue_mod_size,
    note_mod_size_ready,
    observe_mod_size,
    reset_size_observation,
    schedule_library_size_refresh,
    set_size_observation_ui_guard,
    wait_for_size_idle,
)
from tests.helpers.identity import bind_managed_path, create_steam_test_mod
from ui.library_query import SORT_LABELS, filter_sort_entries


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_size_observation()
    reset_directory_size_cache()
    set_size_observation_ui_guard(False)
    manager = DatabaseManager.instance(tmp_path / "size_obs.db")
    manager.upsert_game(GameInfo(app_id=42, name="GameX", folder_name="GameX"))
    yield manager
    reset_size_observation()
    set_size_observation_ui_guard(True)
    DatabaseManager.reset_instance()
    reset_library_cache()


def _seed_mod(
    db: DatabaseManager,
    library: Path,
    *,
    mid: str,
    title: str,
    payload: bytes | None = b"x" * 50,
) -> tuple[Path, str]:
    folder = library / "GameX" / title
    folder.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        (folder / "payload.bin").write_bytes(payload)
    created = create_steam_test_mod(
        db, external_id=mid, title=title, app_id=42, game_name="GameX"
    )
    internal_id = str(created.mod_id)
    bind_managed_path(db, internal_id, folder, title=title, game_name="GameX")
    return folder, internal_id


def test_observe_persists_and_reload_matches(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed_mod(db, library, mid="4101", title="Sized")
    obs = observe_mod_size(mid, folder, force=True)
    assert obs.status == SIZE_STATUS_OK
    assert obs.size_bytes == 50
    row = db.get_mod_size_observation(mid)
    assert row is not None
    assert int(row["local_size_bytes"]) == 50
    assert row["local_size_status"] == SIZE_STATUS_OK
    again = db.get_mod_size_observation(mid)
    assert int(again["local_size_bytes"]) == 50


def test_unknown_is_not_zero(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    _folder, mid = _seed_mod(db, library, mid="4102", title="Unknown")
    row = db.get_mod_size_observation(mid)
    assert row is not None
    assert row["local_size_bytes"] is None
    assert str(row["local_size_status"] or SIZE_STATUS_UNKNOWN) in {
        SIZE_STATUS_UNKNOWN,
        "",
    }
    items = db.list_mod_list_items(mod_id=mid)
    assert items[0]["local_size_bytes"] is None
    assert items[0]["local_size_status"] == SIZE_STATUS_UNKNOWN


def test_empty_directory_is_zero_ok(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed_mod(db, library, mid="4103", title="Empty", payload=None)
    obs = observe_mod_size(mid, folder, force=True)
    assert obs.status == SIZE_STATUS_OK
    assert obs.size_bytes == 0
    item = mod_list_item_from_row(db.list_mod_list_items(mod_id=mid)[0])
    assert item.local_size_bytes == 0
    assert item.local_size_status == SIZE_STATUS_OK
    card = list_item_to_card_data(item)
    assert card.size == 0
    assert card.size_status == SIZE_STATUS_OK


def test_missing_directory_is_not_ok_zero(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed_mod(db, library, mid="4104", title="Gone")
    observe_mod_size(mid, folder, force=True)
    import shutil

    shutil.rmtree(folder)
    obs = observe_mod_size(mid, folder, force=True)
    assert obs.status == SIZE_STATUS_MISSING
    assert obs.size_bytes == 50
    row = db.get_mod_size_observation(mid)
    assert row["local_size_status"] == SIZE_STATUS_MISSING
    assert int(row["local_size_bytes"]) == 50
    item = mod_list_item_from_row(db.list_mod_list_items(mod_id=mid)[0])
    card = list_item_to_card_data(item)
    assert card.size is None
    assert card.size_status == SIZE_STATUS_MISSING


def test_failed_keeps_last_known_not_zero(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed_mod(db, library, mid="4105", title="FailKeep")
    observe_mod_size(mid, folder, force=True)

    def _boom(*_a, **_k):
        raise RuntimeError("walk exploded")

    with patch("services.dir_size.directory_size", side_effect=_boom):
        obs = observe_mod_size(mid, folder, force=True)
    assert obs.status == SIZE_STATUS_FAILED
    assert obs.size_bytes == 50
    row = db.get_mod_size_observation(mid)
    assert row["local_size_status"] == SIZE_STATUS_FAILED
    assert int(row["local_size_bytes"]) == 50


def test_enqueue_does_not_block(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed_mod(db, library, mid="4106", title="Async")
    gate = {"go": False}

    def _gated(path, **_k):
        while not gate["go"]:
            time.sleep(0.01)
        return 7

    with patch("services.dir_size.directory_size", side_effect=_gated):
        started = time.perf_counter()
        assert enqueue_mod_size(mid, folder, force=True) is True
        assert time.perf_counter() - started < 0.2
        assert db.get_mod_size_observation(mid)["local_size_bytes"] is None
        gate["go"] = True
        assert wait_for_size_idle(3.0)
    row = db.get_mod_size_observation(mid)
    assert int(row["local_size_bytes"]) == 7
    assert row["local_size_status"] == SIZE_STATUS_OK


def test_duplicate_enqueue_coalesces(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed_mod(db, library, mid="4107", title="Dup")
    gate = {"go": False}

    def _gated(path, **_k):
        while not gate["go"]:
            time.sleep(0.01)
        return 3

    with patch("services.dir_size.directory_size", side_effect=_gated):
        assert enqueue_mod_size(mid, folder, force=True) is True
        assert enqueue_mod_size(mid, folder, force=True) is False
        gate["go"] = True
        assert wait_for_size_idle(3.0)


def test_stale_generation_does_not_overwrite_newer(
    db: DatabaseManager, tmp_path: Path, monkeypatch
) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed_mod(db, library, mid="4108", title="Stale")
    release_old = {"go": False}
    from services import size_observation as so

    original = so.observe_mod_size
    calls = {"n": 0}

    def _slow(internal_id, managed_path=None, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            while not release_old["go"]:
                time.sleep(0.01)
        return original(internal_id, managed_path, **kwargs)

    monkeypatch.setattr(so, "observe_mod_size", _slow)
    assert enqueue_mod_size(mid, folder, force=True) is True
    assert enqueue_mod_size(mid, folder, force=True) is False
    time.sleep(0.05)
    release_old["go"] = True
    assert wait_for_size_idle(3.0)
    row = db.get_mod_size_observation(mid)
    assert row["local_size_status"] == SIZE_STATUS_OK
    assert int(row["local_size_bytes"]) == 50


def test_two_workers_huge_mod_does_not_block_others(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    huge, huge_id = _seed_mod(db, library, mid="4109", title="Huge")
    small, small_id = _seed_mod(db, library, mid="4110", title="Small", payload=b"z" * 9)
    gate = {"go": False}
    done: list[str] = []

    def _maybe_slow(path, **_k):
        text = str(path)
        if "Huge" in text:
            while not gate["go"]:
                time.sleep(0.01)
            return 999
        done.append(text)
        return 9

    with patch("services.dir_size.directory_size", side_effect=_maybe_slow):
        assert enqueue_mod_size(huge_id, huge, force=True) is True
        assert enqueue_mod_size(small_id, small, force=True) is True
        deadline = time.time() + 2.0
        while not done and time.time() < deadline:
            time.sleep(0.02)
        assert done, "small Mod should complete while huge Mod is gated"
        gate["go"] = True
        assert wait_for_size_idle(3.0)
    assert MAX_SIZE_WORKERS == 2
    assert db.get_mod_size_observation(small_id)["local_size_status"] == SIZE_STATUS_OK


def test_import_hook_does_not_wait(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed_mod(db, library, mid="4111", title="ImportHook")
    gate = {"go": False}

    def _gated(path, **_k):
        while not gate["go"]:
            time.sleep(0.01)
        return 4

    with patch("services.dir_size.directory_size", side_effect=_gated):
        started = time.perf_counter()
        note_mod_size_ready(mid, folder)
        assert time.perf_counter() - started < 0.2
        gate["go"] = True
        assert wait_for_size_idle(3.0)
    assert int(db.get_mod_size_observation(mid)["local_size_bytes"]) == 4


def test_forced_refresh_enqueues_without_walk(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    _folder, _mid = _seed_mod(db, library, mid="4112", title="Refresh")
    walked = {"n": 0}

    def _count(path, **_k):
        walked["n"] += 1
        return 1

    with patch("services.dir_size.directory_size", side_effect=_count):
        n = schedule_library_size_refresh(force=True, game_id=42)
        assert n >= 1
        assert walked["n"] == 0
        assert wait_for_size_idle(3.0)
    assert walked["n"] >= 1


def test_layer1_reads_persisted_size(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed_mod(db, library, mid="4113", title="Layer1")
    observe_mod_size(mid, folder, force=True)
    reset_library_cache()
    snap = build_library_snapshot(library)
    item = next(i for i in snap.list_items if i.internal_id == mid)
    assert_mod_list_item_layer1(item)
    assert item.local_size_bytes == 50
    assert item.local_size_status == SIZE_STATUS_OK
    card = next(c for c in snap.cards if c.id == mid)
    assert card.size == 50
    src = inspect.getsource(build_library_snapshot)
    body = src.split('"""', 2)[-1] if '"""' in src else src
    assert "os.walk" not in body
    assert "directory_size" not in body


def test_observe_patches_warm_projection_without_notify_mod_changed(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed_mod(db, library, mid="4120", title="WarmSize")
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)
    before = cache.get_card_data(mid)
    assert before is not None
    assert before.size_status != SIZE_STATUS_OK or before.size is None

    called: list[object] = []
    monkeypatch.setattr(
        "services.mod_projection_events.notify_mod_changed",
        lambda *a, **k: called.append(a or k),
    )
    observe_mod_size(mid, folder, force=True)
    card = cache.get_card_data(mid)
    assert card is not None
    assert card.size == 50
    assert card.size_status == SIZE_STATUS_OK
    item = None
    for candidate in cache._snapshot.list_items:
        if str(candidate.internal_id) == mid:
            item = candidate
            break
    assert item is not None
    assert item.local_size_bytes == 50
    assert item.local_size_status == SIZE_STATUS_OK
    assert called == []


def test_size_observation_source_never_calls_notify_mod_changed() -> None:
    src = inspect.getsource(
        __import__("services.size_observation", fromlist=["observe_mod_size"])
    )
    assert "notify_mod_changed(" not in src
    assert "patch_local_size" in src
    assert "_notify_size_projection" in src
    src = inspect.getsource(filter_sort_entries)
    assert "os.walk" not in src
    assert "directory_size" not in src
    from ui.library_query import sort_key

    key_src = inspect.getsource(sort_key)
    assert "os.walk" not in key_src
    assert "directory_size" not in key_src
    assert "observe_mod_size" not in key_src
    assert "enqueue_mod_size" not in key_src
    labels = " ".join(label for _k, label in SORT_LABELS)
    assert "大小" in labels
    import ui.library_query as lq

    assert lq.SORT_SIZE == "size"


def test_collection_reuses_projection_scalar(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder, mid = _seed_mod(db, library, mid="4114", title="CollSize")
    observe_mod_size(mid, folder, force=True)
    from ui.library_view import ModLibraryView

    src = inspect.getsource(ModLibraryView._filter_index_from_card_data)
    assert "local_size_bytes" in src
    src_prep = inspect.getsource(ModLibraryView._prepare_collection_content_entries)
    assert "filter_sort_entries(" in src_prep
    assert "collection_sort_entries(" not in src_prep


def test_detail_writeback_persists_observation(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from ui.mod_detail_panel import ModDetailPanel

    qapp = QApplication.instance() or QApplication([])
    library = tmp_path / "mod"
    folder, mid = _seed_mod(db, library, mid="4115", title="DetailWrite")
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid)
    assert wait_for_size_idle(3.0)
    for _ in range(30):
        qapp.processEvents()
        if (panel.size_badge.text() or "") not in ("", "计算中…"):
            break
        time.sleep(0.02)
    row = db.get_mod_size_observation(mid)
    assert row["local_size_status"] == SIZE_STATUS_OK
    assert int(row["local_size_bytes"] or 0) >= 50
