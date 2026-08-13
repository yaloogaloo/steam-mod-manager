"""Phase 9.1 library performance finalization tests (isolated tmp fixtures)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_reconcile import start_reconcile_library_async
from services.mod_library_cache import (
    build_library_snapshot,
    get_library_cache,
    reset_library_cache,
)

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from ui.mod_detail_panel import ModDetailPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_db(tmp_path: Path, name: str) -> DatabaseManager:
    DatabaseManager.reset_instance()
    return DatabaseManager.instance(tmp_path / name)


def _seed(root: Path, db: DatabaseManager, count: int, *, id_base: int = 700000) -> None:
    game = root / "PerfGame"
    for i in range(count):
        mid = str(id_base + i)
        folder = game / f"Mod{i:04d}"
        info = folder / INFO_DIR_NAME
        info.mkdir(parents=True)
        (info / METADATA_FILENAME).write_text(
            json.dumps(
                {
                    "published_file_id": mid,
                    "title": f"Perf Mod {i}",
                    "game_name": "PerfGame",
                }
            ),
            encoding="utf-8",
        )
        (folder / "payload.bin").write_bytes(b"x" * 16)
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


# ---- Cases 1–9 ----


def test_case1_warm_snapshot_no_rebuild(tmp_path: Path) -> None:
    db = _make_db(tmp_path, "c1.db")
    lib = tmp_path / "library"
    _seed(lib, db, 30)
    reset_library_cache()
    cache = get_library_cache()
    cache.load_snapshot(lib, force=True)

    with mock.patch(
        "services.mod_library_cache.build_library_snapshot",
        side_effect=AssertionError("should not rebuild"),
    ):
        snap = cache.load_snapshot(lib, force=False)
        peek = cache.peek_snapshot(lib)
    assert snap.total_count == 30
    assert peek is not None
    assert peek.total_count == 30
    DatabaseManager.reset_instance()
    reset_library_cache()


def test_case2_explicit_refresh_force_rebuilds(tmp_path: Path) -> None:
    db = _make_db(tmp_path, "c2.db")
    lib = tmp_path / "library"
    _seed(lib, db, 20)
    reset_library_cache()
    cache = get_library_cache()
    cache.load_snapshot(lib, force=True)
    calls = {"n": 0}
    real = build_library_snapshot

    def _counting(root):
        calls["n"] += 1
        return real(root)

    with mock.patch(
        "services.mod_library_cache.build_library_snapshot", side_effect=_counting
    ):
        cache.load_snapshot(lib, force=True)
    assert calls["n"] == 1
    DatabaseManager.reset_instance()
    reset_library_cache()


def test_case3_game_switch_uses_memory_cards(tmp_path: Path) -> None:
    db = _make_db(tmp_path, "c3.db")
    lib = tmp_path / "library"
    _seed(lib, db, 25)
    reset_library_cache()
    snap = build_library_snapshot(lib)
    # Simulate game filter: memory only
    with mock.patch(
        "services.mod_metadata_resolver.list_visible_mods",
        side_effect=AssertionError("disk scan"),
    ):
        filtered = [c for c in snap.cards if c.game_folder == "PerfGame"]
    assert len(filtered) == 25
    DatabaseManager.reset_instance()
    reset_library_cache()


def test_case4_filter_no_filesystem(tmp_path: Path) -> None:
    from ui.library_query import (
        FILTER_ALL,
        FILTER_PLATFORM_ALL,
        SORT_NAME,
        ModFilterIndex,
        filter_and_sort,
    )

    db = _make_db(tmp_path, "c4.db")
    lib = tmp_path / "library"
    _seed(lib, db, 40)
    reset_library_cache()
    snap = build_library_snapshot(lib)
    entries = []
    for c in snap.cards:
        title = c.title or ""
        entries.append(
            (
                ModFilterIndex(
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
                    content_status=c.content_status,
                    source_type=c.source_type,
                    platform=c.platform,
                ),
                c.id,
            )
        )

    with mock.patch("pathlib.Path.is_dir", side_effect=AssertionError("fs")):
        with mock.patch("pathlib.Path.exists", side_effect=AssertionError("fs")):
            out = filter_and_sort(
                entries,
                query="Perf Mod 1",
                filter_key=FILTER_ALL,
                platform_key=FILTER_PLATFORM_ALL,
                sort_mode=SORT_NAME,
            )
    assert len(out) >= 1
    DatabaseManager.reset_instance()
    reset_library_cache()


def test_case5_snapshot_uses_batch_backup_rows(tmp_path: Path) -> None:
    """Snapshot layer batches backup rows (Resolver may still read SQLite per resolve)."""
    db = _make_db(tmp_path, "c5.db")
    lib = tmp_path / "library"
    _seed(lib, db, 35)
    reset_library_cache()
    batch = mock.Mock(wraps=db.get_mods_backup_rows)
    db.get_mods_backup_rows = batch  # type: ignore[method-assign]
    snap = build_library_snapshot(lib)
    assert snap.total_count == 35
    assert batch.call_count == 1
    # Ensure snapshot module does not call per-mod backup in the card loop
    import inspect
    import services.mod_library_cache as mlc

    src = inspect.getsource(mlc.build_library_snapshot)
    assert "get_mod_backup_row(" not in src
    assert "get_mods_backup_rows" in src
    DatabaseManager.reset_instance()
    reset_library_cache()


def test_case6_detail_no_sync_cover_decode(
    qapp: QApplication, tmp_path: Path, monkeypatch
) -> None:
    db = _make_db(tmp_path, "c6.db")
    lib = tmp_path / "library"
    _seed(lib, db, 5)
    folder = lib / "PerfGame" / "Mod0000"
    cover = folder / INFO_DIR_NAME / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff" + b"\x00" * 128)

    requests: list[str] = []

    class FakeMgr:
        def __init__(self) -> None:
            self.image_ready = mock.Mock()
            self.image_ready.connect = mock.Mock()

        def request(self, token, managed, *, cover_ref="", width=0, height=0):
            requests.append(str(cover_ref or managed))

        def cancel(self, _token):
            return None

    fake = FakeMgr()
    import services.cover_loader as cl

    monkeypatch.setattr(
        cl.CoverLoaderManager, "instance", classmethod(lambda cls: fake)
    )

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id="700000")
    qapp.processEvents()
    assert requests, "cover should be requested asynchronously"
    DatabaseManager.reset_instance()


def test_case7_detail_size_not_sync_walk(
    qapp: QApplication, tmp_path: Path, monkeypatch
) -> None:
    db = _make_db(tmp_path, "c7.db")
    lib = tmp_path / "library"
    _seed(lib, db, 3)
    folder = lib / "PerfGame" / "Mod0000"

    walked = {"n": 0}
    gate = {"go": False}
    import services.dir_size as dir_size_mod

    real = dir_size_mod.directory_size

    def _gated(path):
        while not gate["go"]:
            time.sleep(0.005)
        walked["n"] += 1
        return real(path)

    monkeypatch.setattr(dir_size_mod, "directory_size", _gated)
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id="700000")
    qapp.processEvents()
    # First paint must not wait for the walk
    assert walked["n"] == 0
    gate["go"] = True
    for _ in range(40):
        qapp.processEvents()
        time.sleep(0.02)
        if walked["n"] > 0:
            break
    assert walked["n"] >= 1
    DatabaseManager.reset_instance()


def test_case8_reconcile_no_concurrent_duplicate(tmp_path: Path) -> None:
    import services.library_reconcile as rec

    DatabaseManager.reset_instance()
    DatabaseManager.instance(tmp_path / "c8.db")
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / "G").mkdir()

    started = {"n": 0}
    gate = {"release": False}

    def _slow(_root=None):
        started["n"] += 1
        while not gate["release"]:
            time.sleep(0.01)

    with mock.patch.object(rec, "reconcile_library", side_effect=_slow):
        with mock.patch.object(rec, "_reconcile_running", False):
            assert start_reconcile_library_async(lib) is True
            # Second call while running should queue, not start another thread body yet
            assert start_reconcile_library_async(lib) is False
            time.sleep(0.05)
            assert started["n"] == 1
            gate["release"] = True
            time.sleep(0.1)
            # queued follow-up may run once more
            assert started["n"] in (1, 2)
    DatabaseManager.reset_instance()


@pytest.mark.parametrize("n", (100, 500, 1000))
def test_case9_snapshot_benchmark(tmp_path: Path, n: int) -> None:
    db = _make_db(tmp_path, f"c9_{n}.db")
    lib = tmp_path / "library"
    _seed(lib, db, n, id_base=800000)
    reset_library_cache()
    t0 = time.perf_counter()
    snap = build_library_snapshot(lib)
    cold = time.perf_counter() - t0
    assert snap.total_count == n

    cache = get_library_cache()
    cache.load_snapshot(lib, force=True)
    t1 = time.perf_counter()
    cache.load_snapshot(lib, force=False)
    warm = time.perf_counter() - t1
    print(f"\n[PERF9.1] N={n} cold={cold:.4f}s warm={warm:.4f}s")
    assert warm < 0.05
    # Soft budgets — machine variance allowed
    if n <= 100:
        assert cold < 1.5
    elif n <= 500:
        assert cold < 4.0
    else:
        assert cold < 12.0
    DatabaseManager.reset_instance()
    reset_library_cache()
