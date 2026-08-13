"""Unified metadata resolver priority: .info > backup > SQLite."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, persist_unified_metadata_dict
from services.metadata_backup import backup_root, reconcile_folder_presence, sync_metadata_backup
from services.mod_metadata_resolver import (
    ModMetadataResolver,
    resolve_cover_path,
    resolve_mod_metadata,
    resolve_offline_page,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "resolver.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    return root


def _write_info(folder: Path, payload: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    persist_unified_metadata_dict(folder, payload)


def _write_backup_files(
    mod_id: str,
    payload: dict,
    *,
    cover: bool = False,
    offline: bool = False,
) -> Path:
    dest = backup_root(mod_id)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if cover:
        (dest / "cover.jpg").write_bytes(b"cover-bytes")
    if offline:
        off = dest / "offline"
        off.mkdir(exist_ok=True)
        (off / "index.html").write_text("<html>backup</html>", encoding="utf-8")
    return dest


def test_existing_folder_prefers_info_over_backup_and_sqlite(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModA"
    _write_info(
        folder,
        {
            "published_file_id": "910101",
            "title": "A",
            "display_name": "A",
            "description": "info-desc",
        },
    )
    db.upsert_mod(
        ModMetadata(published_file_id="910101", title="C", managed_path=str(folder))
    )
    db.update_mod_user_metadata("910101", {"display_name": "C"})
    _write_backup_files(
        "910101",
        {"published_file_id": "910101", "title": "B", "display_name": "B"},
    )

    resolved = resolve_mod_metadata("910101", folder)
    assert resolved is not None
    assert resolved.display_name == "A"
    assert resolved.folder_present is True


def test_missing_folder_prefers_backup_over_sqlite(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModB"
    _write_info(
        folder,
        {
            "published_file_id": "910102",
            "title": "FromInfo",
            "display_name": "FromInfo",
        },
    )
    db.upsert_mod(
        ModMetadata(published_file_id="910102", title="C", managed_path=str(folder))
    )
    db.update_mod_user_metadata("910102", {"display_name": "C"})
    sync_metadata_backup(folder)
    _write_backup_files(
        "910102",
        {
            "published_file_id": "910102",
            "title": "B",
            "display_name": "B",
            "description": "backup-desc",
        },
    )
    shutil.rmtree(folder)
    db.set_mod_folder_present("910102", present=False)

    resolved = resolve_mod_metadata("910102", folder)
    assert resolved is not None
    assert resolved.folder_present is False
    assert resolved.display_name == "B"
    assert resolved.description == "backup-desc"


def test_restored_folder_info_wins_without_resolver_write(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    """Resolver prefers .info and must not rewrite backup (Phase 3-B)."""
    folder = tmp_path / "mod" / "Game" / "ModC"
    _write_info(
        folder,
        {"published_file_id": "910103", "title": "A", "display_name": "A"},
    )
    db.upsert_mod(
        ModMetadata(published_file_id="910103", title="A", managed_path=str(folder))
    )
    sync_metadata_backup(folder)
    _write_backup_files(
        "910103",
        {"published_file_id": "910103", "title": "B", "display_name": "B"},
    )

    resolved = resolve_mod_metadata("910103", folder)
    assert resolved is not None
    assert resolved.display_name == "A"
    saved = json.loads(
        (backup_root("910103") / "metadata.json").read_text(encoding="utf-8")
    )
    # Pure-read: polluted backup remains until an explicit write-path sync.
    assert saved.get("title") == "B" or saved.get("display_name") == "B"

    from services.metadata_backup_sync import sync_after_metadata_change

    sync_after_metadata_change("910103", folder, "repair")
    saved2 = json.loads(
        (backup_root("910103") / "metadata.json").read_text(encoding="utf-8")
    )
    assert saved2.get("title") == "A" or saved2.get("display_name") == "A"


def test_missing_folder_uses_backup_cover_not_sqlite_path(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModD"
    _write_info(
        folder,
        {
            "published_file_id": "910104",
            "title": "CoverMod",
            "display_name": "CoverMod",
            "cover_path": ".info/cover.jpg",
        },
    )
    (folder / INFO_DIR_NAME / "cover.jpg").write_bytes(b"info-cover")
    db.upsert_mod(
        ModMetadata(
            published_file_id="910104", title="CoverMod", managed_path=str(folder)
        )
    )
    db.update_mod_cover_path("910104", str(folder / INFO_DIR_NAME / "cover.jpg"))
    sync_metadata_backup(folder)
    shutil.rmtree(folder)
    db.set_mod_folder_present("910104", present=False)

    cover = resolve_cover_path("910104", folder)
    assert cover is not None
    assert cover.is_file()
    assert "mod_backup" in str(cover).replace("\\", "/")


def test_missing_folder_opens_backup_offline(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModE"
    info = folder / INFO_DIR_NAME / "offline"
    info.mkdir(parents=True)
    (folder / INFO_DIR_NAME / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": "910105",
                "title": "OffMod",
                "display_name": "OffMod",
            }
        ),
        encoding="utf-8",
    )
    (info / "index.html").write_text("<html>info</html>", encoding="utf-8")
    db.upsert_mod(
        ModMetadata(published_file_id="910105", title="OffMod", managed_path=str(folder))
    )
    sync_metadata_backup(folder)
    shutil.rmtree(folder)
    db.set_mod_folder_present("910105", present=False)

    page = resolve_offline_page("910105", folder)
    assert page is not None
    assert page.is_file()
    assert "mod_backup" in str(page).replace("\\", "/")
    assert page.read_text(encoding="utf-8") == "<html>info</html>"


def test_resolver_does_not_read_missing_info_path(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "Gone"
    db.upsert_mod(
        ModMetadata(published_file_id="910106", title="C", managed_path=str(folder))
    )
    db.update_mod_user_metadata("910106", {"display_name": "C"})
    _write_backup_files(
        "910106",
        {"published_file_id": "910106", "title": "B", "display_name": "B"},
        cover=True,
        offline=True,
    )
    db.update_mod_backup_snapshot(
        "910106",
        last_known_path=str(folder),
        folder_present=False,
        backup_metadata_json=json.dumps(
            {"published_file_id": "910106", "title": "B", "display_name": "B"}
        ),
        backup_cover_path=str(backup_root("910106") / "cover.jpg"),
        backup_offline_path=str(backup_root("910106") / "offline" / "index.html"),
    )
    assert not folder.exists()
    resolved = ModMetadataResolver().resolve_missing_folder("910106", folder)
    assert resolved is not None
    assert resolved.display_name == "B"
    assert resolved.cover_path
    assert Path(resolved.cover_path).is_file()
    assert resolved.offline_path
    assert Path(resolved.offline_path).is_file()


def test_reconcile_marks_deleted_folder_missing(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModF"
    _write_info(
        folder,
        {"published_file_id": "910107", "title": "F", "display_name": "F"},
    )
    db.upsert_mod(
        ModMetadata(published_file_id="910107", title="F", managed_path=str(folder))
    )
    sync_metadata_backup(folder)
    shutil.rmtree(folder)
    reconcile_folder_presence(tmp_path / "mod")
    row = db.get_mod_backup_row("910107")
    assert row is not None
    assert int(row["folder_present"]) == 0
