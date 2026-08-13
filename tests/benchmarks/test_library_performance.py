"""Phase 9 library performance baselines (synthetic fixtures).

Records wall time for snapshot build / warm cache / filter / detail for
N in {50, 100, 500, 1000}. Safe: uses tmp_path only — never the user library.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.mod_library_cache import (
    build_library_snapshot,
    get_library_cache,
    reset_library_cache,
)
from ui.library_query import (
    FILTER_ALL,
    FILTER_FAVORITE,
    FILTER_FOLDER_MISSING,
    FILTER_PLATFORM_ALL,
    SORT_NAME,
    ModFilterIndex,
    filter_and_sort,
)

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from ui.mod_detail_panel import ModDetailPanel

NS = (50, 100, 500, 1000)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _seed(root: Path, db: DatabaseManager, count: int) -> None:
    game = root / "PerfGame"
    for i in range(count):
        mid = str(600000 + i)
        folder = game / f"Mod{i:04d}"
        info = folder / INFO_DIR_NAME
        info.mkdir(parents=True)
        (info / METADATA_FILENAME).write_text(
            json.dumps(
                {
                    "published_file_id": mid,
                    "title": f"Perf Mod {i}",
                    "game_name": "PerfGame",
                    "description": f"desc {i}",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (folder / "payload.bin").write_bytes(b"x" * 32)
        if i % 17 == 0:
            (info / "cover.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 64)
        db.upsert_mod(
            ModMetadata(
                published_file_id=mid,
                title=f"Perf Mod {i}",
                managed_path=str(folder),
                game_name="PerfGame",
            )
        )
        db.update_mod_identity_fields(
            mid,
            source_type="steam",
            content_status="healthy",
            folder_present=True,
            last_known_path=str(folder),
            sticky_source=False,
        )
        if i % 11 == 0:
            db.update_mod_user_metadata(mid, {"favorite": True})


def _make_db(tmp_path: Path, name: str) -> DatabaseManager:
    DatabaseManager.reset_instance()
    return DatabaseManager.instance(tmp_path / name)


def _indexes_from_snapshot(snap) -> list[tuple[ModFilterIndex, object]]:
    out: list[tuple[ModFilterIndex, object]] = []
    for c in snap.cards:
        title = str(c.title or "")
        idx = ModFilterIndex(
            mod_id=c.id,
            display_name=title,
            steam_name=c.steam_name,
            notes=c.notes,
            game_name=c.game_name,
            favorite=c.favorite,
            deployed=c.deployed,
            has_offline=c.has_offline,
            mtime=c.updated_time,
            sort_name=title.casefold(),
            invalid=c.invalid,
            conflict=c.conflict,
            tag_values=c.tag_values,
            platform=c.platform,
            source_url=c.source_url,
            external_id=c.external_id,
            is_invalid=c.invalid,
            conflict_status=c.conflict_status,
            enabled=c.enabled,
            category_tags=c.category_tags,
            content_status=c.content_status,
            source_type=c.source_type,
        )
        out.append((idx, object()))
    return out


@pytest.mark.parametrize("n", NS)
def test_baseline_snapshot_cold_and_warm(tmp_path: Path, n: int) -> None:
    db = _make_db(tmp_path, f"perf_{n}.db")
    lib = tmp_path / "library"
    _seed(lib, db, n)
    reset_library_cache()

    t0 = time.perf_counter()
    snap1 = build_library_snapshot(lib)
    cold = time.perf_counter() - t0
    assert snap1.total_count == n

    cache = get_library_cache()
    cache.load_snapshot(lib, force=True)  # populate
    t1 = time.perf_counter()
    snap2 = cache.load_snapshot(lib, force=False)
    warm = time.perf_counter() - t1
    assert snap2.total_count == n
    assert warm < cold or warm < 0.05

    print(f"\n[PERF] N={n} snapshot_cold={cold:.4f}s warm_cache={warm:.4f}s")

    DatabaseManager.reset_instance()
    reset_library_cache()


@pytest.mark.parametrize("n", NS)
def test_baseline_filter_search_memory(tmp_path: Path, n: int) -> None:
    db = _make_db(tmp_path, f"filt_{n}.db")
    lib = tmp_path / "library"
    _seed(lib, db, n)
    reset_library_cache()
    snap = build_library_snapshot(lib)
    entries = _indexes_from_snapshot(snap)

    t0 = time.perf_counter()
    filtered = filter_and_sort(
        entries,
        query="Perf Mod 1",
        filter_key=FILTER_ALL,
        platform_key=FILTER_PLATFORM_ALL,
        sort_mode=SORT_NAME,
    )
    search_t = time.perf_counter() - t0

    t1 = time.perf_counter()
    fav = filter_and_sort(
        entries,
        query="",
        filter_key=FILTER_FAVORITE,
        platform_key=FILTER_PLATFORM_ALL,
        sort_mode=SORT_NAME,
    )
    fav_t = time.perf_counter() - t1

    t2 = time.perf_counter()
    status = filter_and_sort(
        entries,
        query="",
        filter_key=FILTER_FOLDER_MISSING,
        platform_key=FILTER_PLATFORM_ALL,
        sort_mode=SORT_NAME,
    )
    status_t = time.perf_counter() - t2

    print(
        f"\n[PERF] N={n} search={search_t:.4f}s fav={fav_t:.4f}s "
        f"status={status_t:.4f}s hits={len(filtered)}/{len(fav)}/{len(status)}"
    )
    assert search_t < 0.25
    assert fav_t < 0.25
    assert status_t < 0.25
    DatabaseManager.reset_instance()
    reset_library_cache()


def test_baseline_detail_open(qapp: QApplication, tmp_path: Path) -> None:
    db = _make_db(tmp_path, "detail.db")
    lib = tmp_path / "library"
    _seed(lib, db, 20)
    folder = lib / "PerfGame" / "Mod0000"
    panel = ModDetailPanel()
    t0 = time.perf_counter()
    panel.show_mod(folder, mod_id="600000")
    qapp.processEvents()
    elapsed = time.perf_counter() - t0
    print(f"\n[PERF] detail_open={elapsed:.4f}s")
    assert elapsed < 0.5
    DatabaseManager.reset_instance()


def test_baseline_second_force_vs_soft(tmp_path: Path) -> None:
    """Compare force rebuild vs soft cache hit (documents Phase 9 gap)."""
    db = _make_db(tmp_path, "soft.db")
    lib = tmp_path / "library"
    _seed(lib, db, 200)
    reset_library_cache()
    cache = get_library_cache()

    t0 = time.perf_counter()
    cache.load_snapshot(lib, force=True)
    first = time.perf_counter() - t0

    t1 = time.perf_counter()
    cache.load_snapshot(lib, force=True)
    forced = time.perf_counter() - t1

    t2 = time.perf_counter()
    cache.load_snapshot(lib, force=False)
    soft = time.perf_counter() - t2

    print(
        f"\n[PERF] N=200 first={first:.4f}s force_again={forced:.4f}s "
        f"soft={soft:.4f}s"
    )
    assert soft < forced
    DatabaseManager.reset_instance()
    reset_library_cache()
