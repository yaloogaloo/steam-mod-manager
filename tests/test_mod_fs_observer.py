"""Mod filesystem observation lifecycle — L0/L1/L2 + projection touch."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from services.content_status_eval import persist_evaluated_content_status
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY
from services.mod_fs_observer import (
    LEVEL_DEEP,
    LEVEL_PROBE,
    LEVEL_STATE,
    begin_projection_defer,
    drain_projection_touch_ids,
    end_projection_defer,
    observe_mod_fs,
    observe_mods_fs_batch,
    set_ui_thread_guard,
    touch_observation_stamp,
)
from services.mod_library_cache import get_library_cache, reset_library_cache
from services.mod_projection_events import (
    notify_mod_changed,
    reset_mod_changed_listeners,
    subscribe_mod_changed,
)
from services.mod_refresh import reconcile_local_state, refresh_mod
from tests.helpers.identity import bind_managed_path, create_steam_test_mod


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_mod_changed_listeners()
    begin_projection_defer()
    end_projection_defer()
    drain_projection_touch_ids()
    # Unit tests run on the pytest main thread; allow L1/L2 without QApp UI guard.
    set_ui_thread_guard(False)
    manager = DatabaseManager.instance(tmp_path / "fs_obs.db")
    yield manager
    set_ui_thread_guard(True)
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_mod_changed_listeners()
    begin_projection_defer()
    end_projection_defer()
    drain_projection_touch_ids()


def _seed(
    library: Path,
    db: DatabaseManager,
    mid: str,
    *,
    with_payload: bool = True,
    game: str = "ObsGame",
    app_id: int = 880031,
) -> Path:
    db.upsert_game(GameInfo(app_id=app_id, name=game, folder_name=game))
    folder = library / game / f"Mod{mid}"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "internal_id": mid,
                "title": f"Mod{mid}",
                "app_id": app_id,
                "game_name": game,
            }
        ),
        encoding="utf-8",
    )
    if with_payload:
        (folder / "mod.pak").write_bytes(b"payload")
    create_steam_test_mod(
        db, external_id=mid, title=f"Mod{mid}", app_id=app_id, game_name=game
    )
    bind_managed_path(db, mid, folder, game_name=game, title=f"Mod{mid}")
    db.update_mod_content_status(mid, content_status=CONTENT_HEALTHY)
    return folder


def test_l0_records_root_mtime_and_stamp(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    mid = "810001"
    folder = _seed(library, db, mid)
    result = observe_mod_fs(mid, LEVEL_PROBE, db=db)
    assert result.root_exists is True
    assert result.root_mtime is not None
    assert result.observed_at
    row = db.get_mod_fs_observation(mid) or {}
    assert str(row.get("fs_observed_at") or "")
    assert row.get("fs_root_mtime") is not None
    assert abs(float(row["fs_root_mtime"]) - float(result.root_mtime)) < 1e-6
    assert folder.is_dir()


def test_external_mtime_change_marks_dirty(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    mid = "810002"
    folder = _seed(library, db, mid)
    first = observe_mod_fs(mid, LEVEL_PROBE, db=db)
    assert first.dirty is False
    time.sleep(0.05)
    folder.touch()
    second = observe_mod_fs(mid, LEVEL_PROBE, db=db, persist_stamp=False)
    assert second.dirty is True


def test_external_add_file_triggers_l1_content_healthy(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = "810003"
    folder = _seed(library, db, mid, with_payload=False)
    persist_evaluated_content_status(
        mid, folder, db=db, folder_present=True, notify_projection=False
    )
    assert (
        str((db.get_mod_backup_row(mid) or {}).get("content_status") or "")
        == CONTENT_CONTENT_MISSING
    )
    (folder / "mod.pak").write_bytes(b"new-payload")
    folder.touch()
    observe_mod_fs(mid, LEVEL_PROBE, db=db)
    result = observe_mod_fs(mid, LEVEL_STATE, db=db)
    assert result.content_status == CONTENT_HEALTHY
    assert (
        str((db.get_mod_backup_row(mid) or {}).get("content_status") or "")
        == CONTENT_HEALTHY
    )


def test_external_delete_file_triggers_l1_content_missing(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = "810004"
    folder = _seed(library, db, mid, with_payload=True)
    persist_evaluated_content_status(
        mid, folder, db=db, folder_present=True, notify_projection=False
    )
    (folder / "mod.pak").unlink()
    folder.touch()
    result = observe_mod_fs(mid, LEVEL_STATE, db=db)
    assert result.content_status == CONTENT_CONTENT_MISSING


def test_detail_refresh_still_runs_l2(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    mid = "810005"
    folder = _seed(library, db, mid)
    calls: list[str] = []

    from services import info_sidecar
    from services import local_file_index

    real_rescan = info_sidecar.rescan_mod_folder
    real_recon = local_file_index.reconcile_local_files

    def _rescan(*a, **k):
        calls.append("rescan")
        return real_rescan(*a, **k)

    def _recon(*a, **k):
        calls.append("reconcile_local")
        return real_recon(*a, **k)

    monkeypatch.setattr("services.info_sidecar.rescan_mod_folder", _rescan)
    monkeypatch.setattr("services.local_file_index.reconcile_local_files", _recon)

    local = reconcile_local_state(mid, folder, db=db)
    assert local.folder_present is True
    assert "rescan" in calls
    assert "reconcile_local" in calls

    calls.clear()
    refresh_mod(mid, folder, db=db)
    assert "rescan" in calls
    assert "reconcile_local" in calls


def test_observe_l2_reuses_deep_sync(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    mid = "810006"
    folder = _seed(library, db, mid)
    calls: list[str] = []

    def _rescan(*_a, **_k):
        calls.append("rescan")
        from core.mod_platform import ModFilesBundle

        return ModFilesBundle()

    def _recon(*_a, **_k):
        from services.local_file_index import LocalReconcileResult

        calls.append("reconcile_local")
        return LocalReconcileResult(updated=False)

    monkeypatch.setattr("services.info_sidecar.rescan_mod_folder", _rescan)
    monkeypatch.setattr("services.local_file_index.reconcile_local_files", _recon)

    result = observe_mod_fs(mid, LEVEL_DEEP, db=db, managed_path=folder)
    assert result.level == LEVEL_DEEP
    assert "rescan" in calls
    assert "reconcile_local" in calls


def test_global_refresh_batch_refuses_full_library_l2(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = "810007"
    _seed(library, db, mid)
    with pytest.raises(ValueError, match="refuses level>=2"):
        observe_mods_fs_batch(level=LEVEL_DEEP, db=db)
    results = observe_mods_fs_batch(level=LEVEL_PROBE, db=db)
    assert any(r.internal_id == mid for r in results)


def test_library_refresh_schedules_l0_not_l2() -> None:
    import inspect

    from ui.library_view import ModLibraryView

    src = inspect.getsource(ModLibraryView.refresh)
    assert "schedule_observe_mods_fs_batch" in src
    assert "LEVEL_PROBE" in src
    assert "LEVEL_DEEP" not in src
    assert "rescan_mod_folder" not in src
    assert "reconcile_local_files" not in src


def test_projection_receives_filesystem_content_change(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = "810008"
    folder = _seed(library, db, mid, with_payload=True)
    reset_library_cache()
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)
    seen: list[str] = []

    def _on_changed(internal_id: str) -> None:
        seen.append(str(internal_id))

    subscribe_mod_changed(_on_changed)
    (folder / "mod.pak").unlink()
    observe_mod_fs(mid, LEVEL_STATE, db=db)
    assert mid in seen
    card = cache.get_card_data(mid)
    assert card is not None
    assert card.content_status == CONTENT_CONTENT_MISSING


def test_reconcile_defers_projection_touch_then_drains(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = "810009"
    folder = _seed(library, db, mid, with_payload=False)
    db.update_mod_content_status(mid, content_status=CONTENT_HEALTHY)
    seen: list[str] = []
    subscribe_mod_changed(lambda x: seen.append(str(x)))

    begin_projection_defer()
    persist_evaluated_content_status(mid, folder, db=db, folder_present=True)
    assert seen == []
    end_projection_defer()
    pending = drain_projection_touch_ids()
    assert mid in pending
    notify_mod_changed(mid)
    assert mid in seen


def test_l1_l2_refuse_ui_thread(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])

    library = tmp_path / "mod"
    mid = "810010"
    folder = _seed(library, db, mid)

    set_ui_thread_guard(True)
    monkeypatch.setattr(
        QThread, "currentThread", classmethod(lambda cls: app.thread())
    )
    try:
        with pytest.raises(RuntimeError, match="UI thread"):
            observe_mod_fs(mid, LEVEL_STATE, db=db, managed_path=folder)
        with pytest.raises(RuntimeError, match="UI thread"):
            observe_mod_fs(mid, LEVEL_DEEP, db=db, managed_path=folder)
    finally:
        set_ui_thread_guard(False)


def test_deploy_finish_updates_observation_stamp(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid = "810011"
    folder = _seed(library, db, mid)
    before = str((db.get_mod_fs_observation(mid) or {}).get("fs_observed_at") or "")
    touch_observation_stamp(mid, db=db, managed_path=folder)
    after = str((db.get_mod_fs_observation(mid) or {}).get("fs_observed_at") or "")
    assert after
    assert after != before or before == ""


def test_no_workspace_identity_lookup_in_observer() -> None:
    import inspect

    import services.mod_fs_observer as mod

    src = inspect.getsource(mod.observe_mod_fs)
    src += inspect.getsource(mod._bound_root)
    src += inspect.getsource(mod.observe_mods_fs_batch)
    assert "find_mod_by_workspace" not in src
    assert "discover_folder_by_internal_id" not in src
    assert "ensure_mod_identity" not in src
    assert "create_mod_identity" not in src
