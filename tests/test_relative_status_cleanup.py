"""Relative Status cleanup — overlays live only under FILTER_DEPLOYMENT_RECORD."""

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
from ui.library_query import (
    FILTER_ALL,
    FILTER_DEPLOYMENT_RECORD,
    RECORD_STATUS_LABEL_EXTRA,
    RECORD_STATUS_LABEL_MISSING,
    ModFilterIndex,
    RecordRelativeStatus,
    compute_record_relative_status,
    record_relative_badge_label,
)


STARDEW = 413150
FORBIDDEN_COLS = ("extra_deployed", "record_missing", "relative_status", "record_status")


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "relative_status_cleanup.db")
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
        game_name="Stardew",
        favorite=False,
        deployed=deployed,
        has_offline=True,
        mtime=0.0,
        sort_name=f"mod {mod_id}",
    )


def _schema_cols(db: DatabaseManager, table: str) -> set[str]:
    with db._lock:
        return {str(r[1]) for r in db._conn.execute(f"PRAGMA table_info({table})")}


def _assert_no_relative_badge(card) -> None:
    assert getattr(card, "_record_relative", None) is None
    assert card.record_badge.isHidden()
    assert not str(card.record_badge.text() or "").strip()


def test_case1_record_filter_shows_extra_for_deployed_d(
    qapp, tmp_path: Path, db: DatabaseManager
) -> None:
    """Save ABC → enter Record Filter → D deployed → D shows extra."""
    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    _game(db)
    for mid in (1, 2, 3):
        _mod(db, mid, deployed=True)
    record = dr.create_or_update_record(STARDEW, "ABC", db=db)
    _mod(db, 4, deployed=True)

    view = ModLibraryView()
    cards = {
        "1": ModCardWidget(tmp_path / "a", None),
        "2": ModCardWidget(tmp_path / "b", None),
        "3": ModCardWidget(tmp_path / "c", None),
        "4": ModCardWidget(tmp_path / "d", None),
    }
    view._card_entries = [
        (_index("1", deployed=True), cards["1"]),
        (_index("2", deployed=True), cards["2"]),
        (_index("3", deployed=True), cards["3"]),
        (_index("4", deployed=True), cards["4"]),
    ]
    view._card_cache = dict(cards)
    view._set_library_status_filter(
        FILTER_DEPLOYMENT_RECORD,
        record_id=int(record.id),
        record_name=record.name,
    )
    view._cached_record_mod_ids = frozenset({"1", "2", "3"})
    view._last_filter_sig = None
    view._sync_record_overlays()

    assert cards["4"].record_badge.text() == RECORD_STATUS_LABEL_EXTRA
    assert not cards["4"].record_badge.isHidden()
    for mid in ("1", "2", "3"):
        assert record_relative_badge_label(cards[mid]._record_relative) is None


def test_case2_all_filter_clears_every_relative_badge(
    qapp, tmp_path: Path, db: DatabaseManager
) -> None:
    """Click 全部 → A/B/C/D all without relative badges."""
    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    _game(db)
    for mid in (1, 2, 3):
        _mod(db, mid, deployed=True)
    record = dr.create_or_update_record(STARDEW, "ABC", db=db)
    _mod(db, 4, deployed=True)

    view = ModLibraryView()
    cards = {
        mid: ModCardWidget(tmp_path / mid, None) for mid in ("1", "2", "3", "4")
    }
    view._card_entries = [
        (_index("1", deployed=True), cards["1"]),
        (_index("2", deployed=True), cards["2"]),
        (_index("3", deployed=False), cards["3"]),
        (_index("4", deployed=True), cards["4"]),
    ]
    view._card_cache = dict(cards)
    view._set_library_status_filter(
        FILTER_DEPLOYMENT_RECORD,
        record_id=int(record.id),
        record_name="ABC",
    )
    view._cached_record_mod_ids = frozenset({"1", "2", "3"})
    view._sync_record_overlays()
    assert cards["3"].record_badge.text() == RECORD_STATUS_LABEL_MISSING
    assert cards["4"].record_badge.text() == RECORD_STATUS_LABEL_EXTRA

    view._set_library_status_filter(FILTER_ALL)
    assert view._status_filter == FILTER_ALL
    assert view._deployment_record_id is None
    for card in cards.values():
        _assert_no_relative_badge(card)


