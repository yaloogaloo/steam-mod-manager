"""Metadata backup layer — Phase 1.

Backup storage key is ``mods.mod_id`` (DB PK). Entity identity is
``.info/internal_id`` == ``mods.internal_id``. Never use Workshop ID as PK.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, persist_unified_metadata_dict
from services.importers.materialize import materialize_imported_mod
from services.metadata_backup import (
    backup_root,
    load_backup,
    mark_missing,
    reconcile_library_presence,
    sync_metadata_backup,
)
from tests.helpers.identity import bind_managed_path, create_steam_test_mod, write_info_sidecar
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


def _seed_mod(
    db: DatabaseManager,
    library: Path,
    *,
    workshop_id: str,
    game: str,
    title: str,
    meta_title: str = "",
) -> tuple[Path, str, str]:
    """Create Entity + folder + ``.info/internal_id`` proof. Returns folder, pk, frozen."""
    created = create_steam_test_mod(
        db, external_id=workshop_id, title=meta_title or title, game_name=game
    )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "")
    folder = library / game / title
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title=meta_title or title,
        external_id=workshop_id,
        workspace_id=workshop_id,
        game_name=game,
        extra={
            "display_name": meta_title or title,
            "description": f"desc-{workshop_id}",
            "source_type": "steam",
            "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={workshop_id}",
        },
    )
    (folder / "content.txt").write_text("payload", encoding="utf-8")
    bind_managed_path(db, pk, folder, title=meta_title or title, game_name=game)
    return folder, pk, frozen


def test_sync_creates_backup_when_mod_exists(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _seed_mod(
        db, library, workshop_id="910001", game="GameA", title="ModB", meta_title="Title B"
    )

    sync_metadata_backup(folder, mod_id=pk)

    backup = load_backup(pk)
    assert backup is not None
    assert backup.metadata.get("title") == "Title B"
    assert (backup_root(pk) / METADATA_FILENAME).is_file()
    row = db.get_mod_backup_row(pk)
    assert row is not None
    assert int(row["folder_present"]) == 1
    assert Path(str(row["last_known_path"])).samefile(folder)
    assert str(row.get("internal_id") or "") == frozen


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
    folder, pk, _frozen = _seed_mod(
        db, library, workshop_id="910002", game="GameA", title="ModB", meta_title="Gone Mod"
    )

    sync_metadata_backup(folder, mod_id=pk)
    shutil.rmtree(folder)

    reconcile_library_presence(library, on_disk_mod_ids=set())
    missing = db.list_folder_missing_mods(library_root=library)
    assert len(missing) == 1
    assert str(missing[0]["mod_id"]) == pk

    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    monkeypatch.setattr("ui.library_view.get_db", lambda: db)
    monkeypatch.setattr("ui.mod_card.get_db", lambda: db, raising=False)
    from services.file_ops import ModFileManager

    view = ModLibraryView()
    view.set_target_root(str(library))
    view._current_game_filter = "GameA"
    view._render_mod_cards(ModFileManager(library))
    app.processEvents()

    assert len(view._cards) == 1
    card = view._cards[0]
    from services.metadata_backup import is_mod_folder_absent

    assert is_mod_folder_absent(pk, card.managed_path)
    card.show()
    card.refresh_display()
    assert card.missing_badge.text() == "MISS"
    assert not card.missing_badge.isHidden()


def test_restore_folder_syncs_info_priority(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _seed_mod(
        db, library, workshop_id="910003", game="GameA", title="ModB", meta_title="From Info"
    )

    sync_metadata_backup(folder, mod_id=pk)

    backup_meta = json.loads(
        (backup_root(pk) / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    backup_meta["title"] = "Backup A"
    backup_meta["display_name"] = "Backup A"
    (backup_root(pk) / METADATA_FILENAME).write_text(
        json.dumps(backup_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    db.update_mod_backup_snapshot(
        pk,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        backup_metadata_json=json.dumps(backup_meta, ensure_ascii=False),
    )

    persist_unified_metadata_dict(
        folder,
        {
            "internal_id": frozen,
            "published_file_id": "910003",
            "workspace_id": "910003",
            "title": "Info B",
            "display_name": "Info B",
            "game_name": "GameA",
        },
        sync_backup=False,
    )

    shutil.rmtree(folder)
    mark_missing(pk)
    restored = library / "GameA" / "ModB"
    write_info_sidecar(
        restored,
        internal_id=frozen,
        title="Info B",
        external_id="910003",
        workspace_id="910003",
        game_name="GameA",
        extra={"display_name": "Info B"},
    )
    (restored / "content.txt").write_text("payload", encoding="utf-8")
    reconcile_library_presence(library, on_disk_mod_ids={pk})
    sync_metadata_backup(restored, mod_id=pk)

    row = db.get_mod_backup_row(pk)
    assert row is not None
    assert int(row["folder_present"]) == 1
    saved = json.loads(str(row["backup_metadata_json"]))
    assert saved.get("title") == "Info B"
    assert saved.get("display_name") == "Info B"


def test_info_overrides_backup_on_display_conflict(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _seed_mod(
        db, library, workshop_id="910004", game="GameA", title="ModB", meta_title="B"
    )

    persist_unified_metadata_dict(
        folder,
        {
            "internal_id": frozen,
            "published_file_id": "910004",
            "workspace_id": "910004",
            "title": "B",
            "display_name": "B",
            "game_name": "GameA",
        },
        sync_backup=False,
    )
    sync_metadata_backup(folder, mod_id=pk)

    backup_meta = json.loads(
        (backup_root(pk) / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    backup_meta["title"] = "A"
    backup_meta["display_name"] = "A"
    (backup_root(pk) / METADATA_FILENAME).write_text(
        json.dumps(backup_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    sync_metadata_backup(folder, mod_id=pk)

    row = db.get_mod_backup_row(pk)
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
    folder, pk, _frozen = _seed_mod(
        db, library, workshop_id="910009", game="GameA", title="HashMod", meta_title="Hash Me"
    )
    info = folder / INFO_DIR_NAME
    (info / "cover.png").write_bytes(b"\x89PNG" + b"cover-bytes" * 50)
    offline = info / "offline"
    offline.mkdir()
    (offline / "index.html").write_text("<html>offline</html>", encoding="utf-8")

    assert sync_after_metadata_change(pk, folder, "import", wait=True)
    caplog.clear()
    assert sync_after_metadata_change(pk, folder, "restore", wait=True)
    lines = [r.getMessage() for r in caplog.records if "[RECONCILE_TIMING]" in r.getMessage()]
    assert lines
    msg = lines[-1]
    assert "hash_files=" in msg
    hash_files = int(msg.split("hash_files=")[1].split()[0])
    assert hash_files >= 2
    assert "size_match_then_hash=" in msg
    assert int(msg.split("size_match_then_hash=")[1].split()[0]) >= 1
