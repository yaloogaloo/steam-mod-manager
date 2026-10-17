"""Presence Reconcile: Refresh gets MISS without click; one scan per game root."""

from __future__ import annotations

import inspect
import shutil
import time
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from services.identity_service import create_mod_identity, identity_create_scope
from services.metadata_backup import backup_root
from services.metadata_backup_sync import drain_backup_queue, sync_after_metadata_change
from services.mod_library_cache import get_library_cache, reset_library_cache
from services.mod_presence import ENTITY_LIVE, ENTITY_MISS, entity_state, has_valid_backup
from services.presence_reconcile import (
    drain_presence_reconcile,
    last_presence_stats,
    reconcile_presence,
    schedule_presence_reconcile,
)
from tests.helpers.identity import bind_managed_path, write_info_sidecar

APP_ID = 4242
GAME = "小丑牌"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "presence.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name=GAME, folder_name=GAME))
    yield manager
    drain_backup_queue(timeout=5.0)
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def _patch_get_db(db: DatabaseManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)


def _seed(
    db: DatabaseManager,
    *,
    library: Path,
    folder: str,
    workshop_id: str,
    title: str,
    with_backup: bool = False,
) -> tuple[Path, str, str]:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(workshop_id),
            workshop_id=str(workshop_id),
            title=title,
            app_id=APP_ID,
            game_name=GAME,
            operation="import",
        )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "")
    path = library / GAME / folder
    path.mkdir(parents=True)
    (path / "payload.txt").write_text("body", encoding="utf-8")
    write_info_sidecar(
        path,
        internal_id=frozen,
        title=title,
        external_id=str(workshop_id),
        workspace_id=str(created.workspace_id or workshop_id),
        app_id=APP_ID,
        game_name=GAME,
    )
    bind_managed_path(db, pk, path, game_name=GAME, title=title)
    db.update_mod_identity_fields(
        pk,
        workspace_id=str(created.workspace_id or workshop_id),
        last_known_path=str(path.resolve()),
        folder_present=True,
    )
    if with_backup:
        sync_after_metadata_change(pk, path, "edit", wait=True)
    return path, pk, frozen


