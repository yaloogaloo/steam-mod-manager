"""Witcher 3 ONLY game_version dimension — defaults, persistence, lifecycle."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import PLATFORM_MODIO, PLATFORM_NEXUS, PLATFORM_STEAM
from core.steam_api import SteamWorkshopClient
from core.witcher3_game_version import (
    WITCHER3_DEFAULT_VERSION,
    WITCHER3_VERSION_NEXT_GEN,
    WITCHER3_VERSION_ORIGINAL,
    WITCHER3_VERSION_REMAKE,
    is_valid_witcher3_game_version,
    is_witcher3_game,
    validate_witcher3_game_version,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.info_sidecar import apply_sidecar_to_db, write_sidecar_for_mod
from services.library_reconcile import reconcile_library
from services.metadata_refresh import refresh_steam_mod_metadata
from services.modio_api import map_mod_object
from services.modio_metadata_refresh import refresh_modio_mod_metadata
from services.path_lifecycle import record_filesystem_rename

W3 = 292030
PALWORLD = 1623730
ANNO = 916440


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "w3_version.db")
    manager.upsert_game(GameInfo(app_id=W3, name="巫师3", folder_name="巫师3"))
    manager.upsert_game(GameInfo(app_id=PALWORLD, name="Palworld", folder_name="Palworld"))
    manager.upsert_game(GameInfo(app_id=ANNO, name="Anno 1800", folder_name="Anno 1800"))
    yield manager
    DatabaseManager.reset_instance()


def _raw_game_version(database: DatabaseManager, mod_id: str | int) -> str | None:
    with database._lock:
        row = database._conn.execute(
            "SELECT game_version FROM mods WHERE mod_id = ?",
            (int(mod_id),),
        ).fetchone()
    if row is None:
        return None
    return row["game_version"]


def _create_w3_nexus(database: DatabaseManager, *, external_id: str = "73291"):
    return create_mod_identity(
        database,
        platform=PLATFORM_NEXUS,
        external_id=external_id,
        source_url=f"https://www.nexusmods.com/witcher3/mods/{external_id}",
        title="W3 Test Mod",
        app_id=W3,
        game_name="巫师3",
        operation="import",
    )


def _create_palworld_nexus(database: DatabaseManager, *, external_id: str = "88001"):
    return create_mod_identity(
        database,
        platform=PLATFORM_NEXUS,
        external_id=external_id,
        source_url=f"https://www.nexusmods.com/palworld/mods/{external_id}",
        title="Pal Test Mod",
        app_id=PALWORLD,
        game_name="Palworld",
        operation="import",
    )


def test_is_witcher3_game_uses_app_id_not_fuzzy_name() -> None:
    assert is_witcher3_game("", W3) is True
    assert is_witcher3_game("Anything", W3) is True
    assert is_witcher3_game("巫师3", 0) is True
    assert is_witcher3_game("巫师三", 0) is True
    assert is_witcher3_game("The Witcher 3: Wild Hunt", 0) is True
    assert is_witcher3_game("Palworld", PALWORLD) is False
    assert is_witcher3_game("Anno 1800", ANNO) is False
    # Positive non-Witcher App ID wins over a matching display name.
    assert is_witcher3_game("巫师3", PALWORLD) is False
    # No fuzzy / substring matching.
    assert is_witcher3_game("巫师3增强", 0) is False
    assert is_witcher3_game("witcher3_mod_pack", 0) is False
    assert is_witcher3_game("Some Witcher Game", 0) is False


def test_illegal_tokens_rejected() -> None:
    for bad in ("foo", "1.32", "4.0", "次世代版", "version"):
        assert is_valid_witcher3_game_version(bad) is False
        with pytest.raises(ValueError):
            validate_witcher3_game_version(bad)


def test_new_witcher3_mod_defaults_to_next_gen(db: DatabaseManager) -> None:
    created = _create_w3_nexus(db)
    info = db.get_mod_display_info(created.mod_id)
    assert info is not None
    assert info.game_version == WITCHER3_VERSION_NEXT_GEN
    assert _raw_game_version(db, created.mod_id) == WITCHER3_DEFAULT_VERSION


def test_new_steam_witcher3_upsert_defaults_to_next_gen(db: DatabaseManager) -> None:
    mid = "3591452801"
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title="Steam W3", app_id=W3),
        allow_insert=True,
    )
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.game_version == WITCHER3_VERSION_NEXT_GEN
    assert info.app_id == W3


def test_other_game_has_null_game_version(db: DatabaseManager) -> None:
    created = _create_palworld_nexus(db)
    info = db.get_mod_display_info(created.mod_id)
    assert info is not None
    assert info.game_version == ""
    assert _raw_game_version(db, created.mod_id) is None

    anno = create_mod_identity(
        db,
        platform=PLATFORM_MODIO,
        external_id="harborlife",
        source_url="https://mod.io/g/anno-1800/m/harborlife",
        title="Harbor",
        app_id=ANNO,
        game_name="Anno 1800",
        operation="import",
    )
    assert _raw_game_version(db, anno.mod_id) is None
    assert db.get_mod_display_info(anno.mod_id).game_version == ""


def test_legal_values_round_trip(db: DatabaseManager) -> None:
    created = _create_w3_nexus(db, external_id="73292")
    mid = created.mod_id
    for token in (
        WITCHER3_VERSION_ORIGINAL,
        WITCHER3_VERSION_NEXT_GEN,
        WITCHER3_VERSION_REMAKE,
    ):
        db.set_mod_game_version(mid, token)
        info = db.get_mod_display_info(mid)
        assert info is not None
        assert info.game_version == token
        assert _raw_game_version(db, mid) == token


def test_edit_transitions(db: DatabaseManager) -> None:
    created = _create_w3_nexus(db, external_id="73293")
    mid = created.mod_id
    db.set_mod_game_version(mid, WITCHER3_VERSION_NEXT_GEN)
    db.set_mod_game_version(mid, WITCHER3_VERSION_ORIGINAL)
    assert _raw_game_version(db, mid) == WITCHER3_VERSION_ORIGINAL
    db.set_mod_game_version(mid, WITCHER3_VERSION_REMAKE)
    assert _raw_game_version(db, mid) == WITCHER3_VERSION_REMAKE
    db.set_mod_game_version(mid, WITCHER3_VERSION_NEXT_GEN)
    assert _raw_game_version(db, mid) == WITCHER3_VERSION_NEXT_GEN


def test_illegal_value_does_not_save(db: DatabaseManager) -> None:
    created = _create_w3_nexus(db, external_id="73294")
    mid = created.mod_id
    db.set_mod_game_version(mid, WITCHER3_VERSION_ORIGINAL)
    for bad in ("foo", "1.32", "4.0", "次世代版"):
        with pytest.raises(ValueError):
            db.set_mod_game_version(mid, bad)
        assert _raw_game_version(db, mid) == WITCHER3_VERSION_ORIGINAL


def test_non_witcher3_set_forced_to_null(db: DatabaseManager) -> None:
    created = _create_palworld_nexus(db, external_id="88002")
    info = db.set_mod_game_version(created.mod_id, WITCHER3_VERSION_NEXT_GEN)
    assert info.game_version == ""
    assert _raw_game_version(db, created.mod_id) is None


def test_changing_game_version_does_not_change_identity(db: DatabaseManager) -> None:
    created = _create_w3_nexus(db, external_id="73295")
    before = db.get_mod_display_info(created.mod_id)
    assert before is not None
    snapshot = (
        before.mod_id,
        before.external_id,
        before.workspace_id,
        before.source_url,
        before.app_id,
        before.platform,
    )
    db.set_mod_game_version(created.mod_id, WITCHER3_VERSION_REMAKE)
    after = db.get_mod_display_info(created.mod_id)
    assert after is not None
    assert after.game_version == WITCHER3_VERSION_REMAKE
    assert (
        after.mod_id,
        after.external_id,
        after.workspace_id,
        after.source_url,
        after.app_id,
        after.platform,
    ) == snapshot
    # Same entity — not a second row for the same Nexus id.
    found = db.find_mod_by_external(PLATFORM_NEXUS, "73295", app_id=W3)
    assert found is not None
    assert found.mod_id == before.mod_id


def test_steam_refresh_preserves_game_version(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = "3591452802"
    lib = tmp_path / "mod"
    folder = lib / "巫师3" / "W3 Steam"
    info_dir = folder / INFO_DIR_NAME
    info_dir.mkdir(parents=True)
    (info_dir / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": "W3 Steam",
                "game_version": WITCHER3_VERSION_ORIGINAL,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (folder / "payload.zip").write_bytes(b"zip")
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title="W3 Steam", app_id=W3),
        allow_insert=True,
    )
    db.set_mod_game_version(mid, WITCHER3_VERSION_ORIGINAL)
    db.set_official_metadata_synced(mid, False)

    fresh = ModMetadata(
        published_file_id=mid,
        title="Official Steam Title",
        description="Official desc",
        app_id=W3,
    )
    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", MagicMock(return_value=[fresh]))
    monkeypatch.setattr(SteamWorkshopClient, "fetch_and_save_cover", lambda *a, **k: None)

    result = refresh_steam_mod_metadata(
        mid, folder, library_root=lib, force=True, db=db, allow_official_sync=True
    )
    assert result.success
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.game_version == WITCHER3_VERSION_ORIGINAL
    assert info.mod_id == mid
    assert info.external_id == mid


def test_modio_refresh_does_not_map_version_field(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "mod"
    folder = lib / "巫师3" / "W3 Modio"
    info_dir = folder / INFO_DIR_NAME
    info_dir.mkdir(parents=True)
    url = "https://mod.io/g/witcher3/m/w3sample"
    (info_dir / "metadata.json").write_text(
        json.dumps(
            {
                "title": "W3 Modio",
                "url": url,
                "source_type": "modio",
                "game_version": WITCHER3_VERSION_REMAKE,
                "version": "1.32",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    created = create_mod_identity(
        db,
        platform=PLATFORM_MODIO,
        external_id="w3sample",
        source_url=url,
        title="W3 Modio",
        app_id=W3,
        game_name="巫师3",
        operation="import",
    )
    db.set_mod_game_version(created.mod_id, WITCHER3_VERSION_REMAKE)
    db.set_official_metadata_synced(created.mod_id, False)

    payload = {
        "id": 777001,
        "game_id": 164,
        "name": "W3 Modio Official",
        "name_id": "w3sample",
        "summary": "sum",
        "description": "desc",
        "profile_url": url,
        "version": "4.0",
        "logo": {"original": "https://example.com/logo.png"},
        "submitted_by": {"username": "author"},
    }
    details = map_mod_object(payload)

    class FakeClient:
        def resolve_mod(self, **kwargs):
            return details

        def download_file(self, file_url, dest):
            Path(dest).write_bytes(b"\x89PNG\r\n\x1a\n")
            return Path(dest)

        def close(self):
            return None

    monkeypatch.setattr(
        "services.importers.image_picker.validate_cover_image",
        lambda path: Path(path),
    )
    result = refresh_modio_mod_metadata(
        created.mod_id,
        folder,
        library_root=lib,
        client=FakeClient(),  # type: ignore[arg-type]
        download_cover=False,
        allow_official_sync=True,
        db=db,
    )
    assert result.success
    info = db.get_mod_display_info(created.mod_id)
    assert info is not None
    assert info.game_version == WITCHER3_VERSION_REMAKE
    assert info.mod_id == created.mod_id
    disk_root = Path(result.managed_path or folder)
    disk = json.loads((disk_root / INFO_DIR_NAME / "metadata.json").read_text(encoding="utf-8"))
    assert disk.get("game_version") == WITCHER3_VERSION_REMAKE
    assert disk.get("game_version") != "4.0"
    assert disk.get("game_version") != "1.32"


def test_reconcile_preserves_game_version(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    created = _create_w3_nexus(db, external_id="10782")
    folder = library / "巫师3" / "Flashbacks"
    folder.mkdir(parents=True)
    (folder / "content.pak").write_bytes(b"pak")
    sidecar = {
        "published_file_id": "10782",
        "title": "Flashbacks",
        "display_name": "Flashbacks",
        "game_name": "巫师3",
        "source_type": "nexus",
        "url": "https://www.nexusmods.com/witcher3/mods/10782",
        "workspace_id": "10782",
        "external_id": "10782",
        "app_id": W3,
        "game_version": WITCHER3_VERSION_ORIGINAL,
    }
    (folder / INFO_DIR_NAME).mkdir()
    (folder / INFO_DIR_NAME / METADATA_FILENAME).write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    db.update_mod_identity_fields(
        created.mod_id,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        platform=PLATFORM_NEXUS,
        workspace_id="10782",
        external_id="10782",
        source_url="https://www.nexusmods.com/witcher3/mods/10782",
        app_id=W3,
        title="Flashbacks",
    )
    db.set_mod_game_version(created.mod_id, WITCHER3_VERSION_ORIGINAL)
    renamed = library / "巫师3" / "Flashbacks New"
    folder.rename(renamed)
    reconcile_library(library)
    info = db.get_mod_display_info(created.mod_id)
    assert info is not None
    assert info.game_version == WITCHER3_VERSION_ORIGINAL
    assert info.external_id == "10782"
    assert info.mod_id == created.mod_id


def test_rename_move_preserves_game_version(db: DatabaseManager, tmp_path: Path) -> None:
    lib = tmp_path / "mod"
    mid = "3591452803"
    old = lib / "巫师3" / "before"
    old.mkdir(parents=True)
    (old / INFO_DIR_NAME).mkdir()
    (old / INFO_DIR_NAME / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": "before",
                "game_version": WITCHER3_VERSION_REMAKE,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title="before", app_id=W3),
        allow_insert=True,
    )
    db.set_mod_game_version(mid, WITCHER3_VERSION_REMAKE)
    db.update_mod_identity_fields(mid, last_known_path=str(old.resolve()))
    new = lib / "巫师3" / "after"
    old.rename(new)
    result = record_filesystem_rename(mid, old, new, reason="refresh", db=db)
    assert result.success
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.game_version == WITCHER3_VERSION_REMAKE
    disk = json.loads((new / INFO_DIR_NAME / "metadata.json").read_text(encoding="utf-8"))
    assert disk.get("game_version") == WITCHER3_VERSION_REMAKE


def test_sidecar_round_trip_preserves_game_version(db: DatabaseManager, tmp_path: Path) -> None:
    created = _create_w3_nexus(db, external_id="73296")
    db.set_mod_game_version(created.mod_id, WITCHER3_VERSION_ORIGINAL)
    folder = tmp_path / "mod" / "巫师3" / "Side"
    folder.mkdir(parents=True)
    write_sidecar_for_mod(folder, created.mod_id, db=db)
    disk = json.loads((folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert disk.get("game_version") == WITCHER3_VERSION_ORIGINAL
    assert "version" not in disk or disk.get("version") != WITCHER3_VERSION_ORIGINAL
    db.set_mod_game_version(created.mod_id, WITCHER3_VERSION_NEXT_GEN)
    apply_sidecar_to_db(folder, mod_id=created.mod_id, db=db)
    info = db.get_mod_display_info(created.mod_id)
    assert info is not None
    assert info.game_version == WITCHER3_VERSION_ORIGINAL


def test_non_witcher_sidecar_omits_game_version(db: DatabaseManager, tmp_path: Path) -> None:
    created = _create_palworld_nexus(db, external_id="88003")
    folder = tmp_path / "mod" / "Palworld" / "P"
    folder.mkdir(parents=True)
    write_sidecar_for_mod(folder, created.mod_id, db=db)
    disk = json.loads((folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert "game_version" not in disk


def test_existing_data_migration(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE games (
            app_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            header_url TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE mods (
            mod_id INTEGER PRIMARY KEY,
            app_id INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            preview_url TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        INSERT INTO games VALUES (292030, '巫师3', '', '', '2020-01-01T00:00:00+00:00');
        INSERT INTO games VALUES (1623730, 'Palworld', '', '', '2020-01-01T00:00:00+00:00');
        INSERT INTO mods VALUES (11, 292030, 'Old W3', '', '', '2020-01-01T00:00:00+00:00');
        INSERT INTO mods VALUES (22, 1623730, 'Old Pal', '', '', '2020-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    DatabaseManager.reset_instance()
    manager = DatabaseManager(path)
    cols = {
        str(r[1]) for r in manager._conn.execute("PRAGMA table_info(mods)").fetchall()
    }
    assert "game_version" in cols
    assert _raw_game_version(manager, 11) == WITCHER3_VERSION_NEXT_GEN
    assert _raw_game_version(manager, 22) is None
    w3 = manager.get_mod_display_info(11)
    pal = manager.get_mod_display_info(22)
    assert w3 is not None and w3.game_version == WITCHER3_VERSION_NEXT_GEN
    assert pal is not None and pal.game_version == ""
    # Idempotent second open.
    manager.close()
    DatabaseManager.reset_instance()
    manager = DatabaseManager(path)
    assert _raw_game_version(manager, 11) == WITCHER3_VERSION_NEXT_GEN
    assert _raw_game_version(manager, 22) is None
    manager.close()
    DatabaseManager.reset_instance()


def test_migration_does_not_stamp_next_gen_on_other_games(tmp_path: Path) -> None:
    path = tmp_path / "legacy2.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE games (
            app_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            header_url TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE mods (
            mod_id INTEGER PRIMARY KEY,
            app_id INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            preview_url TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            game_version TEXT
        );
        INSERT INTO games VALUES (916440, 'Anno 1800', '', '', '2020-01-01T00:00:00+00:00');
        INSERT INTO mods VALUES (33, 916440, 'Anno Mod', '', '', '2020-01-01T00:00:00+00:00', 'next_gen');
        """
    )
    conn.commit()
    conn.close()
    DatabaseManager.reset_instance()
    manager = DatabaseManager(path)
    assert _raw_game_version(manager, 33) is None
    manager.close()
    DatabaseManager.reset_instance()


