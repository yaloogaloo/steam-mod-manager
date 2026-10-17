"""Offline HTML entry: Frozen UUID and SQLite PK both resolve via dal_mod_pk."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    PLATFORM_NEXUS,
    PROVIDER_NEXUS_MANUAL_IMPORT,
)
from services.file_ops import INFO_DIR_NAME
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from services.mod_library_cache import dal_mod_pk
from services.offline.manager import attach_nexus_offline_page
from services.offline.nexus_manual import NexusManualOfflineProvider
from tests.helpers.identity import bind_managed_path, create_test_mod_identity

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")
NEXUS_HTML = """<!DOCTYPE html><html><head>
<meta property="og:url" content="https://www.nexusmods.com/palworld/mods/{nid}">
<meta property="og:title" content="{title}">
</head><body><h1>{title}</h1><p>offline body</p></body></html>
"""


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "offline_html_identity.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed_nexus(
    tmp_path: Path,
    db: DatabaseManager,
    *,
    nexus_id: str,
    title: str,
) -> tuple[str, str, Path]:
    created = create_test_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        title=title,
        external_id=nexus_id,
        source_url=f"https://www.nexusmods.com/palworld/mods/{nexus_id}",
        app_id=1623730,
        game_name="Palworld",
    )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "").strip()
    assert frozen
    assert frozen != pk
    folder = tmp_path / "library" / "Palworld" / title
    folder.mkdir(parents=True)
    (folder / "mod.pak").write_bytes(b"pak")
    bind_managed_path(db, pk, folder, game_name="Palworld", title=title)
    return pk, frozen, folder


def _html(tmp_path: Path, *, nexus_id: str, title: str) -> Path:
    path = tmp_path / f"{nexus_id}.html"
    path.write_text(
        NEXUS_HTML.format(nid=nexus_id, title=title),
        encoding="utf-8",
    )
    return path


def test_dal_mod_pk_uuid_resolves_to_single_sqlite_pk(
    tmp_path: Path, db: DatabaseManager
) -> None:
    pk, frozen, _folder = _seed_nexus(tmp_path, db, nexus_id="33601", title="UuidPk")
    resolved = dal_mod_pk(frozen)
    assert resolved == pk
    assert resolved.isdigit()
    rows = db._conn.execute(
        "SELECT mod_id FROM mods WHERE TRIM(internal_id) = ?",
        (frozen,),
    ).fetchall()
    assert [str(r["mod_id"]) for r in rows] == [pk]


def test_uuid_input_attaches_offline_html(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen, folder = _seed_nexus(tmp_path, db, nexus_id="33602", title="UuidAttach")
    html = _html(tmp_path, nexus_id="33602", title="UuidAttach")
    looked_up: list[str] = []
    orig = DatabaseManager.get_mod_display_info

    def _spy(self, mod_id):
        looked_up.append(str(mod_id).strip())
        return orig(self, mod_id)

    monkeypatch.setattr(DatabaseManager, "get_mod_display_info", _spy)

    before = int(
        db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    )
    attach = attach_nexus_offline_page(
        frozen,
        html,
        managed_path=folder,
        library_root=tmp_path / "library",
    )
    after = int(
        db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    )

    assert attach.status == OFFLINE_STATUS_ARCHIVED
    assert attach.provider == PROVIDER_NEXUS_MANUAL_IMPORT
    assert str(attach.mod_id) == pk
    index = folder / INFO_DIR_NAME / "offline" / "index.html"
    assert index.is_file()
    assert "offline body" in index.read_text(encoding="utf-8")
    assert (folder / INFO_DIR_NAME / "offline" / "manifest.json").is_file()
    row = db.get_mod_display_info(pk)
    assert row is not None
    assert row.offline_status == OFFLINE_STATUS_ARCHIVED
    backup = db.get_mod_backup_row(pk) or {}
    assert str(backup.get("internal_id") or "") == frozen
    assert after == before
    assert frozen not in looked_up
    assert pk in looked_up


def test_pk_input_attaches_offline_html(tmp_path: Path, db: DatabaseManager) -> None:
    pk, frozen, folder = _seed_nexus(tmp_path, db, nexus_id="33603", title="PkAttach")
    html = _html(tmp_path, nexus_id="33603", title="PkAttach")
    attach = attach_nexus_offline_page(
        pk,
        html,
        managed_path=folder,
        library_root=tmp_path / "library",
    )
    assert attach.status == OFFLINE_STATUS_ARCHIVED
    assert str(attach.mod_id) == pk
    assert (folder / INFO_DIR_NAME / "offline" / "index.html").is_file()
    backup = db.get_mod_backup_row(pk) or {}
    assert str(backup.get("internal_id") or "") == frozen


def test_provider_uuid_and_pk_both_succeed(tmp_path: Path, db: DatabaseManager) -> None:
    pk, frozen, folder = _seed_nexus(tmp_path, db, nexus_id="33604", title="ProviderBoth")
    html = _html(tmp_path, nexus_id="33604", title="ProviderBoth")
    lib = tmp_path / "library"
    by_uuid = NexusManualOfflineProvider().import_offline_page(
        frozen, html, managed_path=folder, library_root=lib
    )
    assert by_uuid.status == OFFLINE_STATUS_ARCHIVED
    assert str(by_uuid.mod_id) == pk
    by_pk = NexusManualOfflineProvider().import_offline_page(
        pk, html, managed_path=folder, library_root=lib
    )
    assert by_pk.status == OFFLINE_STATUS_ARCHIVED
    assert str(by_pk.mod_id) == pk


def test_invalid_uuid_is_not_found(tmp_path: Path, db: DatabaseManager) -> None:
    _seed_nexus(tmp_path, db, nexus_id="33605", title="MissingUuid")
    html = _html(tmp_path, nexus_id="33605", title="MissingUuid")
    missing = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert dal_mod_pk(missing) == ""
    with pytest.raises(ValueError, match="Mod not found in database"):
        attach_nexus_offline_page(
            missing,
            html,
            managed_path=tmp_path / "library" / "Palworld" / "MissingUuid",
            library_root=tmp_path / "library",
        )


def test_invalid_pk_is_not_found(tmp_path: Path, db: DatabaseManager) -> None:
    _seed_nexus(tmp_path, db, nexus_id="33606", title="MissingPk")
    html = _html(tmp_path, nexus_id="33606", title="MissingPk")
    missing_pk = "999999999"
    assert dal_mod_pk(missing_pk) == missing_pk
    with pytest.raises(ValueError, match="Mod not found in database"):
        attach_nexus_offline_page(
            missing_pk,
            html,
            managed_path=tmp_path / "library" / "Palworld" / "MissingPk",
            library_root=tmp_path / "library",
        )


def test_nexus_import_pk_path_still_attaches(
    tmp_path: Path, db: DatabaseManager
) -> None:
    src = tmp_path / "mod"
    src.mkdir()
    (src / "mod.pak").write_bytes(b"pak")
    html = _html(tmp_path, nexus_id="33607", title="ImportPk")
    lib = tmp_path / "library"
    result = NexusImporter(db=db).import_mod(
        source_folder=src,
        title="ImportPk",
        nexus_url="https://www.nexusmods.com/palworld/mods/33607",
        nexus_id="33607",
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    assert str(result.mod_id).isdigit()
    before = int(
        db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    )
    attach = attach_nexus_offline_page(
        result.mod_id,
        html,
        managed_path=result.managed_path,
        library_root=lib,
    )
    after = int(
        db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    )
    assert attach.status == OFFLINE_STATUS_ARCHIVED
    assert str(attach.mod_id) == str(result.mod_id)
    index = Path(result.managed_path) / INFO_DIR_NAME / "offline" / "index.html"
    assert index.is_file()
    row = db.get_mod_display_info(result.mod_id)
    assert row is not None
    assert row.offline_status == OFFLINE_STATUS_ARCHIVED
    backup = db.get_mod_backup_row(result.mod_id) or {}
    frozen = str(backup.get("internal_id") or "")
    assert frozen
    assert frozen != str(result.mod_id)
    assert after == before
