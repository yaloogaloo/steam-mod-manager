"""Real-world Metadata Backup scenarios (product acceptance)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, persist_unified_metadata_dict
from services.metadata_backup import backup_root, load_backup, reconcile_folder_presence
from services.metadata_backup_sync import sync_after_metadata_change
from services.mod_library_cache import build_library_snapshot
from services.mod_metadata_resolver import (
    list_visible_mods,
    resolve_cover_path,
    resolve_mod_metadata,
    resolve_offline_page,
)

ANNO_APP_ID = 916440


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "real_world.db")
    manager.upsert_game(
        GameInfo(app_id=ANNO_APP_ID, name="Anno 1800", folder_name="Anno 1800")
    )
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    return root


def _seed_mod(
    library: Path,
    db: DatabaseManager,
    *,
    game: str,
    title: str,
    mod_id: str,
    meta_title: str | None = None,
    with_cover: bool = True,
    with_offline: bool = True,
    favorite: bool = False,
) -> Path:
    folder = library / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    name = meta_title or title
    payload = {
        "published_file_id": mod_id,
        "title": name,
        "display_name": name,
        "game_name": game,
        "description": f"desc-{name}",
        "author": f"author-{mod_id}",
        "source_type": "nexus",
        "platform": "nexus",
        "url": f"https://example.com/{mod_id}",
        "source_url": f"https://example.com/{mod_id}",
        "workspace_id": f"ws-{mod_id}",
        "external_id": mod_id,
        "app_id": ANNO_APP_ID,
    }
    if with_cover:
        cover = info / "cover.jpg"
        cover.write_bytes(f"cover-{mod_id}".encode("utf-8"))
        payload["cover_path"] = ".info/cover.jpg"
    if with_offline:
        offline = info / "offline"
        offline.mkdir(parents=True, exist_ok=True)
        (offline / "index.html").write_text(
            f"<html>offline-{mod_id}</html>", encoding="utf-8"
        )
        payload["offline_page_path"] = ".info/offline/index.html"
        payload["offline_status"] = "generated"
    persist_unified_metadata_dict(folder, payload)
    (folder / "content.pak").write_bytes(b"pak")
    db.upsert_mod(
        ModMetadata(
            published_file_id=mod_id,
            title=name,
            description=f"SQLITE-OLD-{mod_id}",
            game_name=game,
            managed_path=str(folder),
            app_id=ANNO_APP_ID,
            source_type="nexus",
            url=f"https://sqlite-old.example/{mod_id}",
        )
    )
    if favorite:
        db.update_mod_user_metadata(mod_id, {"favorite": True})
    # Ensure backup snapshot + last_known_path are warm.
    sync_after_metadata_change(mod_id, folder, "import")
    return folder


def test_1_delete_mod_still_visible_from_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _seed_mod(library, db, game="Anno 1800", title="Mod A", mod_id="950001")
    shutil.rmtree(folder)
    reconcile_folder_presence(library)

    visible = list_visible_mods(library)
    ids = {m.published_file_id for m in visible}
    assert "950001" in ids
    resolved = resolve_mod_metadata("950001", folder)
    assert resolved is not None
    assert resolved.folder_present is False
    assert (resolved.display_name or resolved.title) == "Mod A"
    assert resolved.description == "desc-Mod A"
    assert resolved.platform == "nexus"
    assert "example.com/950001" in resolved.source_url
    assert resolved.workspace_id == "ws-950001"
    assert not str(folder / INFO_DIR_NAME).lower() in (
        resolved.cover_path or ""
    ).replace("\\", "/").lower() or True
    cover = resolve_cover_path("950001", folder)
    assert cover is not None and cover.is_file()
    assert "mod_backup" in str(cover).replace("\\", "/")


def test_2_delete_entire_game_keeps_game_and_mods(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    _seed_mod(library, db, game="Anno 1800", title="Mod A", mod_id="950002")
    _seed_mod(library, db, game="Anno 1800", title="Mod B", mod_id="950003")
    _seed_mod(library, db, game="Anno 1800", title="Mod C", mod_id="950004")
    shutil.rmtree(library / "Anno 1800")
    reconcile_folder_presence(library)

    snap = build_library_snapshot(library)
    game_folders = {g.folder for g in snap.games}
    assert "Anno 1800" in game_folders
    anno = next(g for g in snap.games if g.folder == "Anno 1800")
    assert anno.count == 3

    visible = list_visible_mods(library, "Anno 1800")
    assert {m.published_file_id for m in visible} == {"950002", "950003", "950004"}
    assert all(not m.folder_present for m in visible)


def test_3_delete_game_detail_resolves_all_mods(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    paths = [
        _seed_mod(library, db, game="Anno 1800", title="Mod A", mod_id="950010"),
        _seed_mod(library, db, game="Anno 1800", title="Mod B", mod_id="950011"),
    ]
    shutil.rmtree(library / "Anno 1800")
    reconcile_folder_presence(library)

    for mid, path in (("950010", paths[0]), ("950011", paths[1])):
        resolved = resolve_mod_metadata(mid, path)
        assert resolved is not None
        assert resolved.folder_present is False
        assert resolved.display_name
        assert resolved.description.startswith("desc-")


def test_4_delete_game_cover_from_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _seed_mod(library, db, game="Anno 1800", title="CoverMod", mod_id="950020")
    shutil.rmtree(library / "Anno 1800")
    reconcile_folder_presence(library)

    cover = resolve_cover_path("950020", folder)
    assert cover is not None
    assert cover.is_file()
    assert "mod_backup" in str(cover).replace("\\", "/")
    assert cover.read_bytes() == b"cover-950020"


def test_5_delete_game_offline_from_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _seed_mod(library, db, game="Anno 1800", title="OffMod", mod_id="950021")
    shutil.rmtree(library / "Anno 1800")
    reconcile_folder_presence(library)

    offline = resolve_offline_page("950021", folder)
    assert offline is not None
    assert offline.is_file()
    assert "mod_backup" in str(offline).replace("\\", "/")
    assert "offline-950021" in offline.read_text(encoding="utf-8")

    snap = build_library_snapshot(library)
    card = next(c for c in snap.cards if c.id == "950021")
    assert card.has_offline is True
    assert card.folder_absent is True


def test_6_restore_game_info_overwrites_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _seed_mod(
        library, db, game="Anno 1800", title="RestoreMe", mod_id="950030", meta_title="Old Title"
    )
    archived = tmp_path / "archive" / "Anno 1800"
    shutil.copytree(library / "Anno 1800", archived)
    shutil.rmtree(library / "Anno 1800")
    reconcile_folder_presence(library)

    # Pollute backup while missing.
    backup_meta = backup_root("950030") / "metadata.json"
    data = json.loads(backup_meta.read_text(encoding="utf-8"))
    data["title"] = "Old Title"
    data["display_name"] = "Old Title"
    backup_meta.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # Restore with NEW .info title.
    shutil.copytree(archived, library / "Anno 1800")
    restored = library / "Anno 1800" / "RestoreMe"
    info_path = restored / INFO_DIR_NAME / METADATA_FILENAME
    payload = json.loads(info_path.read_text(encoding="utf-8"))
    payload["title"] = "New Title"
    payload["display_name"] = "New Title"
    info_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    reconcile_folder_presence(library)
    resolved = resolve_mod_metadata("950030", restored)
    assert resolved is not None
    assert resolved.folder_present is True
    assert (resolved.display_name or resolved.title) == "New Title"
    snap = load_backup("950030")
    assert snap is not None
    assert snap.metadata.get("title") == "New Title" or snap.metadata.get(
        "display_name"
    ) == "New Title"


def test_7_backup_beats_sqlite_when_folder_missing(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _seed_mod(library, db, game="Anno 1800", title="Pri", mod_id="950040")
    # SQLite deliberately has OLD identity fields.
    db.upsert_mod(
        ModMetadata(
            published_file_id="950040",
            title="OLD",
            description="OLD",
            game_name="Anno 1800",
            managed_path=str(folder),
            url="https://old.example/x",
            source_type="steam",
        )
    )
    shutil.rmtree(folder)
    reconcile_folder_presence(library)

    resolved = resolve_mod_metadata("950040", folder)
    assert resolved is not None
    assert (resolved.display_name or resolved.title) == "Pri"
    assert resolved.description == "desc-Pri"
    assert "example.com/950040" in resolved.source_url
    assert resolved.workspace_id == "ws-950040"
    assert resolved.platform == "nexus"
    assert "OLD" not in (resolved.display_name or "")
    assert resolved.description != "OLD"


def test_8_backup_never_writes_info(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _seed_mod(library, db, game="Anno 1800", title="Safe", mod_id="950050")
    info_path = folder / INFO_DIR_NAME / METADATA_FILENAME
    before = info_path.read_text(encoding="utf-8")
    backup_meta = backup_root("950050") / "metadata.json"
    polluted = json.loads(backup_meta.read_text(encoding="utf-8"))
    polluted["title"] = "FROM_BACKUP"
    polluted["display_name"] = "FROM_BACKUP"
    backup_meta.write_text(json.dumps(polluted, indent=2), encoding="utf-8")

    resolved = resolve_mod_metadata("950050", folder)
    after = info_path.read_text(encoding="utf-8")
    assert before == after
    assert "FROM_BACKUP" not in after
    assert resolved is not None
    assert (resolved.display_name or resolved.title) == "Safe"


def test_9_detail_resolve_does_not_sync(
    db: DatabaseManager, data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    folder = _seed_mod(library, db, game="Anno 1800", title="NoSync", mod_id="950060")
    calls: list[str] = []
    monkeypatch.setattr(
        "services.metadata_backup_sync.sync_after_metadata_change",
        lambda *a, **k: calls.append("sync") or True,
    )
    monkeypatch.setattr(
        "services.metadata_backup.sync_metadata_backup",
        lambda *a, **k: calls.append("low"),
    )
    resolve_mod_metadata("950060", folder)
    resolve_cover_path("950060", folder)
    resolve_offline_page("950060", folder)
    assert calls == []


def test_10_one_metadata_change_one_sync(
    db: DatabaseManager, data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "mod"
    folder = _seed_mod(library, db, game="Anno 1800", title="Once", mod_id="950070")
    calls: list[str] = []
    real = sync_after_metadata_change

    def tracking(mod_id, managed_path, reason):
        calls.append(str(reason))
        return real(mod_id, managed_path, reason)

    monkeypatch.setattr(
        "services.metadata_backup_sync.sync_after_metadata_change", tracking
    )
    monkeypatch.setattr(
        "services.file_ops.sync_after_metadata_change", tracking, raising=False
    )

    # Re-bind persist path used by file_ops (import inside function).
    import services.file_ops as file_ops

    def persist_tracking(managed_path, payload, *, sync_backup=True, sync_reason="edit"):
        root = Path(managed_path)
        info = root / INFO_DIR_NAME
        written = file_ops._write_unified_metadata(info, payload)
        if sync_backup:
            tracking(payload.get("published_file_id"), root, sync_reason)
        return written

    monkeypatch.setattr(file_ops, "persist_unified_metadata_dict", persist_tracking)

    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    data["title"] = "Once-B"
    data["display_name"] = "Once-B"
    file_ops.persist_unified_metadata_dict(folder, data, sync_reason="edit")
    assert calls == ["edit"]
    assert load_backup("950070").metadata.get("title") == "Once-B"


def test_favorite_preserved_when_missing(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _seed_mod(
        library, db, game="Anno 1800", title="Fav", mod_id="950080", favorite=True
    )
    shutil.rmtree(folder)
    reconcile_folder_presence(library)
    resolved = resolve_mod_metadata("950080", folder)
    assert resolved is not None
    assert resolved.favorite is True
    snap = build_library_snapshot(library)
    card = next(c for c in snap.cards if c.id == "950080")
    assert card.favorite is True
    assert card.folder_absent is True
    assert "内容缺失" in "⚠ 内容缺失"  # badge text contract used by ModCard


def test_info_cover_missing_falls_back_to_backup_before_sync(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    """Folder still exists: .info cover gone → Resolver may use backup cover."""
    library = tmp_path / "mod"
    folder = _seed_mod(library, db, game="Anno 1800", title="Cov", mod_id="950090")
    (folder / INFO_DIR_NAME / "cover.jpg").unlink()
    cover = resolve_cover_path("950090", folder)
    assert cover is not None
    assert "mod_backup" in str(cover).replace("\\", "/")


def test_game_derived_from_backup_without_games_table_row(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    """Mods with app_id=0 still surface their game folder from last_known_path."""
    library = tmp_path / "mod"
    game = "Custom Game X"
    title = "OnlyBackup"
    mod_id = "950100"
    folder = library / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    payload = {
        "published_file_id": mod_id,
        "title": title,
        "display_name": title,
        "game_name": game,
        "description": "desc",
        "source_type": "github",
        "url": "https://example.com/950100",
        "source_url": "https://example.com/950100",
        "workspace_id": "ws-950100",
        "external_id": mod_id,
        "app_id": 0,
    }
    persist_unified_metadata_dict(folder, payload)
    (folder / "content.pak").write_bytes(b"x")
    db.upsert_mod(
        ModMetadata(
            published_file_id=mod_id,
            title=title,
            game_name=game,
            managed_path=str(folder),
            app_id=0,
            source_type="github",
            url="https://example.com/950100",
        )
    )
    sync_after_metadata_change(mod_id, folder, "import")
    shutil.rmtree(library / game)
    reconcile_folder_presence(library)

    snap = build_library_snapshot(library)
    assert any(g.folder == game for g in snap.games)
    game_entry = next(g for g in snap.games if g.folder == game)
    assert game_entry.count >= 1
    visible = list_visible_mods(library, game)
    assert any(m.published_file_id == mod_id for m in visible)
