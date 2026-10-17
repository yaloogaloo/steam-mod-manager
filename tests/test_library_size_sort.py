"""Phase 4B — Size sort uses persisted observation scalars only (no FS walk)."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import patch

import pytest

from ui.library_query import (
    SORT_LABELS,
    SORT_MTIME,
    SORT_NAME,
    SORT_SIZE,
    ModFilterIndex,
    filter_sort_entries,
    sort_key,
)


MB = 1024 * 1024
GB = 1024 * 1024 * 1024


def _idx(
    mid: str,
    name: str,
    *,
    size: int | None = None,
    status: str = "unknown",
    mtime: float = 1.0,
) -> ModFilterIndex:
    return ModFilterIndex(
        mod_id=str(mid),
        display_name=name,
        steam_name=name,
        notes="",
        game_name="GameX",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=mtime,
        sort_name=name,
        local_size_bytes=size,
        local_size_status=status,
    )


def _ids(entries: list[tuple[ModFilterIndex, str]]) -> list[str]:
    return [payload for _index, payload in entries]


def test_sort_size_orders_valid_bytes_ascending() -> None:
    entries = [
        (_idx("1", "five_gb", size=5 * GB, status="ok"), "5GB"),
        (_idx("2", "ten_mb", size=10 * MB, status="ok"), "10MB"),
        (_idx("3", "one_gb", size=1 * GB, status="ok"), "1GB"),
        (_idx("4", "hundred_mb", size=100 * MB, status="ok"), "100MB"),
    ]
    ordered = filter_sort_entries(entries, sort_mode=SORT_SIZE)
    assert _ids(ordered) == ["10MB", "100MB", "1GB", "5GB"]


def test_sort_size_zero_ok_is_valid_not_unknown() -> None:
    entries = [
        (_idx("1", "mid", size=20 * MB, status="ok"), "20MB"),
        (_idx("2", "empty", size=0, status="ok"), "0B"),
        (_idx("3", "unk", size=None, status="unknown"), "unknown"),
    ]
    ordered = filter_sort_entries(entries, sort_mode=SORT_SIZE)
    assert _ids(ordered) == ["0B", "20MB", "unknown"]
    empty = ordered[0][0]
    assert empty.local_size_status == "ok"
    assert empty.local_size_bytes == 0
    key = sort_key(empty, SORT_SIZE)
    unk_key = sort_key(ordered[2][0], SORT_SIZE)
    assert key[0] == 0
    assert unk_key[0] == 1


def test_sort_size_unknown_missing_failed_after_valid() -> None:
    entries = [
        (_idx("1", "unk", size=None, status="unknown"), "unknown"),
        (_idx("2", "ok", size=500 * MB, status="ok"), "500MB"),
        (_idx("3", "miss", size=50, status="missing"), "missing"),
        (_idx("4", "fail", size=50, status="failed"), "failed"),
    ]
    ordered = filter_sort_entries(entries, sort_mode=SORT_SIZE)
    ids = _ids(ordered)
    assert ids[0] == "500MB"
    assert set(ids[1:]) == {"unknown", "missing", "failed"}


def test_sort_size_mixed_valid_then_invalid_groups() -> None:
    entries = [
        (_idx("1", "unknown", size=None, status="unknown"), "unknown"),
        (_idx("2", "five_hundred", size=500 * MB, status="ok"), "500MB"),
        (_idx("3", "missing", size=None, status="missing"), "missing"),
        (_idx("4", "zero", size=0, status="ok"), "0B"),
        (_idx("5", "failed", size=99, status="failed"), "failed"),
        (_idx("6", "twenty", size=20 * MB, status="ok"), "20MB"),
        (_idx("7", "two_gb", size=2 * GB, status="ok"), "2GB"),
    ]
    ordered = filter_sort_entries(entries, sort_mode=SORT_SIZE)
    ids = _ids(ordered)
    assert ids[:4] == ["0B", "20MB", "500MB", "2GB"]
    assert set(ids[4:]) == {"unknown", "missing", "failed"}
    # Invalid last-known bytes must not rank as real size (failed=99 must not
    # sort as 99 B ahead of 0 B).
    assert ids.index("failed") > ids.index("2GB")


def test_sort_size_does_not_coerce_null_or_invalid_to_zero() -> None:
    zero = _idx("1", "zero", size=0, status="ok")
    unk = _idx("2", "unk", size=None, status="unknown")
    miss = _idx("3", "miss", size=None, status="missing")
    fail = _idx("4", "fail", size=0, status="failed")
    assert sort_key(zero, SORT_SIZE)[0] == 0
    assert sort_key(zero, SORT_SIZE)[1] == 0
    assert sort_key(unk, SORT_SIZE)[0] == 1
    assert sort_key(miss, SORT_SIZE)[0] == 1
    assert sort_key(fail, SORT_SIZE)[0] == 1
    assert sort_key(unk, SORT_SIZE) != sort_key(zero, SORT_SIZE)


def test_sort_size_is_memory_only_no_filesystem_or_workers() -> None:
    src = inspect.getsource(sort_key) + inspect.getsource(filter_sort_entries)
    assert "os.walk" not in src
    assert "directory_size" not in src
    assert "observe_mod_size" not in src
    assert "enqueue_mod_size" not in src
    entries = [
        (_idx("1", "a", size=3, status="ok"), "a"),
        (_idx("2", "b", size=1, status="ok"), "b"),
    ]
    with (
        patch("os.walk", side_effect=AssertionError("walk")),
        patch("services.dir_size.directory_size", side_effect=AssertionError("dir")),
        patch(
            "services.size_observation.observe_mod_size",
            side_effect=AssertionError("observe"),
        ),
        patch(
            "services.size_observation.enqueue_mod_size",
            side_effect=AssertionError("enqueue"),
        ),
    ):
        ordered = filter_sort_entries(entries, sort_mode=SORT_SIZE)
    assert _ids(ordered) == ["b", "a"]


def test_cover_change_does_not_require_view_recompute() -> None:
    from ui.library_query import projection_requires_view_recompute

    old = _idx("1", "Same", size=10, status="ok")
    new = _idx("1", "Same", size=10, status="ok")
    assert projection_requires_view_recompute(old, new, sort_mode=SORT_MTIME) is False


def test_name_change_requires_recompute_only_for_name_sort() -> None:
    from ui.library_query import projection_requires_view_recompute

    old = _idx("1", "Alpha", size=10, status="ok")
    new = ModFilterIndex(
        **{**old.__dict__, "display_name": "Zulu", "sort_name": "Zulu"}
    )
    assert projection_requires_view_recompute(old, new, sort_mode=SORT_MTIME) is False
    assert projection_requires_view_recompute(old, new, sort_mode=SORT_NAME) is True


def test_size_change_requires_recompute_only_for_size_sort() -> None:
    from ui.library_query import projection_requires_view_recompute

    old = _idx("1", "Sized", size=100, status="ok")
    new = _idx("1", "Sized", size=1_000_000, status="ok")
    assert projection_requires_view_recompute(old, new, sort_mode=SORT_MTIME) is False
    assert projection_requires_view_recompute(old, new, sort_mode=SORT_SIZE) is True
    labels = [label for _key, label in SORT_LABELS]
    keys = [key for key, _label in SORT_LABELS]
    assert "大小" in labels
    assert SORT_SIZE in keys
    assert keys.count(SORT_SIZE) == 1
    assert not any("升序" in label or "降序" in label for label in labels)


def test_library_sort_combo_offers_size(qapp_mod_library) -> None:
    view = qapp_mod_library
    texts = [view.sort_combo.itemText(i) for i in range(view.sort_combo.count())]
    keys = [view.sort_combo.itemData(i) for i in range(view.sort_combo.count())]
    assert "大小" in texts
    assert SORT_SIZE in keys
    idx = view.sort_combo.findData(SORT_SIZE)
    assert idx >= 0
    view.sort_combo.setCurrentIndex(idx)
    assert view._sort_mode == SORT_SIZE


def test_library_size_sort_applies_to_normal_library_rows(qapp_mod_library, tmp_path: Path) -> None:
    view = qapp_mod_library
    folder = tmp_path / "GameX"
    rows = [
        ("u", "Unknown", None, "unknown", 9.0),
        ("b", "Big", 100 * MB, "ok", 1.0),
        ("z", "Zero", 0, "ok", 8.0),
        ("m", "Missing", 12, "missing", 7.0),
    ]
    entries = []
    for mid, name, size, status, mtime in rows:
        path = folder / name
        path.mkdir(parents=True, exist_ok=True)
        index = _idx(mid, name, size=size, status=status, mtime=mtime)
        from services.mod_library_cache import ModCardData
        from core.mod_platform import PLATFORM_STEAM

        payload = ModCardData(
            id=mid,
            title=name,
            platform=PLATFORM_STEAM,
            cover="",
            description="",
            tags="",
            size=size if status == "ok" else None,
            updated_time=mtime,
            managed_path=str(path),
            game_folder="GameX",
            steam_name=name,
            game_name="GameX",
            size_status=status,
        )
        entries.append((index, payload))
    view._game_row_entries = entries
    view._filtered_row_entries = list(entries)
    view._last_filter_sig = None
    idx = view.sort_combo.findData(SORT_SIZE)
    view.sort_combo.setCurrentIndex(idx)
    names = [index.display_name for index, _p in view._filtered_row_entries]
    assert names[:2] == ["Zero", "Big"]
    assert set(names[2:]) == {"Unknown", "Missing"}


@pytest.fixture()
def qapp_mod_library():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from ui.library_view import ModLibraryView

    app = QApplication.instance() or QApplication([])
    view = ModLibraryView()
    yield view
    view.deleteLater()
    app.processEvents()
