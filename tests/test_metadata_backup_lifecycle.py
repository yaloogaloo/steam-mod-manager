"""Phase 3-A: metadata backup lifecycle (.info → backup only)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, persist_unified_metadata_dict
from services.metadata_backup import backup_root, load_backup
from services.metadata_backup_sync import (
    drain_backup_queue,
    rebuild_missing_metadata_backup,
    sync_after_metadata_change,
)
from services.mod_metadata_resolver import resolve_mod_metadata
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    write_info_sidecar,
)

APP_ID = 4242


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "lifecycle.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name="GameA", folder_name="GameA"))
    yield manager
    drain_backup_queue(timeout=5.0)
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    return root


def _register(
    db: DatabaseManager, *, workshop_id: str, title: str, folder: Path
) -> tuple[str, str]:
    """Create Steam entity; return (mods.mod_id PK, Entity internal_id)."""
    created = create_steam_test_mod(
        db,
        external_id=str(workshop_id),
        title=title,
        app_id=APP_ID,
        game_name="GameA",
    )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "").strip()
    bind_managed_path(db, pk, folder, game_name="GameA", title=title)
    return pk, frozen


def _write_mod(
    library: Path,
    db: DatabaseManager,
    *,
    game: str,
    title: str,
    workshop_id: str,
    meta_title: str = "",
) -> tuple[Path, str]:
    folder = library / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    pk, frozen = _register(
        db, workshop_id=workshop_id, title=meta_title or title, folder=folder
    )
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title=meta_title or title,
        external_id=workshop_id,
        workspace_id=str(workshop_id),
        app_id=APP_ID,
        game_name=game,
        extra={
            "display_name": meta_title or title,
            "description": f"desc-{workshop_id}",
            "source_type": "steam",
            "url": (
                "https://steamcommunity.com/sharedfiles/filedetails/"
                f"?id={workshop_id}"
            ),
            "source_url": (
                "https://steamcommunity.com/sharedfiles/filedetails/"
                f"?id={workshop_id}"
            ),
            "published_file_id": workshop_id,
        },
    )
    drain_backup_queue(timeout=5.0)
    (folder / "content.txt").write_text("payload", encoding="utf-8")
    sync_after_metadata_change(pk, folder, "import", wait=True)
    return folder, pk


def test_case1_info_change_syncs_backup_title(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk = _write_mod(
        library, db, game="GameA", title="ModA", workshop_id="930001", meta_title="A"
    )
    snap = load_backup(pk)
    assert snap is not None
    assert snap.metadata.get("title") == "A"

    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    data["title"] = "B"
    data["display_name"] = "B"
    persist_unified_metadata_dict(folder, data)
    drain_backup_queue(timeout=5.0)

    snap2 = load_backup(pk)
    assert snap2 is not None
    assert snap2.metadata.get("title") == "B"


def test_case2_deleted_folder_ui_uses_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk = _write_mod(
        library,
        db,
        game="GameA",
        title="ModGone",
        workshop_id="930002",
        meta_title="KeepMe",
    )
    assert load_backup(pk) is not None

    shutil.rmtree(folder)
    assert not folder.exists()

    resolved = resolve_mod_metadata(pk, managed_path=str(folder))
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
    pk, frozen = _register(db, workshop_id="930003", title="NeedsBackup", folder=folder)
    payload = {
        "internal_id": frozen,
        "published_file_id": "930003",
        "title": "NeedsBackup",
        "display_name": "NeedsBackup",
        "source_type": "steam",
        "url": "https://steamcommunity.com/sharedfiles/filedetails/?id=930003",
        "source_url": "https://steamcommunity.com/sharedfiles/filedetails/?id=930003",
        "workspace_id": "930003",
        "external_id": "930003",
    }
    # Write .info without going through persist (which would auto-sync).
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (folder / "content.txt").write_text("x", encoding="utf-8")

    backup_meta = backup_root(pk) / "metadata.json"
    assert not backup_meta.is_file()

    monkeypatch.setattr(
        "services.metadata_backup_sync.default_mod_library",
        lambda: library,
        raising=False,
    )
    monkeypatch.setattr("core.paths.default_mod_library", lambda: library)

    created = rebuild_missing_metadata_backup(library)
    assert created >= 1
    assert backup_meta.is_file()
    snap = load_backup(pk)
    assert snap is not None
    assert snap.metadata.get("title") == "NeedsBackup"


def test_case4_backup_never_writes_back_to_info(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk = _write_mod(
        library,
        db,
        game="GameA",
        title="ModPri",
        workshop_id="930004",
        meta_title="FromInfo",
    )

    backup_meta = backup_root(pk) / "metadata.json"
    assert backup_meta.is_file()
    polluted = json.loads(backup_meta.read_text(encoding="utf-8"))
    polluted["title"] = "FromBackup"
    polluted["display_name"] = "FromBackup"
    backup_meta.write_text(
        json.dumps(polluted, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    info_before = (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    resolved = resolve_mod_metadata(pk, managed_path=str(folder))
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
    folder, pk = _write_mod(
        library,
        db,
        game="GameA",
        title="ModOff",
        workshop_id="930005",
        meta_title="OfflineMe",
    )

    offline_dir = folder / INFO_DIR_NAME / "offline"
    offline_dir.mkdir(parents=True, exist_ok=True)
    (offline_dir / "index.html").write_text(
        "<html><body>offline</body></html>", encoding="utf-8"
    )
    assets = offline_dir / "assets"
    assets.mkdir()
    (assets / "all.css").write_text("body{}", encoding="utf-8")
    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    data["offline_page_path"] = ".info/offline/index.html"
    data["offline_status"] = "generated"
    persist_unified_metadata_dict(folder, data)
    sync_after_metadata_change(pk, folder, "offline_change", wait=True)

    backup_index = backup_root(pk) / "offline" / "index.html"
    assert backup_index.is_file()
    assert "offline" in backup_index.read_text(encoding="utf-8")
    assert not (backup_root(pk) / "offline" / "assets").exists()
    assert (assets / "all.css").is_file()


def test_sync_forbids_backup_write_when_folder_missing(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    missing = tmp_path / "mod" / "GameA" / "NoFolder"
    created = create_steam_test_mod(
        db,
        external_id="930006",
        title="Seed",
        app_id=APP_ID,
        game_name="GameA",
    )
    pk = str(created.mod_id)
    # Seed an existing backup so we can detect mutation
    dest = backup_root(pk)
    dest.mkdir(parents=True)
    original = {"title": "Seed", "published_file_id": "930006"}
    meta = dest / "metadata.json"
    meta.write_text(json.dumps(original), encoding="utf-8")
    before = meta.read_text(encoding="utf-8")

    ok = sync_after_metadata_change(pk, missing, "edit", wait=True)
    assert ok is False
    assert meta.read_text(encoding="utf-8") == before