def test_deleted_folder_is_miss_in_projection_without_selection(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, _frozen = _seed(
        db, library=library, folder="Alpha", workshop_id="88011", title="Alpha",
        with_backup=True,
    )
    reset_library_cache()
    cache = get_library_cache()
    cache.load_snapshot(library, force=True)
    assert cache.get_card_data(pk) is not None
    assert cache.get_card_data(pk).folder_absent is False

    shutil.rmtree(folder)
    stats = reconcile_presence(library, game_folder=GAME, notify=True)
    assert stats.miss >= 1
    assert pk in stats.changed_ids
    row = db.get_mod_backup_row(pk) or {}
    assert int(row.get("folder_present") or 0) == 0
    card = cache.get_card_data(pk)
    assert card is not None
    assert card.folder_absent is True
    assert entity_state(pk, db=db) == ENTITY_MISS


def test_rename_stays_live_and_updates_last_known_path(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _seed(
        db, library=library, folder="Religion", workshop_id="1178185727", title="Religion"
    )
    renamed = folder.parent / "Religion v2"
    folder.rename(renamed)
    stats = reconcile_presence(library, game_folder=GAME, notify=False)
    assert stats.rediscovered == 1
    assert stats.miss == 0
    row = db.get_mod_backup_row(pk) or {}
    assert Path(str(row.get("last_known_path") or "")).resolve() == renamed.resolve()
    assert int(row.get("folder_present") or 0) == 1
    assert str(row.get("internal_id") or "") == frozen
    assert entity_state(pk, db=db) == ENTITY_LIVE


def test_batch_same_game_one_root_scan(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    seeded: list[tuple[Path, str]] = []
    for i in range(40):
        path, pk, _frozen = _seed(
            db,
            library=library,
            folder=f"Mod{i:03d}",
            workshop_id=str(900000 + i),
            title=f"Mod{i:03d}",
        )
        seeded.append((path, pk))
    for path, _pk in seeded:
        shutil.rmtree(path)
    t0 = time.perf_counter()
    stats = reconcile_presence(library, game_folder=GAME, notify=False)
    duration_ms = (time.perf_counter() - t0) * 1000.0
    assert stats.mods_examined == 40
    assert stats.game_roots_examined == 1
    assert stats.directory_scans == 1
    assert stats.miss == 40
    assert stats.rediscovered == 0
    for _path, pk in seeded:
        assert int((db.get_mod_backup_row(pk) or {}).get("folder_present") or 0) == 0
    print(
        "batch_presence "
        f"examined={stats.mods_examined} roots={stats.game_roots_examined} "
        f"scans={stats.directory_scans} stat_calls={stats.filesystem_stat_calls} "
        f"info_reads={stats.info_reads} duration_ms={stats.duration_ms:.1f} "
        f"wall_ms={duration_ms:.1f} ui_blocking_ms={stats.ui_blocking_ms:.1f}"
    )


def test_second_reconcile_keeps_miss_and_live(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    live_path, live_pk, _a = _seed(
        db, library=library, folder="Keep", workshop_id="88021", title="Keep"
    )
    gone_path, gone_pk, _b = _seed(
        db, library=library, folder="Gone", workshop_id="88022", title="Gone",
        with_backup=True,
    )
    shutil.rmtree(gone_path)
    first = reconcile_presence(library, game_folder=GAME, notify=False)
    assert first.directory_scans == 1
    second = reconcile_presence(library, game_folder=GAME, notify=False)
    assert second.directory_scans == 1
    assert second.info_reads <= first.info_reads
    assert int((db.get_mod_backup_row(live_pk) or {}).get("folder_present") or 0) == 1
    assert int((db.get_mod_backup_row(gone_pk) or {}).get("folder_present") or 0) == 0
    assert live_path.is_dir()
    assert not gone_path.exists()


def test_miss_keeps_backup_takeover(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder, pk, _frozen = _seed(
        db, library=library, folder="Bak", workshop_id="88031", title="Bak",
        with_backup=True,
    )
    assert (backup_root(pk) / "metadata.json").is_file()
    shutil.rmtree(folder)
    reconcile_presence(library, game_folder=GAME, notify=False)
    assert entity_state(pk, db=db) == ENTITY_MISS
    assert has_valid_backup(pk, db=db) is True


def test_schedule_does_not_block_like_linear_stat_walk(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    for i in range(24):
        _seed(
            db,
            library=library,
            folder=f"S{i:02d}",
            workshop_id=str(910000 + i),
            title=f"S{i:02d}",
        )
    t0 = time.perf_counter()
    started = schedule_presence_reconcile(library, game_folder=GAME, db=db)
    ui_ms = (time.perf_counter() - t0) * 1000.0
    assert started is True
    assert ui_ms < 50.0
    drain_presence_reconcile(timeout=20.0)
    stats = last_presence_stats()
    assert stats["mods_examined"] == 24
    assert stats["directory_scans"] == 1
    assert stats["ui_blocking_ms"] < 50.0


def test_library_refresh_schedules_presence_not_identity_reconcile() -> None:
    from ui.library_view import ModLibraryView

    src = inspect.getsource(ModLibraryView.refresh)
    assert "schedule_presence_reconcile" in src
    assert "reconcile_library(" not in src
    assert "start_reconcile" not in src


def test_card_rebind_does_not_scan_presence() -> None:
    from ui.mod_card import ModCardWidget

    src = inspect.getsource(ModCardWidget.rebind)
    assert "reconcile_presence" not in src
    assert "observe_mod_fs" not in src
    assert "path.exists" not in src
    assert ".is_dir(" not in src
    cover = inspect.getsource(ModCardWidget._cover_token_for_current)
    assert ".exists(" not in cover
    menu = inspect.getsource(ModCardWidget._build_context_menu)
    assert "presence_projection" not in menu


def test_detail_show_does_not_detect_miss() -> None:
    from ui.mod_detail_panel import ModDetailPanel

    src = inspect.getsource(ModDetailPanel)
    assert "_schedule_fs_observation_on_show" not in src
    assert "observe_mod_fs" not in src
    show = inspect.getsource(ModDetailPanel.show_mod)
    assert "reconcile_presence" not in show
    assert "mark_missing" not in show


def test_resolver_does_not_rediscover() -> None:
    from services.mod_metadata_resolver import ModMetadataResolver

    src = inspect.getsource(ModMetadataResolver._resolve_identity)
    assert "resolve_managed_folder" not in src
    assert "rediscover_entity_path" not in src


def test_restored_folder_clears_stale_content_missing(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """MISS → restore → Presence Refresh clears content_missing without Detail."""
    from services.content_status_eval import persist_evaluated_content_status
    from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY

    library = tmp_path / "mod"
    folder, pk, _frozen = _seed(
        db,
        library=library,
        folder="Restored",
        workshop_id="88041",
        title="Restored",
        with_backup=True,
    )
    shutil.rmtree(folder)
    persist_evaluated_content_status(
        pk, None, db=db, folder_present=False, sync_sticky_marker=False
    )
    db.set_mod_folder_present(pk, present=False)
    row = db.get_mod_backup_row(pk) or {}
    assert int(row.get("folder_present") or 0) == 0
    assert str(row.get("content_status") or "") == CONTENT_CONTENT_MISSING

    folder.mkdir(parents=True)
    (folder / "payload.txt").write_text("body", encoding="utf-8")
    write_info_sidecar(
        folder,
        internal_id=str(row.get("internal_id") or ""),
        title="Restored",
        external_id="88041",
        workspace_id=str(row.get("workspace_id") or "88041"),
        app_id=APP_ID,
        game_name=GAME,
    )

    stats = reconcile_presence(library, game_folder=GAME, notify=False, db=db)
    assert stats.content_reevaluated >= 1
    assert stats.content_status_cleared >= 1
    after = db.get_mod_backup_row(pk) or {}
    assert int(after.get("folder_present") or 0) == 1
    assert str(after.get("content_status") or "") == CONTENT_HEALTHY
    assert entity_state(pk, db=db) == ENTITY_LIVE


def test_batch_restore_clears_content_missing_for_twelve(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from services.content_status_eval import persist_evaluated_content_status
    from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY

    library = tmp_path / "mod"
    seeds: list[tuple[Path, str, str]] = []
    for i in range(12):
        path, pk, frozen = _seed(
            db,
            library=library,
            folder=f"Batch{i:02d}",
            workshop_id=str(920000 + i),
            title=f"Batch{i:02d}",
            with_backup=True,
        )
        seeds.append((path, pk, frozen))

    for path, pk, _frozen in seeds:
        shutil.rmtree(path)
        persist_evaluated_content_status(
            pk, None, db=db, folder_present=False, sync_sticky_marker=False
        )
        db.set_mod_folder_present(pk, present=False)

    for path, pk, frozen in seeds:
        path.mkdir(parents=True)
        (path / "payload.txt").write_text("body", encoding="utf-8")
        write_info_sidecar(
            path,
            internal_id=frozen,
            title=path.name,
            external_id=str((db.get_mod_backup_row(pk) or {}).get("workspace_id") or ""),
            workspace_id=str((db.get_mod_backup_row(pk) or {}).get("workspace_id") or ""),
            app_id=APP_ID,
            game_name=GAME,
        )

    stats = reconcile_presence(library, game_folder=GAME, notify=False, db=db)
    assert stats.content_status_cleared >= 12
    for _path, pk, _frozen in seeds:
        row = db.get_mod_backup_row(pk) or {}
        assert int(row.get("folder_present") or 0) == 1
        assert str(row.get("content_status") or "") == CONTENT_HEALTHY
        assert str(row.get("content_status") or "") != CONTENT_CONTENT_MISSING


def test_true_empty_payload_keeps_content_missing_after_presence(
    db: DatabaseManager, tmp_path: Path
) -> None:
    from services.library_status import CONTENT_CONTENT_MISSING, normalize_content_axis

    library = tmp_path / "mod"
    folder, pk, frozen = _seed(
        db,
        library=library,
        folder="EmptyPayload",
        workshop_id="88051",
        title="EmptyPayload",
    )
    # Remove payload; keep only .info so folder is LIVE but content missing.
    for child in list(folder.iterdir()):
        if child.name == ".info":
            continue
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)
    db.update_mod_content_status(
        pk,
        content_status=CONTENT_CONTENT_MISSING,
        folder_present=True,
    )
    assert str((db.get_mod_backup_row(pk) or {}).get("content_status") or "") == (
        CONTENT_CONTENT_MISSING
    )
    key_rows = [r for r in db.iter_mod_backup_key_rows() if r["mod_id"] == pk]
    assert key_rows, "iter_mod_backup_key_rows must include seeded mod"
    assert normalize_content_axis(key_rows[0].get("content_status")) == (
        CONTENT_CONTENT_MISSING
    ), key_rows[0]

    stats = reconcile_presence(library, game_folder=GAME, notify=False, db=db)
    assert stats.content_reevaluated >= 1, stats.as_dict()
    assert stats.content_status_cleared == 0, stats.as_dict()
    after = db.get_mod_backup_row(pk) or {}
    assert int(after.get("folder_present") or 0) == 1
    assert str(after.get("content_status") or "") == CONTENT_CONTENT_MISSING
    assert str(after.get("internal_id") or "") == frozen


def test_stale_live_content_missing_healed_without_miss_transition(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """folder_present=1 + stale content_missing cleared on Presence pass."""
    from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY

    library = tmp_path / "mod"
    folder, pk, _frozen = _seed(
        db,
        library=library,
        folder="StaleLive",
        workshop_id="88061",
        title="StaleLive",
    )
    db.update_mod_content_status(
        pk,
        content_status=CONTENT_CONTENT_MISSING,
        folder_present=True,
    )
    assert str((db.get_mod_backup_row(pk) or {}).get("content_status") or "") == (
        CONTENT_CONTENT_MISSING
    )
    stats = reconcile_presence(library, game_folder=GAME, notify=False, db=db)
    assert stats.content_status_cleared >= 1
    assert str((db.get_mod_backup_row(pk) or {}).get("content_status") or "") == (
        CONTENT_HEALTHY
    )
    assert folder.is_dir()
