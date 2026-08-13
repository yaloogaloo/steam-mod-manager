"""Phase 3-A: metadata backup lifecycle (.info → backup only)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, persist_unified_metadata_dict
from services.metadata_backup import backup_root, load_backup
from services.metadata_backup_sync import (
    rebuild_missing_metadata_backup,
    sync_after_metadata_change,
)
from services.mod_metadata_resolver import resolve_mod_metadata


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "lifecycle.db")
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
    *,
    game: str,
    title: str,
    mod_id: str,
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
        "source_url": f"https://example.com/{mod_id}",
        "workspace_id": f"ws-{mod_id}",
        "external_id": mod_id,
    }
    persist_unified_metadata_dict(folder, payload)
    (folder / "content.txt").write_text("payload", encoding="utf-8")
    return folder


def test_case1_info_change_syncs_backup_title(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_mod(
        library, game="GameA", title="ModA", mod_id="930001", meta_title="A"
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="930001",
            title="A",
            game_name="GameA",
            managed_path=str(folder),
        )
    )
    snap = load_backup("930001")
    assert snap is not None
    assert snap.metadata.get("title") == "A"

    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    data["title"] = "B"
    data["display_name"] = "B"
    persist_unified_metadata_dict(folder, data)

    snap2 = load_backup("930001")
    assert snap2 is not None
    assert snap2.metadata.get("title") == "B"


def test_case2_deleted_folder_ui_uses_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_mod(
        library, game="GameA", title="ModGone", mod_id="930002", meta_title="KeepMe"
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="930002",
            title="KeepMe",
            game_name="GameA",
            managed_path=str(folder),
        )
    )
    assert load_backup("930002") is not None

    shutil.rmtree(folder)
    assert not folder.exists()

    resolved = resolve_mod_metadata("930002", managed_path=str(folder))
    assert resolved is not None
    assert resolved.folder_present is False
    assert resolved.display_name == "KeepMe" or resolved.title == "KeepMe"


def test_case3_rebuild_creates_missing_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    folder = library / "GameA" / "NeedsBackup"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    payload = {
        "published_file_id": "930003",
        "title": "NeedsBackup",
        "display_name": "NeedsBackup",
        "source_type": "steam",
        "url": "https://example.com/930003",
        "source_url": "https://example.com/930003",
        "workspace_id": "ws-930003",
        "external_id": "930003",
    }
    # Write .info without going through persist (which would auto-sync).
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (folder / "content.txt").write_text("x", encoding="utf-8")
    db.upsert_mod(
        ModMetadata(
            published_file_id="930003",
            title="NeedsBackup",
            game_name="GameA",
            managed_path=str(folder),
        )
    )

    backup_meta = backup_root("930003") / "metadata.json"
    assert not backup_meta.is_file()

    monkeypatch.setattr(
        "services.metadata_backup_sync.default_mod_library",
        lambda: library,
        raising=False,
    )
    # Patch via core.paths used inside rebuild
    monkeypatch.setattr("core.paths.default_mod_library", lambda: library)

    created = rebuild_missing_metadata_backup(library)
    assert created >= 1
    assert backup_meta.is_file()
    snap = load_backup("930003")
    assert snap is not None
    assert snap.metadata.get("title") == "NeedsBackup"


def test_case4_backup_never_writes_back_to_info(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_mod(
        library, game="GameA", title="ModPri", mod_id="930004", meta_title="FromInfo"
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="930004",
            title="FromInfo",
            game_name="GameA",
            managed_path=str(folder),
        )
    )

    backup_meta = backup_root("930004") / "metadata.json"
    assert backup_meta.is_file()
    polluted = json.loads(backup_meta.read_text(encoding="utf-8"))
    polluted["title"] = "FromBackup"
    polluted["display_name"] = "FromBackup"
    backup_meta.write_text(
        json.dumps(polluted, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    info_before = (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    resolved = resolve_mod_metadata("930004", managed_path=str(folder))
    info_after = (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")

    assert info_before == info_after
    assert resolved is not None
    assert resolved.folder_present is True
    name = resolved.display_name or resolved.title
    assert name == "FromInfo"
    assert "FromBackup" not in info_after


def test_case5_offline_sync_copies_index(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_mod(
        library, game="GameA", title="ModOff", mod_id="930005", meta_title="OfflineMe"
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="930005",
            title="OfflineMe",
            game_name="GameA",
            managed_path=str(folder),
        )
    )

    offline_dir = folder / INFO_DIR_NAME / "offline"
    offline_dir.mkdir(parents=True, exist_ok=True)
    (offline_dir / "index.html").write_text(
        "<html><body>offline</body></html>", encoding="utf-8"
    )
    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    data["offline_page_path"] = ".info/offline/index.html"
    data["offline_status"] = "generated"
    persist_unified_metadata_dict(folder, data)
    sync_after_metadata_change("930005", folder, "offline_change")

    backup_index = backup_root("930005") / "offline" / "index.html"
    assert backup_index.is_file()
    assert "offline" in backup_index.read_text(encoding="utf-8")


def test_sync_forbids_backup_write_when_folder_missing(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "mod" / "GameA" / "NoFolder"
    # Seed an existing backup so we can detect mutation
    dest = backup_root("930006")
    dest.mkdir(parents=True)
    original = {"title": "Seed", "published_file_id": "930006"}
    meta = dest / "metadata.json"
    meta.write_text(json.dumps(original), encoding="utf-8")
    before = meta.read_text(encoding="utf-8")

    ok = sync_after_metadata_change("930006", missing, "edit")
    assert ok is False
    assert meta.read_text(encoding="utf-8") == before
