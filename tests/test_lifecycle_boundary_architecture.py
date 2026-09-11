"""P0 lifecycle boundary architecture — ownership contracts, not symptom guards.

Covers:
1. Reconcile must not call create_mod_identity
2. Unchanged reconcile must not enqueue backup
3. Library snapshot must not filesystem-scan
4. Library must not call resolve_games
5. Refresh with identical metadata must not dirty backup
6. list_mod_list_items(game_id) must SQL-filter (no full-table Python filter)
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager, _utc_now
from core.game_info import GameInfo
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_reconcile import reconcile_library
from services.metadata_backup_sync import backup_queue_size
from services.mod_library_cache import build_library_snapshot, reset_library_cache
from services.orphan_import import import_orphan_candidates

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "lifecycle_boundary.db")
    yield manager
    DatabaseManager.reset_instance()
    reset_library_cache()


def _write_info(folder: Path, payload: dict) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (folder / "content.pak").write_bytes(b"pak")


# ---------------------------------------------------------------------------
# 1. Reconcile must not create Identity
# ---------------------------------------------------------------------------


def test_reconcile_source_forbids_create_mod_identity() -> None:
    src = (ROOT / "services" / "library_reconcile.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            assert name != "create_mod_identity", (
                "Reconcile must not call create_mod_identity — "
                "emit OrphanCandidate for Import/Sync instead"
            )


def test_reconcile_unknown_folder_emits_orphan_not_entity(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("core.paths.data_dir", lambda: data)
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: data)

    library = tmp_path / "mod"
    folder = library / "GameX" / "ModA"
    _write_info(
        folder,
        {
            "title": "ModA",
            "game_name": "GameX",
            "source_type": "nexus",
            "url": "https://www.nexusmods.com/gamex/mods/960001",
            "workspace_id": "960001",
            "external_id": "960001",
            "app_id": 1,
        },
    )
    before = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    result = reconcile_library(library)
    after = db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"]
    assert after == before
    assert result.orphans, "expected OrphanCandidate for unknown official folder"
    assert any("ORPHAN_CANDIDATE" in n for n in result.notes)

    imported = import_orphan_candidates(result.orphans, db=db)
    assert imported.imported >= 1
    assert db._conn.execute("SELECT COUNT(*) AS c FROM mods").fetchone()["c"] == before + 1


# ---------------------------------------------------------------------------
# 2. Reconcile unchanged → backup queue == 0
# ---------------------------------------------------------------------------


def test_reconcile_unchanged_10000_mods_backup_queue_zero(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Scale contract: 10000 existing entities, no metadata change → backup queue 0.

    Uses DB seed + bind-only identity stubs so the test measures lifecycle
    ownership (not Windows mkdir throughput).
    """
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("core.paths.data_dir", lambda: data)
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: data)

    library = tmp_path / "mod"
    game = "ScaleGame"
    (library / game).mkdir(parents=True)
    db.upsert_game(GameInfo(app_id=91001, name=game, folder_name=game))
    now = _utc_now()
    n = 10_000
    folders: list[Path] = []
    rows: list[tuple] = []
    for i in range(n):
        mid = 2_000_000 + i
        folder = library / game / f"Mod{i:05d}"
        folders.append(folder)
        rows.append(
            (
                mid,
                91001,
                f"Mod{i}",
                "",
                "",
                f"Mod{i}",
                "",
                "",
                0,
                "not_deployed",
                "steam",
                f"https://steamcommunity.com/sharedfiles/filedetails/?id={mid}",
                str(mid),
                str(mid),
                "{}",
                0,
                "none",
                1,
                "none",
                "",
                now,
                str(folder),
                1,
                "healthy",
                "steam",
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

    # Minimal Path objects — discovery stubbed (avoid 10k NTFS mkdir wall time).
    folders = [library / game / f"Mod{i:05d}" for i in range(n)]

    def _ensure(folder, raw=None, db=None):
        # Map Mod00000 → 2000000+index via folder name digits.
        idx = int("".join(ch for ch in Path(folder).name if ch.isdigit()) or "0")
        mid = str(2_000_000 + idx)
        payload = {
            "published_file_id": mid,
            "workspace_id": mid,
            "external_id": mid,
            "title": Path(folder).name,
            "identity_status": "complete",
            "app_id": 91001,
        }
        return mid, payload, False  # changed=False → must not dirty backup

    monkeypatch.setattr(
        "services.library_reconcile.ensure_mod_identity", _ensure
    )
    monkeypatch.setattr(
        "services.library_reconcile.read_info_metadata_dict",
        lambda folder: {"title": Path(folder).name},
    )
    monkeypatch.setattr(
        "services.file_ops.ModFileManager.list_managed_mods",
        lambda self: folders,
    )
    monkeypatch.setattr(
        "services.path_lifecycle.detect_path_drift",
        lambda *_a, **_k: None,
    )
    # Status recovery / content eval / identity persist rewrite sidecars for
    # every folder — out of scope for the backup-queue contract and dominate
    # wall time at 10k scale.
    monkeypatch.setattr(
        "services.status_recovery.run_status_recovery",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "services.status_recovery.run_status_model_cleanup_v2",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "services.identity_service.persist_identity",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "services.content_status_eval.persist_evaluated_content_status",
        lambda *_a, **_k: None,
    )

    dirty_calls: list[tuple] = []

    def _track_dirty(mod_id, managed_path, reason):
        dirty_calls.append((str(mod_id), str(reason)))
        return True

    monkeypatch.setattr(
        "services.library_reconcile.mark_backup_dirty", _track_dirty
    )
    reconcile_library(library)
    assert dirty_calls == [], (
        f"unchanged reconcile must not mark_backup_dirty; got {len(dirty_calls)} "
        f"sample={dirty_calls[:3]}"
    )
    assert backup_queue_size() == 0


# ---------------------------------------------------------------------------
# 3 / 4. Library forbids FS scan + resolve_games
# ---------------------------------------------------------------------------


def test_library_snapshot_forbids_filesystem_scan_and_resolve_games() -> None:
    snap_src = inspect.getsource(
        __import__(
            "services.mod_library_cache", fromlist=["build_library_snapshot"]
        ).build_library_snapshot
    )
    entries_src = inspect.getsource(
        __import__(
            "services.mod_library_cache", fromlist=["_build_game_entries"]
        )._build_game_entries
    )
    combined = snap_src + "\n" + entries_src

    def _strip_docs(s: str) -> str:
        parts = s.split('"""')
        if len(parts) < 3:
            return s
        return parts[0] + "".join(parts[2::2])

    code = _strip_docs(combined)
    assert "resolve_games" not in code
    assert "list_games" not in code
    assert "list_managed_mods" not in code
    assert "iterdir" not in code
    assert "build_game_sidebar_view_models" in combined


def test_library_snapshot_uses_db_sidebar(
    db: DatabaseManager, tmp_path: Path
) -> None:
    lib = tmp_path / "mod"
    db.upsert_game(GameInfo(app_id=42, name="DBGame", folder_name="DBGame"))
    folder = lib / "DBGame" / "Only"
    folder.mkdir(parents=True)
    db.upsert_mod(
        ModMetadata(
            published_file_id="420001",
            title="Only",
            app_id=42,
            managed_path=str(folder),
        )
    )
    db.update_mod_identity_fields(
        "420001",
        folder_present=True,
        last_known_path=str(folder),
        app_id=42,
    )
    # Filesystem-only decoy must not appear in sidebar.
    (lib / "FsOnlyGame").mkdir(parents=True)
    reset_library_cache()
    snap = build_library_snapshot(lib)
    folders = {g.folder for g in snap.games}
    assert "DBGame" in folders
    assert "FsOnlyGame" not in folders


# ---------------------------------------------------------------------------
# 5. Refresh identical metadata → no backup dirty
# ---------------------------------------------------------------------------


def test_refresh_identical_metadata_does_not_enqueue_backup(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.mod_platform import PLATFORM_STEAM
    from services.metadata_refresh import refresh_steam_mod_metadata

    library = tmp_path / "mod"
    mid = "3555555555"
    folder = library / "G" / "Same"
    folder.mkdir(parents=True)
    payload = {
        "published_file_id": mid,
        "title": "Same",
        "display_name": "Same",
        "description": "desc",
        "preview_url": "https://example.com/p.jpg",
        "workspace_id": mid,
        "external_id": mid,
        "source_type": "steam",
        "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={mid}",
        "app_id": 1,
        "game_name": "G",
    }
    _write_info(folder, payload)
    db.upsert_game(GameInfo(app_id=1, name="G", folder_name="G"))
    db.upsert_mod(
        ModMetadata(
            published_file_id=mid,
            title="Same",
            description="desc",
            preview_url="https://example.com/p.jpg",
            app_id=1,
            managed_path=str(folder),
        )
    )
    db.update_mod_identity_fields(
        mid,
        folder_present=True,
        last_known_path=str(folder),
        workspace_id=mid,
        external_id=mid,
        platform=PLATFORM_STEAM,
        app_id=1,
    )
    db.set_official_metadata_synced(mid, False)

    fake = ModMetadata(
        published_file_id=mid,
        title="Same",
        description="desc",
        preview_url="https://example.com/p.jpg",
        app_id=1,
    )

    class _FakeClient:
        def refresh_details(self, ids, enable_scrape_fallback=False):
            return [fake]

        def close(self):
            return None

    dirty: list[tuple] = []

    def _no_dirty(*a, **k):
        dirty.append(a)
        return True

    import services.metadata_backup_sync as mbs

    monkeypatch.setattr(mbs, "sync_after_metadata_change", _no_dirty)
    monkeypatch.setattr(mbs, "mark_backup_dirty", _no_dirty)
    # Identical old/new fingerprint → Refresh must not dirty backup.
    monkeypatch.setattr(mbs, "metadata_fingerprint", lambda data=None: "SAME")

    # Avoid network cover download side paths.
    monkeypatch.setattr(
        "core.steam_api.SteamWorkshopClient.fetch_and_save_cover",
        lambda *a, **k: None,
        raising=False,
    )

    result = refresh_steam_mod_metadata(
        mid,
        folder,
        library_root=library,
        force=True,
        allow_official_sync=True,
        db=db,
        client=_FakeClient(),
    )
    assert result.success
    assert dirty == [], f"identical metadata must not dirty backup: {dirty}"


def test_refresh_local_reconcile_does_not_unconditionally_backup() -> None:
    src = inspect.getsource(
        __import__(
            "services.mod_refresh", fromlist=["reconcile_local_state"]
        ).reconcile_local_state
    )
    # Executable calls only — architecture notes may mention forbidden APIs.
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            assert name not in {"sync_after_metadata_change", "mark_backup_dirty"}


# ---------------------------------------------------------------------------
# 6. list_mod_list_items SQL pushdown
# ---------------------------------------------------------------------------


def test_list_mod_list_items_sql_filters_by_game_id(
    db: DatabaseManager, tmp_path: Path
) -> None:
    lib = tmp_path / "mod"
    db.upsert_game(GameInfo(app_id=101, name="GameA", folder_name="GameA"))
    db.upsert_game(GameInfo(app_id=202, name="GameB", folder_name="GameB"))
    for i, (gid, gname) in enumerate([(101, "GameA"), (202, "GameB")]):
        for j in range(3):
            mid = str(500000 + i * 10 + j)
            folder = lib / gname / f"M{j}"
            folder.mkdir(parents=True, exist_ok=True)
            db.upsert_mod(
                ModMetadata(
                    published_file_id=mid,
                    title=f"M{j}",
                    app_id=gid,
                    managed_path=str(folder),
                )
            )
            db.update_mod_identity_fields(
                mid, folder_present=True, last_known_path=str(folder), app_id=gid
            )

    rows = db.list_mod_list_items(game_id=101)
    assert len(rows) == 3
    assert all(int(r["game_id"]) == 101 for r in rows)

    src = inspect.getsource(DatabaseManager.list_mod_list_items)
    assert "m.app_id = ?" in src
    assert "game_id" in src
    assert "folder_filter and derived_folder" not in src


def test_list_mod_list_items_100000_switch_reads_target_game_only(
    db: DatabaseManager, tmp_path: Path
) -> None:
    lib = tmp_path / "mod"
    db.upsert_game(GameInfo(app_id=1, name="BigA", folder_name="BigA"))
    db.upsert_game(GameInfo(app_id=2, name="BigB", folder_name="BigB"))
    now = _utc_now()
    rows: list[tuple] = []
    # 99900 in game 1, 100 in game 2 — switching to game 2 must return 100 only.
    for i in range(99_900):
        mid = 3_000_000 + i
        path = str(lib / "BigA" / f"M{i}")
        rows.append((mid, 1, f"A{i}", path, now))
    for i in range(100):
        mid = 4_000_000 + i
        path = str(lib / "BigB" / f"M{i}")
        rows.append((mid, 2, f"B{i}", path, now))
    with db._lock:
        db._conn.executemany(
            """
            INSERT INTO mods (
                mod_id, app_id, title, last_known_path, updated_at, folder_present
            ) VALUES (?, ?, ?, ?, ?, 1)
            """,
            rows,
        )
        db._conn.commit()

    out = db.list_mod_list_items(game_id=2)
    assert len(out) == 100
    assert all(int(r["game_id"]) == 2 for r in out)
    # Full-library read would be 100000; SQL pushdown must not.
    assert len(db.list_mod_list_items()) == 100_000