def test_edit_dialog_shows_version_only_for_witcher3() -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from ui.edit_mod_dialog import EditModDialog

    app = QApplication.instance()
    if app is None:
        app = QApplication([])

    pal = EditModDialog(
        mod_id="1",
        game_name="Palworld",
        game_id=PALWORLD,
        display_name="P",
    )
    assert pal._game_version_combo is None
    assert "game_version" not in pal.values()
    pal.close()

    anno = EditModDialog(mod_id="2", game_name="Anno 1800", game_id=ANNO)
    assert anno._game_version_combo is None
    anno.close()

    w3 = EditModDialog(
        mod_id="3",
        game_name="巫师3",
        game_id=W3,
        game_version=WITCHER3_VERSION_ORIGINAL,
    )
    assert w3._game_version_combo is not None
    labels = [w3._game_version_combo.itemText(i) for i in range(w3._game_version_combo.count())]
    assert labels == ["原版", "次世代版", "重制版"]
    assert w3._game_version_combo.currentData() == WITCHER3_VERSION_ORIGINAL
    assert w3._game_version_combo.currentText() == "原版"
    values = w3.values()
    assert values["game_version"] == WITCHER3_VERSION_ORIGINAL
    w3.close()

    batch = EditModDialog(
        mod_ids=["1", "2"],
        game_name="巫师3",
        game_id=W3,
        game_version=WITCHER3_VERSION_REMAKE,
    )
    assert batch._game_version_combo is None
    assert "game_version" not in batch.values()
    batch.close()


