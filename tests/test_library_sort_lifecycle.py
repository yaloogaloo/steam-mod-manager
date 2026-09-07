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
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.mod_library_cache import (
    list_item_to_card_data,
    mod_list_item_from_row,
    reset_library_cache,
)
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
    mid: str,
    title: str,
    updated_at: str,
    display_name: str = "",
) -> Path:
    folder = library / "GameX" / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (folder / "a.txt").write_text("x", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "{title}",\n'
        '  "app_id": 42,\n'
        '  "game_name": "GameX"\n'
        "}\n",
        encoding="utf-8",
    )
    db.upsert_mod(ModMetadata(published_file_id=mid, title=title, app_id=42))
    db._conn.execute(
        "UPDATE mods SET last_known_path = ?, folder_present = 1, "
        "display_name = ?, updated_at = ? WHERE mod_id = ?",
        (str(folder), display_name, updated_at, int(mid)),
    )
    db._conn.commit()
    return folder


def _index_from_row(row: dict) -> ModFilterIndex:
    name = str(row.get("name") or "")
    return ModFilterIndex(
        internal_id=str(row.get("internal_id") or ""),
        display_name=name,
        steam_name=str(row.get("steam_name") or name),
        notes=str(row.get("notes_preview") or ""),
        game_name=str(row.get("game_folder") or ""),
        favorite=bool(row.get("favorite")),
        deployed=bool(row.get("deployed")),
        has_offline=bool(row.get("has_offline")),
        mtime=float(row.get("mtime") or 0.0),
        sort_name=name,
    )


def _entries(db: DatabaseManager) -> list[tuple[ModFilterIndex, dict]]:
    return [(_index_from_row(r), r) for r in db.list_mod_list_items(game_id=42)]


def test_name_asc_and_desc_change_order(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    # Identical mtime — previously made name==mtime; ASC/DESC must still differ.
    stamp = _iso(50)
    _seed_mod(db, library, mid="8001", title="Charlie", updated_at=stamp)
    _seed_mod(db, library, mid="8002", title="Alpha", updated_at=stamp)
    _seed_mod(db, library, mid="8003", title="Bravo", updated_at=stamp)

    entries = _entries(db)
    asc = [p["internal_id"] for _i, p in filter_sort_entries(entries, sort_mode=SORT_NAME)]
    desc = [
        p["internal_id"]
        for _i, p in filter_sort_entries(entries, sort_mode=SORT_NAME_DESC)
    ]
    assert asc == ["8002", "8003", "8001"]
    assert desc == ["8001", "8003", "8002"]
    assert asc != desc


def test_mtime_sort_differs_from_name_when_times_equal(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Regression: equal updated_at must not make SORT_MTIME == SORT_NAME."""
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    stamp = _iso(10)
    _seed_mod(db, library, mid="8101", title="Zulu", updated_at=stamp)
    _seed_mod(db, library, mid="8102", title="Alpha", updated_at=stamp)
    _seed_mod(db, library, mid="8103", title="Mike", updated_at=stamp)

    entries = _entries(db)
    by_name = [
        p["internal_id"] for _i, p in filter_sort_entries(entries, sort_mode=SORT_NAME)
    ]
    by_mtime = [
        p["internal_id"] for _i, p in filter_sort_entries(entries, sort_mode=SORT_MTIME)
    ]
    assert by_name == ["8102", "8103", "8101"]
    # Tie-break is mod_id DESC, not name — so modes differ.
    assert by_mtime != by_name
    assert by_mtime == ["8103", "8102", "8101"]


def test_recent_modified_orders_by_updated_at(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    _seed_mod(db, library, mid="8201", title="Old", updated_at=_iso(10))
    _seed_mod(db, library, mid="8202", title="Mid", updated_at=_iso(20))
    _seed_mod(db, library, mid="8203", title="New", updated_at=_iso(99))

    ordered = filter_sort_entries(_entries(db), sort_mode=SORT_MTIME)
    assert [p["internal_id"] for _i, p in ordered] == ["8203", "8202", "8201"]


def test_sort_does_not_scan_filesystem_or_reconcile(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    _seed_mod(db, library, mid="8301", title="A", updated_at=_iso(1))
    _seed_mod(db, library, mid="8302", title="B", updated_at=_iso(2))

    fs_probe = MagicMock(side_effect=AssertionError("filesystem scan forbidden"))
    reconcile = MagicMock(side_effect=AssertionError("reconcile forbidden"))
    monkeypatch.setattr("pathlib.Path.stat", fs_probe)
    monkeypatch.setattr("pathlib.Path.iterdir", fs_probe)
    monkeypatch.setattr(
        "services.library_reconcile.reconcile_library", reconcile, raising=False
    )

    rows = db.list_mod_list_items(game_id=42)
    entries = [(_index_from_row(r), r) for r in rows]
    filter_sort_entries(entries, sort_mode=SORT_NAME)
    filter_sort_entries(entries, sort_mode=SORT_MTIME)
    fs_probe.assert_not_called()
    reconcile.assert_not_called()


def test_sort_10k_performance(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    # Lightweight inserts — skip full folder tree for scale.
    now = _utc_now_bulk = datetime(2026, 2, 1, tzinfo=timezone.utc)
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
    assert [p["internal_id"] for _i, p in name_order] != [
        p["internal_id"] for _i, p in mtime_order
    ]
    # Budget: Query sort of 10k must stay interactive; repo load similarly.
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
        _seed_mod(db, library, mid=mid, title=title, updated_at=_iso(bump))

    entries = _entries(db)
    name_rows = filter_sort_entries(entries, sort_mode=SORT_NAME)
    mtime_rows = filter_sort_entries(entries, sort_mode=SORT_MTIME)
    assert [p["internal_id"] for _i, p in name_rows] != [
        p["internal_id"] for _i, p in mtime_rows
    ]

    for sorted_rows in (name_rows, mtime_rows):
        query_ids = [p["internal_id"] for _i, p in sorted_rows]
        window = compute_viewport_window(
            item_count=len(sorted_rows),
            scroll_y=0,
            viewport_height=2000,
            viewport_width=1200,
        )
        slice_ids = [
            sorted_rows[i][1]["internal_id"]
            for i in range(window.first_index, window.last_index)
        ]
        assert slice_ids == query_ids[window.first_index : window.last_index]


def test_status_recovery_does_not_bump_sort_mtime(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    _seed_mod(db, library, mid="8501", title="Keep", updated_at=_iso(40))
    before = db._conn.execute(
        "SELECT updated_at FROM mods WHERE mod_id = 8501"
    ).fetchone()["updated_at"]

    db.update_mod_content_status(
        8501,
        content_status="healthy",
        touch_updated_at=False,
    )
    db.update_mod_identity_fields(8501, identity_status="identity_conflict")

    after = db._conn.execute(
        "SELECT updated_at, identity_status FROM mods WHERE mod_id = 8501"
    ).fetchone()
    assert after["updated_at"] == before
    assert after["identity_status"] == "identity_conflict"


def test_list_item_projection_feeds_query_mtime(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(42, name="GameX", mod_path="")
    _seed_mod(db, library, mid="8601", title="Snap", updated_at=_iso(77))
    row = db.list_mod_list_items(game_id=42)[0]
    item = mod_list_item_from_row(row)
    card = list_item_to_card_data(item)
    expected = updated_at_to_mtime(_iso(77))
    assert item.mtime == expected
    assert card.updated_time == expected
    assert sort_key(_index_from_row(row), SORT_MTIME)[0] == -expected
