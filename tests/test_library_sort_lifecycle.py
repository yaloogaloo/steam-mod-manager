"""Library sort lifecycle — Query owns order; equal mtime must not collapse to name.

Covers DB-first Layer-1 after Library lifecycle refactor:

Sort State → Query → Repository projection → ModListItem → Viewport order

Does **not** require UI rebuild / FS scan / reconcile.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import (
    DatabaseManager,
    updated_at_to_mtime,
)
from services.mod_library_cache import (
    list_item_to_card_data,
    mod_list_item_from_row,
    reset_library_cache,
)
from tests.helpers.identity import prove_managed_folder, create_steam_test_mod
from ui.library_query import (
    SORT_MTIME,
    SORT_NAME,
    SORT_NAME_DESC,
    ModFilterIndex,
    filter_sort_entries,
    sort_key,
)
from ui.library_viewport import compute_viewport_window


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    reset_library_cache()
    manager = DatabaseManager(tmp_path / "sort_lifecycle.db")
    from core.game_info import GameInfo

    manager.upsert_game(GameInfo(app_id=42, name="GameX", folder_name="GameX"))
    yield manager
    manager.close()
    DatabaseManager.reset_instance()
    reset_library_cache()


def _iso(offset_seconds: int) -> str:
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_seconds)).replace(microsecond=0).isoformat()


def _seed_mod(
    db: DatabaseManager,
    library: Path,
    *,
    workshop_id: str,
    title: str,
    updated_at: str,
    display_name: str = "",
) -> str:
    """Create Steam entity + folder proof. Returns mods.mod_id PK."""
    created = create_steam_test_mod(
        db, external_id=workshop_id, title=title, app_id=42, game_name="GameX"
    )
    pk = str(created.mod_id)
    folder = library / "GameX" / title
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "a.txt").write_text("x", encoding="utf-8")
    prove_managed_folder(
        db,
        folder,
        handle=pk,
        title=title,
        app_id=42,
        game_name="GameX",
    )
    db._conn.execute(
        "UPDATE mods SET last_known_path = ?, folder_present = 1, "
        "display_name = ?, updated_at = ? WHERE mod_id = ?",
        (str(folder), display_name, updated_at, int(pk)),
    )
    db._conn.commit()
    return pk


def _row_id(row: dict) -> str:
    """Layer-1 session key is SQLite PK (projection field still named internal_id)."""
    return str(row.get("internal_id") or "")


def _index_from_row(row: dict) -> ModFilterIndex:
    name = str(row.get("name") or "")
    return ModFilterIndex(
        mod_id=_row_id(row),
        display_name=name,
        steam_name=str(row.get("steam_name") or name),
        notes=str(row.get("notes_preview") or ""),
        game_name=str(row.get("game_folder") or ""),
        favorite=bool(row.get("favorite")),
        deployed=bool(row.get("deployed")),
        has_offline=bool(row.get("has_offline")),
        mtime=float(row.get("mtime") or 0.0),
        sort_name=name,
        workspace_id=str(row.get("workspace_id") or ""),
    )


def _entries(db: DatabaseManager) -> list[tuple[ModFilterIndex, dict]]:
    return [(_index_from_row(r), r) for r in db.list_mod_list_items(game_id=42)]


def test_name_asc_and_desc_change_order(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    stamp = _iso(50)
    pk_charlie = _seed_mod(
        db, library, workshop_id="8001", title="Charlie", updated_at=stamp
    )
    pk_alpha = _seed_mod(
        db, library, workshop_id="8002", title="Alpha", updated_at=stamp
    )
    pk_bravo = _seed_mod(
        db, library, workshop_id="8003", title="Bravo", updated_at=stamp
    )

    entries = _entries(db)
    asc = [_row_id(p) for _i, p in filter_sort_entries(entries, sort_mode=SORT_NAME)]
    desc = [
        _row_id(p)
        for _i, p in filter_sort_entries(entries, sort_mode=SORT_NAME_DESC)
    ]
    assert asc == [pk_alpha, pk_bravo, pk_charlie]
    assert desc == [pk_charlie, pk_bravo, pk_alpha]
    assert asc != desc


def test_mtime_sort_differs_from_name_when_times_equal(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Regression: equal updated_at must not make SORT_MTIME == SORT_NAME."""
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    stamp = _iso(10)
    pk_zulu = _seed_mod(
        db, library, workshop_id="8101", title="Zulu", updated_at=stamp
    )
    pk_alpha = _seed_mod(
        db, library, workshop_id="8102", title="Alpha", updated_at=stamp
    )
    pk_mike = _seed_mod(
        db, library, workshop_id="8103", title="Mike", updated_at=stamp
    )

    entries = _entries(db)
    by_name = [
        _row_id(p) for _i, p in filter_sort_entries(entries, sort_mode=SORT_NAME)
    ]
    by_mtime = [
        _row_id(p) for _i, p in filter_sort_entries(entries, sort_mode=SORT_MTIME)
    ]
    assert by_name == [pk_alpha, pk_mike, pk_zulu]
    # Tie-break is mod_id DESC, not name — so modes differ.
    assert by_mtime != by_name
    assert by_mtime == [pk_mike, pk_alpha, pk_zulu]