def _detail_folder(lib: Path, game: str, title: str, payload: dict) -> Path:
    folder = lib / game / title
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "content.pak").write_bytes(b"pak")
    return folder


def _meta_text(panel) -> str:
    return str(panel.meta_rich_label.text() or "")


def test_detail_panel_shows_witcher3_game_version_labels(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from ui.mod_detail_panel import ModDetailPanel

    app = QApplication.instance() or QApplication([])
    del app

    lib = tmp_path / "mod"
    mid = "3591452811"
    folder = _detail_folder(
        lib,
        "巫师3",
        "YenneferLook",
        {
            "published_file_id": mid,
            "title": "YenneferLook",
            "app_id": W3,
        },
    )
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title="YenneferLook", app_id=W3),
        allow_insert=True,
    )
    ident_before = db.get_mod_display_info(mid)
    assert ident_before is not None
    snapshot = (
        ident_before.mod_id,
        ident_before.external_id,
        ident_before.workspace_id,
        ident_before.source_url,
    )

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid, game_id=W3, game_name="巫师3")
    html = _meta_text(panel)
    assert "次世代版" in html
    assert "<b>版本：</b>" in html
    assert "next_gen" not in html
    assert ident_before.game_version == WITCHER3_VERSION_NEXT_GEN

    for token, label in (
        (WITCHER3_VERSION_ORIGINAL, "原版"),
        (WITCHER3_VERSION_REMAKE, "重制版"),
        (WITCHER3_VERSION_NEXT_GEN, "次世代版"),
    ):
        db.set_mod_game_version(mid, token)
        panel.show_mod(folder, mod_id=mid, game_id=W3, game_name="巫师3")
        html = _meta_text(panel)
        assert f"<b>版本：</b> {label}" in html
        assert token not in html.replace(label, "")
        for other in ("原版", "次世代版", "重制版"):
            if other != label:
                assert other not in html

    after = db.get_mod_display_info(mid)
    assert after is not None
    assert (
        after.mod_id,
        after.external_id,
        after.workspace_id,
        after.source_url,
    ) == snapshot
    panel.close()


