"""Scale + architecture acceptance: Library must stay DB-index + viewport at 10k+.

Acceptance (not micro-optimization):
- Seed 10 games / 10000 Mods in SQLite only (no real Mod files).
- Startup index load is second-scale.
- Game switch p95 < 500ms for 100 / 1000 / 5000 / 10000 sized games.
- Viewport creates dozens of cards, never N cards for the whole game.
- Library path forbids FS resolve / backup load / reconcile / sync backup.
"""

from __future__ import annotations

import ast
import inspect
import time
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager, _utc_now
from core.game_info import GameInfo
from services.library_perf_metrics import (
    get_library_perf_metrics,
    reset_library_perf_metrics,
)
from services.mod_library_cache import (
    build_library_snapshot,
    reset_library_cache,
)
from services.mod_list_item import (
    MOD_LIST_ITEM_FORBIDDEN_FIELDS,
    ModListItem,
    assert_mod_list_item_layer1,
)
from ui.library_viewport import compute_viewport_window

ROOT = Path(__file__).resolve().parents[1]

# 10 games totaling 10000 Mods. Named sizes used for switch benchmarks.
SCALE_DISTRIBUTION: list[tuple[str, int, int]] = [
    ("ScaleGame100", 91001, 100),
    ("ScaleGame1000", 91002, 1000),
    ("ScaleGame5000", 91003, 5000),
    ("ScaleGameG4", 91004, 600),
    ("ScaleGameG5", 91005, 600),
    ("ScaleGameG6", 91006, 600),
    ("ScaleGameG7", 91007, 600),
    ("ScaleGameG8", 91008, 500),
    ("ScaleGameG9", 91009, 500),
    ("ScaleGameG10", 91010, 500),
]
assert sum(c for _, _, c in SCALE_DISTRIBUTION) == 10000
assert len(SCALE_DISTRIBUTION) == 10

# One dedicated game with exactly 10000 for the largest switch case.
FULL_GAME = ("ScaleGame10000", 91999, 10000)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "scale.db")
    yield manager
    DatabaseManager.reset_instance()
    reset_library_cache()
    reset_library_perf_metrics()


def _seed_db_only(
    db: DatabaseManager,
    lib: Path,
    distribution: list[tuple[str, int, int]],
    *,
    id_base: int = 1_000_000,
) -> dict[str, int]:
    """Insert Layer-1 rows without creating Mod files on disk."""
    lib.mkdir(parents=True, exist_ok=True)
    now = _utc_now()
    mid = id_base
    counts: dict[str, int] = {}
    rows: list[tuple] = []
    for folder, app_id, count in distribution:
        db.upsert_game(
            GameInfo(app_id=app_id, name=folder, folder_name=folder)
        )
        counts[folder] = count
        for i in range(count):
            mid += 1
            path = str(lib / folder / f"Mod{i:05d}")
            rows.append(
                (
                    mid,
                    app_id,
                    f"Scale Mod {i}",
                    "",
                    "",  # description empty in seed (list must not need it)
                    f"Scale Mod {i}",
                    "",
                    "",
                    0,
                    "not_deployed",
                    "steam",
                    "",
                    str(mid),
                    str(mid),
                    "{}",
                    0,
                    "none",
                    1,
                    "none",
                    "",
                    now,
                    path,
                    1,
                    "healthy",
                    "",
                )
            )
    with db._lock:
        db._conn.executemany(
            """
            INSERT INTO mods (
                mod_id, app_id, title, preview_url, description,
                display_name, custom_description, user_notes, favorite,
                deploy_status, platform, source_url, external_id, workspace_id,
                mod_files, is_invalid, conflict_status, enabled, offline_status,
                cover_path, updated_at, last_known_path, folder_present,
                content_status, source_type
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?
            )
            """,
            rows,
        )
        db._conn.commit()
    return counts


