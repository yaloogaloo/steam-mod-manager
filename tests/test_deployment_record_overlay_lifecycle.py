"""Deployment Record overlay lifecycle — survive viewport rebind / scroll.

Relative badges (记录缺失 / 额外部署) are memory-only. ModCard.rebind clears
them; LibraryView must restore after every viewport bind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.game_info import GameInfo
from core.models import ModMetadata
from services import deployment_record as dr
from services.mod_library_cache import ModCardData
from ui.library_query import (
    FILTER_ALL,
    FILTER_DEPLOYMENT_RECORD,
    RECORD_STATUS_LABEL_EXTRA,
    RECORD_STATUS_LABEL_MISSING,
    ModFilterIndex,
    record_relative_badge_label,
)


STARDEW = 413150
FORBIDDEN_COLS = ("extra_deployed", "record_missing", "relative_status", "record_status")


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "overlay_lifecycle.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _game(db: DatabaseManager) -> None:
    db.upsert_game(
        GameInfo(app_id=STARDEW, name="Stardew Valley", folder_name="Stardew Valley")
    )


def _mod(db: DatabaseManager, mod_id: int, *, deployed: bool) -> None:
    db.upsert_mod(
        ModMetadata(
            published_file_id=str(mod_id),
            title=f"Mod {mod_id}",
            app_id=STARDEW,
        )
    )
    db.update_mod_deploy_status(
        mod_id,
        deploy_status=(
            DEPLOY_STATUS_DEPLOYED if deployed else DEPLOY_STATUS_NOT_DEPLOYED
        ),
        deploy_path="" if not deployed else f"/fake/{mod_id}",
    )


def _index(mod_id: str, *, deployed: bool) -> ModFilterIndex:
    return ModFilterIndex(
        mod_id=mod_id,
        display_name=f"Mod {mod_id}",
        steam_name="",
        notes="",
        game_name="Stardew Valley",
        favorite=False,
        deployed=deployed,
        has_offline=False,
        mtime=0.0,
        sort_name=f"mod {mod_id}",
    )


def _card_data(mod_id: str, folder: Path, *, deployed: bool) -> ModCardData:
    return ModCardData(
        id=mod_id,
        title=f"Mod {mod_id}",
        platform="steam",
        cover="",
        description="",
        tags="",
        size=0,
        updated_time=0.0,
        managed_path=str(folder),
        game_folder="Stardew Valley",
        deployed=deployed,
        deploy_status=(
            DEPLOY_STATUS_DEPLOYED if deployed else DEPLOY_STATUS_NOT_DEPLOYED
        ),
    )


def _assert_no_relative_badge(card) -> None:
    assert getattr(card, "_record_relative", None) is None
    assert card.record_badge.isHidden()
    assert not str(card.record_badge.text() or "").strip()


def _schema_cols(db: DatabaseManager, table: str) -> set[str]:
    with db._lock:
        return {str(r[1]) for r in db._conn.execute(f"PRAGMA table_info({table})")}


def test_missing_overlay_survives_viewport_rebind(
    qapp, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    """recorded=True, deployed=False → 记录缺失 survives _sync_viewport_cards."""
    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    _game(db)
    _mod(db, 1, deployed=False)
    record = dr.create_or_update_record(STARDEW, "SaveA", mod_ids=[1], db=db)
    monkeypatch.setattr("services.deployment_record.get_db", lambda: db)

    folder = tmp_path / "m1"
    folder.mkdir()
    card = ModCardWidget(folder, None)
    data = _card_data("1", folder, deployed=False)
    index = _index("1", deployed=False)

    view = ModLibraryView()
    view._status_filter = FILTER_DEPLOYMENT_RECORD
    view._deployment_record_id = int(record.id)
    view._deployment_record_name = record.name
    view._cached_record_mod_ids = frozenset({"1"})
    view._filtered_row_entries = [(index, data)]
    view._card_cache = {view._card_cache_key(folder, mod_id="1"): card}
    view._card_entries = [(index, card)]

    view._sync_record_overlays()
    assert card.record_badge.text() == RECORD_STATUS_LABEL_MISSING
    assert not card.record_badge.isHidden()

    # Viewport rebind path (scroll / filter refresh) clears then restores.
    view._sync_viewport_cards()
    assert len(view._card_entries) == 1
    bound = view._card_entries[0][1]
    assert bound.record_badge.text() == RECORD_STATUS_LABEL_MISSING
    assert not bound.record_badge.isHidden()
    assert bound._record_relative is not None
    assert bound._record_relative.recorded_not_deployed


def test_extra_overlay_survives_viewport_rebind(
    qapp, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    """recorded=False, deployed=True → 额外部署 survives viewport rebind."""
    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    _game(db)
    _mod(db, 1, deployed=True)
    _mod(db, 9, deployed=True)
    record = dr.create_or_update_record(STARDEW, "SaveB", mod_ids=[1], db=db)
    monkeypatch.setattr("services.deployment_record.get_db", lambda: db)

    folder = tmp_path / "m9"
    folder.mkdir()
    card = ModCardWidget(folder, None)
    data = _card_data("9", folder, deployed=True)
    index = _index("9", deployed=True)

    view = ModLibraryView()
    view._status_filter = FILTER_DEPLOYMENT_RECORD
    view._deployment_record_id = int(record.id)
    view._deployment_record_name = record.name
    view._cached_record_mod_ids = frozenset({"1"})
    view._filtered_row_entries = [(index, data)]
    view._card_cache = {view._card_cache_key(folder, mod_id="9"): card}
    view._card_entries = [(index, card)]

    view._sync_record_overlays()
    assert card.record_badge.text() == RECORD_STATUS_LABEL_EXTRA

    view._sync_viewport_cards()
    bound = view._card_entries[0][1]
    assert bound.record_badge.text() == RECORD_STATUS_LABEL_EXTRA
    assert not bound.record_badge.isHidden()
    assert bound._record_relative.not_recorded_deployed


def test_exit_record_filter_clears_overlay(
    qapp, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    _game(db)
    _mod(db, 1, deployed=False)
    record = dr.create_or_update_record(STARDEW, "SaveC", mod_ids=[1], db=db)
    monkeypatch.setattr("services.deployment_record.get_db", lambda: db)

    folder = tmp_path / "m1"
    folder.mkdir()
    card = ModCardWidget(folder, None)
    data = _card_data("1", folder, deployed=False)
    index = _index("1", deployed=False)

    view = ModLibraryView()
    view._game_row_entries = [(index, data)]
    view._filtered_row_entries = [(index, data)]
    view._card_entries = [(index, card)]
    view._card_cache = {view._card_cache_key(folder, mod_id="1"): card}
    view._set_library_status_filter(
        FILTER_DEPLOYMENT_RECORD,
        record_id=int(record.id),
        record_name=record.name,
    )
    view._cached_record_mod_ids = frozenset({"1"})
    view._filtered_row_entries = [(index, data)]
    view._sync_viewport_cards()
    assert view._card_entries[0][1].record_badge.text() == RECORD_STATUS_LABEL_MISSING

    view._set_library_status_filter(FILTER_ALL)
    # Leaving record filter clears overlays even if cards are only in cache.
    for c in list(view._card_cache.values()):
        _assert_no_relative_badge(c)
    if view._card_entries:
        for _idx, c in view._card_entries:
            _assert_no_relative_badge(c)
    else:
        _assert_no_relative_badge(card)


def test_reenter_record_filter_recomputes_overlay(
    qapp, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    _game(db)
    _mod(db, 1, deployed=True)
    _mod(db, 2, deployed=True)
    record = dr.create_or_update_record(STARDEW, "SaveD", mod_ids=[1], db=db)
    monkeypatch.setattr("services.deployment_record.get_db", lambda: db)

    folder = tmp_path / "m2"
    folder.mkdir()
    card = ModCardWidget(folder, None)
    data = _card_data("2", folder, deployed=True)
    index = _index("2", deployed=True)

    view = ModLibraryView()
    view._filtered_row_entries = [(index, data)]
    view._card_cache = {view._card_cache_key(folder, mod_id="2"): card}
    view._card_entries = [(index, card)]
    view._game_row_entries = [(index, data)]

    view._set_library_status_filter(
        FILTER_DEPLOYMENT_RECORD,
        record_id=int(record.id),
        record_name=record.name,
    )
    view._cached_record_mod_ids = frozenset({"1"})
    # _set_library_status_filter → _apply_view_filter may empty viewport without
    # real snapshot rows; drive the bind path explicitly.
    view._filtered_row_entries = [(index, data)]
    view._sync_viewport_cards()
    assert card.record_badge.text() == RECORD_STATUS_LABEL_EXTRA

    view._set_library_status_filter(FILTER_ALL)
    _assert_no_relative_badge(card)

    view._status_filter = FILTER_DEPLOYMENT_RECORD
    view._deployment_record_id = int(record.id)
    view._cached_record_mod_ids = frozenset({"1"})
    view._filtered_row_entries = [(index, data)]
    view._card_cache = {view._card_cache_key(folder, mod_id="2"): card}
    view._sync_viewport_cards()
    bound = view._card_entries[0][1]
    assert bound.record_badge.text() == RECORD_STATUS_LABEL_EXTRA
    assert not bound.record_badge.isHidden()


def test_card_reuse_does_not_leak_overlay_across_mods(
    qapp, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    """Same card widget rebound to another mod must not keep prior badge."""
    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    _game(db)
    _mod(db, 1, deployed=True)
    record = dr.create_or_update_record(STARDEW, "SaveE", mod_ids=[1], db=db)
    monkeypatch.setattr("services.deployment_record.get_db", lambda: db)

    folder_extra = tmp_path / "extra"
    folder_extra.mkdir()
    folder_match = tmp_path / "match"
    folder_match.mkdir()

    card = ModCardWidget(folder_extra, None)
    extra_index = _index("9", deployed=True)
    extra_data = _card_data("9", folder_extra, deployed=True)
    match_index = _index("1", deployed=True)
    match_data = _card_data("1", folder_match, deployed=True)

    view = ModLibraryView()
    view._status_filter = FILTER_DEPLOYMENT_RECORD
    view._deployment_record_id = int(record.id)
    view._cached_record_mod_ids = frozenset({"1"})
    view._filtered_row_entries = [(extra_index, extra_data)]
    view._card_cache = {view._card_cache_key(folder_extra, mod_id="9"): card}
    view._sync_viewport_cards()
    assert view._card_entries[0][1].record_badge.text() == RECORD_STATUS_LABEL_EXTRA

    # Rebind viewport to the recorded+deployed mod (no badge).
    view._filtered_row_entries = [(match_index, match_data)]
    view._card_cache = {
        view._card_cache_key(folder_match, mod_id="1"): card,
    }
    view._sync_viewport_cards()
    bound = view._card_entries[0][1]
    # Recorded + deployed → no temporary badge (matched).
    assert bound.record_badge.isHidden()
    assert not str(bound.record_badge.text() or "").strip()
    assert record_relative_badge_label(bound._record_relative) is None


def test_no_relative_columns_in_schema(db: DatabaseManager) -> None:
    for table in ("mods", "deployment_records", "deployment_record_items"):
        cols = _schema_cols(db, table)
        assert not cols & set(FORBIDDEN_COLS)


def test_apply_view_filter_order_restores_after_viewport(
    qapp, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    """_apply_view_filter must not leave overlays wiped by viewport rebind."""
    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    _game(db)
    _mod(db, 3, deployed=False)
    record = dr.create_or_update_record(STARDEW, "SaveF", mod_ids=[3], db=db)
    monkeypatch.setattr("services.deployment_record.get_db", lambda: db)

    folder = tmp_path / "m3"
    folder.mkdir()
    card = ModCardWidget(folder, None)
    data = _card_data("3", folder, deployed=False)
    index = _index("3", deployed=False)

    view = ModLibraryView()
    view._game_row_entries = [(index, data)]
    view._filtered_row_entries = [(index, data)]
    view._card_cache = {view._card_cache_key(folder, mod_id="3"): card}
    view._card_entries = [(index, card)]
    view._status_filter = FILTER_DEPLOYMENT_RECORD
    view._deployment_record_id = int(record.id)
    view._deployment_record_name = record.name
    view._cached_record_mod_ids = frozenset({"3"})
    view._last_filter_sig = None

    view._apply_view_filter()
    # After full filter+viewport path, missing badge must still be present.
    assert view._card_entries, "viewport should bind at least one card"
    bound = view._card_entries[0][1]
    assert bound.record_badge.text() == RECORD_STATUS_LABEL_MISSING
    assert not bound.record_badge.isHidden()