def test_detail_panel_hides_game_version_for_other_games(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from ui.mod_detail_panel import ModDetailPanel

    app = QApplication.instance() or QApplication([])
    del app

    lib = tmp_path / "mod"
    mid = "3606624999"
    folder = _detail_folder(
        lib,
        "Palworld",
        "PlayablePals",
        {"published_file_id": mid, "title": "PlayablePals", "app_id": PALWORLD},
    )
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title="PlayablePals", app_id=PALWORLD),
        allow_insert=True,
    )
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid, game_id=PALWORLD, game_name="Palworld")
    html = _meta_text(panel)
    assert "次世代版" not in html
    assert "原版" not in html
    assert "重制版" not in html
    assert "next_gen" not in html
    # No Witcher 3 game_version row. Author mod_version is empty, so no 版本 line.
    assert "<b>版本：</b>" not in html
    panel.close()


def test_detail_panel_refresh_after_edit_dialog_save(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QDialog

    from ui.edit_mod_dialog import EditModDialog
    from ui.mod_detail_panel import ModDetailPanel

    app = QApplication.instance() or QApplication([])
    del app

    lib = tmp_path / "mod"
    mid = "3591452812"
    folder = _detail_folder(
        lib,
        "巫师3",
        "CiriHair",
        {"published_file_id": mid, "title": "CiriHair", "app_id": W3},
    )
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title="CiriHair", app_id=W3),
        allow_insert=True,
    )
    before = db.get_mod_display_info(mid)
    assert before is not None
    snapshot = (
        before.mod_id,
        before.external_id,
        before.workspace_id,
        before.source_url,
    )

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid, game_id=W3, game_name="巫师3")
    assert "次世代版" in _meta_text(panel)

    def _accept(self: EditModDialog) -> int:
        idx = self._game_version_combo.findData(WITCHER3_VERSION_ORIGINAL)
        self._game_version_combo.setCurrentIndex(idx)
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(EditModDialog, "exec", _accept)
    panel.open_edit_info_dialog()
    html = _meta_text(panel)
    assert "<b>版本：</b> 原版" in html
    assert "次世代版" not in html
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.game_version == WITCHER3_VERSION_ORIGINAL
    assert (
        info.mod_id,
        info.external_id,
        info.workspace_id,
        info.source_url,
    ) == snapshot
    panel.close()