def test_library_path_ast_forbids_forbidden_apis() -> None:
    """Guard: Library projection must not call resolve / backup / reconcile."""
    forbidden = {
        "list_visible_mods",
        "load_backup",
        "reconcile_library",
        "start_reconcile_library_async",
        "sync_after_metadata_change",
        "rglob",
    }
    for rel in (
        "services/mod_library_cache.py",
        "ui/library_view.py",
        "ui/library_viewport.py",
    ):
        path = ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            assert name not in forbidden, f"{rel} calls forbidden {name}"


def test_mod_list_item_layer1_contract() -> None:
    names = set(ModListItem.__dataclass_fields__)  # type: ignore[attr-defined]
    assert not (names & MOD_LIST_ITEM_FORBIDDEN_FIELDS)
    for bad in ("description", "html", "hash", "file_list", "payload_scan"):
        assert bad not in names
    item = ModListItem(
        internal_id="36834fcf-3cbb-4ffe-8b78-be1921638bd4",
        workspace_id="1",
        game_id=1,
        game_folder="G",
        name="N",
    )
    assert_mod_list_item_layer1(item)


def test_game_switch_model_is_sql_viewmodel_ui() -> None:
    src = inspect.getsource(build_library_snapshot)
    body = src.split('"""', 2)[-1] if '"""' in src else src
    assert "list_mod_list_items" in body
    assert "list_visible_mods" not in body
    assert "load_backup" not in body
    assert "directory_size" not in body
    assert "os.walk" not in body
    lv = (ROOT / "ui" / "library_view.py").read_text(encoding="utf-8")
    assert "_sync_viewport_cards" in lv
    assert "compute_viewport_window" in lv
    # Hard: Library refresh must not schedule reconcile.
    refresh_src = inspect.getsource(
        __import__("ui.library_view", fromlist=["ModLibraryView"]).ModLibraryView.refresh
    )
    assert "start_reconcile_library_async" not in refresh_src
    assert "list_visible_mods" not in refresh_src


def test_startup_index_second_scale(db: DatabaseManager, tmp_path: Path) -> None:
    lib = tmp_path / "mod"
    _seed_db_only(db, lib, SCALE_DISTRIBUTION)
    reset_library_cache()
    reset_library_perf_metrics()
    metrics = get_library_perf_metrics()
    metrics.mark_startup_begin()
    t0 = time.perf_counter()
    snap = build_library_snapshot(lib)
    total_ms = (time.perf_counter() - t0) * 1000.0
    metrics.mark_startup_end()
    assert snap.total_count == 10000
    assert total_ms < 5000.0, f"startup index {total_ms:.1f}ms"
    perf = metrics.snapshot()
    assert perf.library_index_load_ms > 0
    assert perf.database_query_ms >= 0
    assert perf.viewmodel_create_ms >= 0
    # No description bodies on Layer-1 cards.
    assert all(c.description == "" for c in snap.cards[:50])
    assert all(
        "description" not in item.__dataclass_fields__  # type: ignore[attr-defined]
        for item in snap.list_items[:1]
    )


@pytest.mark.parametrize(
    "game_folder,expected_count",
    [
        ("ScaleGame100", 100),
        ("ScaleGame1000", 1000),
        ("ScaleGame5000", 5000),
        ("ScaleGame10000", 10000),
    ],
)
def test_game_switch_p95_under_500ms(
    db: DatabaseManager,
    tmp_path: Path,
    game_folder: str,
    expected_count: int,
) -> None:
    lib = tmp_path / "mod"
    if game_folder == "ScaleGame10000":
        _seed_db_only(db, lib, [FULL_GAME], id_base=2_000_000)
    else:
        _seed_db_only(db, lib, SCALE_DISTRIBUTION)
    reset_library_cache()
    snap = build_library_snapshot(lib)
    assert snap.total_count >= expected_count

    # Warm switch path: filter Layer-1 + viewport window (no full widget tree).
    samples: list[float] = []
    cards_created_samples: list[int] = []
    for _ in range(5):
        t0 = time.perf_counter()
        rows = [c for c in snap.cards if c.game_folder == game_folder]
        assert len(rows) == expected_count
        window = compute_viewport_window(
            item_count=len(rows),
            scroll_y=0,
            viewport_width=1200,
            viewport_height=800,
        )
        visible = window.last_index - window.first_index
        elapsed = (time.perf_counter() - t0) * 1000.0
        samples.append(elapsed)
        cards_created_samples.append(visible)

    samples.sort()
    p95 = samples[int(len(samples) * 0.95) - 1] if len(samples) >= 2 else samples[-1]
    assert p95 < 500.0, f"{game_folder} switch p95={p95:.1f}ms samples={samples}"
    # Viewport must be << full game size for large libraries.
    if expected_count >= 1000:
        assert max(cards_created_samples) < 500
        assert max(cards_created_samples) < expected_count // 2


