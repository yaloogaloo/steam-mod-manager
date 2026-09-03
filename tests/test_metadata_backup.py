"""Metadata backup layer — Phase 1."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, persist_unified_metadata_dict
from services.metadata_backup import (
    backup_root,
    load_backup,
    mark_missing,
    reconcile_library_presence,
    sync_metadata_backup,
)
from services.importers.materialize import materialize_imported_mod
from ui.library_view import ModLibraryView


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "backup.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    return root


def _write_mod(
    library: Path,
    game: str,
    title: str,
    mod_id: str,
    *,
    meta_title: str = "",
) -> Path:
    folder = library / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    payload = {
        "published_file_id": mod_id,
        "title": meta_title or title,
        "display_name": meta_title or title,
        "game_name": game,
        "description": f"desc-{mod_id}",
        "source_type": "github",
        "url": f"https://example.com/{mod_id}",
        "workspace_id": f"ws-{mod_id}",
    }
    persist_unified_metadata_dict(folder, payload)
    (folder / "content.txt").write_text("payload", encoding="utf-8")
    return folder


def test_sync_creates_backup_when_mod_exists(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_mod(library, "GameA", "ModB", "910001", meta_title="Title B")
    db.upsert_mod(
        ModMetadata(
            published_file_id="910001",
            title="Title B",
            game_name="GameA",
            managed_path=str(folder),
        )
    )

    sync_metadata_backup(folder)

    backup = load_backup("910001")
    assert backup is not None
    assert backup.metadata.get("title") == "Title B"
    assert (backup_root("910001") / METADATA_FILENAME).is_file()
    row = db.get_mod_backup_row("910001")
    assert row is not None
    assert int(row["folder_present"]) == 1
    assert Path(str(row["last_known_path"])).samefile(folder)


def test_library_shows_missing_mod_after_folder_deleted(
    db: DatabaseManager,
    data_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qapp = pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    library = tmp_path / "mod"
    folder = _write_mod(library, "GameA", "ModB", "910002", meta_title="Gone Mod")
    db.upsert_mod(
        ModMetadata(
            published_file_id="910002",
            title="Gone Mod",
            game_name="GameA",
            managed_path=str(folder),
        )
    )
    sync_metadata_backup(folder)
    shutil.rmtree(folder)

    reconcile_library_presence(library, on_disk_mod_ids=set())
    missing = db.list_folder_missing_mods(library_root=library)
    assert len(missing) == 1
    assert str(missing[0]["mod_id"]) == "910002"

    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db)
    from services.file_ops import ModFileManager

    view = ModLibraryView()
    view.set_target_root(str(library))
    view._current_game_filter = "GameA"
    view._render_mod_cards(ModFileManager(library))
    app.processEvents()

    assert len(view._cards) == 1
    card = view._cards[0]
    from services.metadata_backup import is_mod_folder_absent

    assert is_mod_folder_absent("910002", card.managed_path)
    card.show()
    card.refresh_display()
    assert card.missing_badge.text() == "⚠ 目录缺失"
    assert not card.missing_badge.isHidden()


def test_restore_folder_syncs_info_priority(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_mod(library, "GameA", "ModB", "910003", meta_title="From Info")
    db.upsert_mod(
        ModMetadata(
            published_file_id="910003",
            title="From Info",
            game_name="GameA",
            managed_path=str(folder),
        )
    )
    sync_metadata_backup(folder)

    # Mutate backup title while folder still exists with different .info title.
    backup_meta = json.loads(
        (backup_root("910003") / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    backup_meta["title"] = "Backup A"
    backup_meta["display_name"] = "Backup A"
    (backup_root("910003") / METADATA_FILENAME).write_text(
        json.dumps(backup_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    db.update_mod_backup_snapshot(
        "910003",
        last_known_path=str(folder.resolve()),
        folder_present=True,
        backup_metadata_json=json.dumps(backup_meta, ensure_ascii=False),
    )

    persist_unified_metadata_dict(
        folder,
        {
            "published_file_id": "910003",
            "title": "Info B",
            "display_name": "Info B",
            "game_name": "GameA",
        },
    )

    shutil.rmtree(folder)
    mark_missing("910003")
    restored = _write_mod(library, "GameA", "ModB", "910003", meta_title="Info B")
    reconcile_library_presence(library, on_disk_mod_ids={"910003"})
    sync_metadata_backup(restored)

    row = db.get_mod_backup_row("910003")
    assert row is not None
    assert int(row["folder_present"]) == 1
    saved = json.loads(str(row["backup_metadata_json"]))
    assert saved.get("title") == "Info B"
    assert saved.get("display_name") == "Info B"


def test_info_overrides_backup_on_display_conflict(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_mod(library, "GameA", "ModB", "910004", meta_title="B")
    db.upsert_mod(
        ModMetadata(
            published_file_id="910004",
            title="B",
            game_name="GameA",
            managed_path=str(folder),
        )
    )

    persist_unified_metadata_dict(
        folder,
        {
            "published_file_id": "910004",
            "title": "B",
            "display_name": "B",
            "game_name": "GameA",
        },
    )
    sync_metadata_backup(folder)

    backup_meta = json.loads(
        (backup_root("910004") / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    backup_meta["title"] = "A"
    backup_meta["display_name"] = "A"
    (backup_root("910004") / METADATA_FILENAME).write_text(
        json.dumps(backup_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    sync_metadata_backup(folder)

    row = db.get_mod_backup_row("910004")
    assert row is not None
    saved = json.loads(str(row["backup_metadata_json"]))
    assert saved.get("title") == "B"
    assert saved.get("display_name") == "B"


def test_materialize_import_triggers_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    src = tmp_path / "src"
    src.mkdir()
    (src / "mod.dat").write_text("x", encoding="utf-8")
    db.register_external_mod(
        platform="github",
        external_id="gh-1",
        source_url="https://github.com/a/b",
        title="Imported",
        app_id=100,
        game_name="GameA",
        mod_id=9_000_000_000_000_100,
    )
    dest = materialize_imported_mod(
        library_root=library,
        mod_id=9_000_000_000_000_100,
        title="Imported",
        game_name="GameA",
        source_folder=src,
    )
    row = db.get_mod_backup_row("9000000000000100")
    assert row is not None
    assert int(row["folder_present"]) == 1
    assert (backup_root("9000000000000100") / METADATA_FILENAME).is_file()
    assert dest.is_dir()


def test_unchanged_cover_still_hashed_on_second_sync(
    db: DatabaseManager, data_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """P0-4: size-equal cover/offline still SHA256 on a no-op second sync."""
    import logging

    from services.metadata_backup_sync import sync_after_metadata_change

    caplog.set_level(logging.INFO)
    library = tmp_path / "mod"
    folder = _write_mod(library, "GameA", "HashMod", "910009", meta_title="Hash Me")
    info = folder / INFO_DIR_NAME
    (info / "cover.png").write_bytes(b"\x89PNG" + b"cover-bytes" * 50)
    offline = info / "offline"
    offline.mkdir()
    (offline / "index.html").write_text("<html>offline</html>", encoding="utf-8")
    db.upsert_mod(
        ModMetadata(
            published_file_id="910009",
            title="Hash Me",
            game_name="GameA",
            managed_path=str(folder),
        )
    )
    assert sync_after_metadata_change("910009", folder, "import")
    caplog.clear()
    assert sync_after_metadata_change("910009", folder, "restore")
    lines = [r.getMessage() for r in caplog.records if "[RECONCILE_TIMING]" in r.getMessage()]
    assert lines
    msg = lines[-1]
    assert "hash_files=" in msg
    hash_files = int(msg.split("hash_files=")[1].split()[0])
    copy_files = int(msg.split("copy_files=")[1].split()[0])
    assert hash_files >= 2
    assert copy_files == 0
    assert "size_match_then_hash=" in msg
    assert int(msg.split("size_match_then_hash=")[1].split()[0]) >= 1