def test_recent_modified_orders_by_updated_at(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    pk_old = _seed_mod(
        db, library, workshop_id="8201", title="Old", updated_at=_iso(10)
    )
    pk_mid = _seed_mod(
        db, library, workshop_id="8202", title="Mid", updated_at=_iso(20)
    )
    pk_new = _seed_mod(
        db, library, workshop_id="8203", title="New", updated_at=_iso(99)
    )

    ordered = filter_sort_entries(_entries(db), sort_mode=SORT_MTIME)
    assert [_row_id(p) for _i, p in ordered] == [pk_new, pk_mid, pk_old]


def test_sort_does_not_scan_filesystem_or_reconcile(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    _seed_mod(db, library, workshop_id="8301", title="A", updated_at=_iso(1))
    _seed_mod(db, library, workshop_id="8302", title="B", updated_at=_iso(2))

    fs_calls: list[str] = []
    real_stat = Path.stat
    real_iterdir = Path.iterdir

    def track_stat(self, *args, **kwargs):  # noqa: ANN001
        fs_calls.append(f"stat:{self}")
        return real_stat(self, *args, **kwargs)

    def track_iterdir(self, *args, **kwargs):  # noqa: ANN001
        fs_calls.append(f"iterdir:{self}")
        return real_iterdir(self, *args, **kwargs)

    reconcile = MagicMock(side_effect=AssertionError("reconcile forbidden"))
    monkeypatch.setattr(Path, "stat", track_stat)
    monkeypatch.setattr(Path, "iterdir", track_iterdir)
    monkeypatch.setattr(
        "services.library_reconcile.reconcile_library", reconcile, raising=False
    )

    rows = db.list_mod_list_items(game_id=42)
    entries = [(_index_from_row(r), r) for r in rows]
    filter_sort_entries(entries, sort_mode=SORT_NAME)
    filter_sort_entries(entries, sort_mode=SORT_MTIME)
    assert fs_calls == []
    reconcile.assert_not_called()


def test_sort_10k_performance(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    # Lightweight inserts — skip full folder tree for scale.
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    with db._lock:
        for i in range(10_000):
            mid = 700_000 + i
            stamp = (now + timedelta(seconds=i % 500)).replace(microsecond=0).isoformat()
            title = f"Mod-{i:05d}"
            db._conn.execute(
                """
                INSERT INTO mods (
                    mod_id, app_id, title, display_name, updated_at,
                    folder_present, last_known_path, workspace_id, platform
                ) VALUES (?, 42, ?, ?, ?, 1, ?, ?, 'steam')
                """,
                (
                    mid,
                    title,
                    title,
                    stamp,
                    str(library / "GameX" / title),
                    str(mid),
                ),
            )
        db._conn.commit()

    t0 = time.perf_counter()
    rows = db.list_mod_list_items(game_id=42)
    t_repo = time.perf_counter() - t0
    assert len(rows) == 10_000

    entries = [(_index_from_row(r), r) for r in rows]
    t1 = time.perf_counter()
    name_order = filter_sort_entries(entries, sort_mode=SORT_NAME)
    mtime_order = filter_sort_entries(entries, sort_mode=SORT_MTIME)
    t_query = time.perf_counter() - t1

    assert len(name_order) == 10_000
    assert [_row_id(p) for _i, p in name_order] != [
        _row_id(p) for _i, p in mtime_order
    ]
    assert t_query < 1.5, f"query sort too slow: {t_query:.3f}s"
    assert t_repo < 3.0, f"repo list too slow: {t_repo:.3f}s"


def test_viewport_preserves_query_order_after_resort(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    for mid, title, bump in (
        ("8401", "Zulu", 10),
        ("8402", "Alpha", 20),
        ("8403", "Mike", 30),
        ("8404", "Bravo", 40),
        ("8405", "Delta", 50),
    ):
        _seed_mod(
            db, library, workshop_id=mid, title=title, updated_at=_iso(bump)
        )

    entries = _entries(db)
    name_rows = filter_sort_entries(entries, sort_mode=SORT_NAME)
    mtime_rows = filter_sort_entries(entries, sort_mode=SORT_MTIME)
    assert [_row_id(p) for _i, p in name_rows] != [
        _row_id(p) for _i, p in mtime_rows
    ]

    for sorted_rows in (name_rows, mtime_rows):
        query_ids = [_row_id(p) for _i, p in sorted_rows]
        window = compute_viewport_window(
            item_count=len(sorted_rows),
            scroll_y=0,
            viewport_height=2000,
            viewport_width=1200,
        )
        slice_ids = [
            _row_id(sorted_rows[i][1])
            for i in range(window.first_index, window.last_index)
        ]
        assert slice_ids == query_ids[window.first_index : window.last_index]


def test_status_recovery_does_not_bump_sort_mtime(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    pk = _seed_mod(
        db, library, workshop_id="8501", title="Keep", updated_at=_iso(40)
    )
    before = db._conn.execute(
        "SELECT updated_at FROM mods WHERE mod_id = ?",
        (int(pk),),
    ).fetchone()["updated_at"]

    db.update_mod_content_status(
        pk,
        content_status="healthy",
        touch_updated_at=False,
    )
    db.update_mod_identity_fields(pk, identity_status="identity_conflict")

    after = db._conn.execute(
        "SELECT updated_at, identity_status FROM mods WHERE mod_id = ?",
        (int(pk),),
    ).fetchone()
    assert after["updated_at"] == before
    assert after["identity_status"] == "identity_conflict"


def test_list_item_projection_feeds_query_mtime(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    _seed_mod(db, library, workshop_id="8601", title="Snap", updated_at=_iso(77))
    row = db.list_mod_list_items(game_id=42)[0]
    item = mod_list_item_from_row(row)
    card = list_item_to_card_data(item)
    expected = updated_at_to_mtime(_iso(77))
    assert item.mtime == expected
    assert card.updated_time == expected
    assert sort_key(_index_from_row(row), SORT_MTIME)[0] == -expected