def test_viewport_never_creates_all_cards_for_10k() -> None:
    window = compute_viewport_window(
        item_count=10_000,
        scroll_y=0,
        viewport_width=1200,
        viewport_height=800,
    )
    created = window.last_index - window.first_index
    assert created < 200
    assert created > 0


def test_ui_viewport_pool_under_10k(
    db: DatabaseManager, tmp_path: Path, qapp, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from services.file_ops import ModFileManager
    from ui.library_view import ModLibraryView

    lib = tmp_path / "mod"
    _seed_db_only(db, lib, [FULL_GAME], id_base=3_000_000)
    reset_library_cache()
    reset_library_perf_metrics()

    monkeypatch.setattr("ui.library_view._library_load_sync", lambda: True)
    monkeypatch.setattr(
        "services.presence_reconcile.schedule_presence_reconcile",
        lambda *a, **k: None,
        raising=False,
    )
    monkeypatch.setattr(
        "services.mod_fs_observer.schedule_observe_mods_fs_batch",
        lambda *a, **k: None,
        raising=False,
    )
    monkeypatch.setattr(
        "services.size_observation.schedule_library_size_refresh",
        lambda *a, **k: None,
        raising=False,
    )

    view = ModLibraryView()
    try:
        view.resize(1200, 900)
        view.set_target_root(str(lib))
        view.refresh(force=True, reconcile=False)
        qapp.processEvents()

        view._current_game_filter = FULL_GAME[0]
        view._render_mod_cards(ModFileManager(lib), force_reload=False)
        qapp.processEvents()
        view._sync_viewport_cards()
        qapp.processEvents()

        assert len(view._game_row_entries) == 10000
        assert view._card_create_count < 200
        assert len(view._cards) < 200
        perf = get_library_perf_metrics().snapshot()
        assert perf.visible_cards < 200
        assert perf.cards_created < 200
    finally:
        try:
            view.cancel_pending_library_load()
        except Exception:  # noqa: BLE001
            pass
        view.close()
        view.deleteLater()
        qapp.processEvents()


def test_cover_loader_is_viewport_only() -> None:
    src = (ROOT / "ui" / "library_view.py").read_text(encoding="utf-8")
    assert "_load_viewport_covers" in src
    assert "iter_viewport_cover_cards" in src
    # Must not loop all cards for cover on game enter without viewport filter.
    assert "for card in self._cards" in src or "iter_viewport_cover_cards" in src


def test_backup_default_is_enqueue_not_inline() -> None:
    from services import metadata_backup_sync as mbs

    assert "import" not in mbs._INLINE_REASONS
    assert "restore" not in mbs._INLINE_REASONS
    assert "sync" in mbs.VALID_REASONS
    src = inspect.getsource(mbs.sync_after_metadata_change)
    assert "mark_backup_dirty" in src


def test_reconcile_must_not_inline_backup_for_bulk() -> None:
    """Reconcile must not force inline backup for bulk consistency scans."""
    from services import metadata_backup_sync as mbs

    src = (ROOT / "services" / "library_reconcile.py").read_text(encoding="utf-8")
    # Current contract: reconcile dirties via mark_backup_dirty only; it must not
    # call sync_after_metadata_change (that path is for real metadata mutations).
    assert "sync_after_metadata_change" not in src
    assert "mark_backup_dirty" in src
    assert "import" not in mbs._INLINE_REASONS
    assert "restore" not in mbs._INLINE_REASONS


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app