def test_detail_panel_does_not_show_illegal_game_version(
    db: DatabaseManager, tmp_path: Path
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from ui.mod_detail_panel import ModDetailPanel

    app = QApplication.instance() or QApplication([])
    del app

    lib = tmp_path / "mod"
    mid = "3591452813"
    folder = _detail_folder(
        lib,
        "巫师3",
        "IllegalVer",
        {"published_file_id": mid, "title": "IllegalVer", "app_id": W3},
    )
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title="IllegalVer", app_id=W3),
        allow_insert=True,
    )
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET game_version = ? WHERE mod_id = ?",
            ("1.32", int(mid)),
        )
        db._conn.commit()
    ident = db.get_mod_display_info(mid)
    assert ident is not None
    snapshot = (
        ident.mod_id,
        ident.external_id,
        ident.workspace_id,
        ident.source_url,
    )
    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=mid, game_id=W3, game_name="巫师3")
    html = _meta_text(panel)
    assert "1.32" not in html
    assert "4.0" not in html
    assert "foo" not in html
    after = db.get_mod_display_info(mid)
    assert after is not None
    assert after.game_version == "1.32"
    assert (
        after.mod_id,
        after.external_id,
        after.workspace_id,
        after.source_url,
    ) == snapshot
    panel.close()