def test_case3_restart_ordinary_list_has_no_badge(
    qapp, tmp_path: Path, db: DatabaseManager
) -> None:
    """Leave Record Filter / reopen app → ordinary list has no relative badge."""
    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    _game(db)
    for mid in (1, 2, 3):
        _mod(db, mid, deployed=True)
    record = dr.create_or_update_record(STARDEW, "ABC", db=db)
    _mod(db, 4, deployed=True)
    recorded = frozenset(dr.get_record_mod_ids(record.id, db=db))
    assert compute_record_relative_status(
        _index("4", deployed=True), recorded
    ).not_recorded_deployed

    for table in ("mods", "deployment_records", "deployment_record_items"):
        for name in FORBIDDEN_COLS:
            assert name not in _schema_cols(db, table)

    view2 = ModLibraryView()
    assert view2._status_filter == FILTER_ALL
    card = ModCardWidget(tmp_path / "fresh_d", None)
    view2._card_entries = [(_index("4", deployed=True), card)]
    view2._card_cache = {"4": card}
    view2._apply_view_filter()
    _assert_no_relative_badge(card)


def test_case4_reenter_record_filter_recomputes_extra(
    qapp, tmp_path: Path, db: DatabaseManager
) -> None:
    """Re-enter Record Filter → D shows extra again (recomputed, not cached)."""
    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    _game(db)
    for mid in (1, 2, 3):
        _mod(db, mid, deployed=True)
    record = dr.create_or_update_record(STARDEW, "ABC", db=db)
    _mod(db, 4, deployed=True)

    view = ModLibraryView()
    card_d = ModCardWidget(tmp_path / "d", None)
    view._card_entries = [
        (_index("1", deployed=True), ModCardWidget(tmp_path / "a", None)),
        (_index("2", deployed=True), ModCardWidget(tmp_path / "b", None)),
        (_index("3", deployed=True), ModCardWidget(tmp_path / "c", None)),
        (_index("4", deployed=True), card_d),
    ]
    view._card_cache = {str(i): c for i, (_idx, c) in enumerate(view._card_entries)}

    view._set_library_status_filter(
        FILTER_DEPLOYMENT_RECORD,
        record_id=int(record.id),
        record_name="ABC",
    )
    view._cached_record_mod_ids = frozenset({"1", "2", "3"})
    view._sync_record_overlays()
    assert card_d.record_badge.text() == RECORD_STATUS_LABEL_EXTRA

    view._set_library_status_filter(FILTER_ALL)
    _assert_no_relative_badge(card_d)

    # Wipe any leftover so re-entry must recompute from record∪deploy.
    card_d._record_relative = None
    card_d.record_badge.clear()
    card_d.record_badge.hide()

    view._set_library_status_filter(
        FILTER_DEPLOYMENT_RECORD,
        record_id=int(record.id),
        record_name="ABC",
    )
    view._cached_record_mod_ids = frozenset({"1", "2", "3"})
    view._sync_record_overlays()
    assert card_d.record_badge.text() == RECORD_STATUS_LABEL_EXTRA
    assert not card_d.record_badge.isHidden()
    assert card_d._record_relative is not None
    assert card_d._record_relative.not_recorded_deployed


def test_case5_card_reuse_does_not_carry_extra(
    qapp, tmp_path: Path
) -> None:
    """Same ModCard: bind D (extra) then rebind E → E has no extra."""
    from ui.mod_card import ModCardWidget

    card = ModCardWidget(tmp_path / "d", None)
    card.set_record_relative_status(
        RecordRelativeStatus(recorded=False, deployed=True)
    )
    assert card.record_badge.text() == RECORD_STATUS_LABEL_EXTRA
    assert not card.record_badge.isHidden()

    card.rebind(tmp_path / "e", ModMetadata(published_file_id="5", title="Mod E"))
    _assert_no_relative_badge(card)

    # refresh_display alone also must not resurrect a stale overlay.
    card.set_record_relative_status(
        RecordRelativeStatus(recorded=False, deployed=True)
    )
    card.refresh_display()
    _assert_no_relative_badge(card)


def test_apply_view_filter_early_return_still_clears_stale_overlay(
    qapp, tmp_path: Path
) -> None:
    """Same filter_sig early-return must still clear leftover overlays."""
    from ui.library_view import ModLibraryView
    from ui.mod_card import ModCardWidget

    view = ModLibraryView()
    card = ModCardWidget(tmp_path / "stale", None)
    view._status_filter = FILTER_ALL
    view._card_entries = [(_index("4", deployed=True), card)]
    view._card_cache = {"stale": card}
    view._apply_view_filter()
    # Bypass public setter to simulate leftover after a buggy path.
    card._record_relative = RecordRelativeStatus(recorded=False, deployed=True)
    card._render_record_badge()
    assert not card.record_badge.isHidden()

    view._apply_view_filter()
    _assert_no_relative_badge(card)
