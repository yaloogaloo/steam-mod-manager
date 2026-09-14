"""Backup-backed MISS lifecycle, source capability, and safe recovery."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, read_info_metadata_dict
from services.identity_service import create_mod_identity, identity_create_scope
from services.metadata_backup import (
    BACKUP_OFFLINE_DIR,
    backup_root,
    mark_missing,
    reconcile_folder_presence,
)
from services.metadata_backup_sync import drain_backup_queue, sync_after_metadata_change
from services.mod_metadata_resolver import resolve_cover_path, resolve_mod_metadata, resolve_offline_page
from services.mod_presence import (
    ENTITY_ABSENT,
    ENTITY_LIVE,
    ENTITY_MISS,
    SOURCE_LOCAL_FOLDER,
    SOURCE_WORKSHOP,
    attempt_recovery,
    backup_offline_index,
    deployment_capability,
    entity_state,
    persist_entity_metadata_to_backup,
    persist_miss_cover,
    presence_projection,
)
from tests.helpers.identity import bind_managed_path, write_info_sidecar

STELLARIS = 281990
ANNO = 916440
WH3 = 1142710


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "miss.db")
    yield manager
    drain_backup_queue(timeout=5.0)
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    monkeypatch.setattr("core.paths.data_dir", lambda: root)
    return root


@pytest.fixture(autouse=True)
def _patch_get_db(db: DatabaseManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)


def _create(
    db: DatabaseManager,
    *,
    library: Path,
    game: str,
    folder: str,
    workshop_id: str,
    app_id: int,
    title: str,
) -> tuple[Path, str, str]:
    db.upsert_game(GameInfo(app_id=app_id, name=game, folder_name=game))
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(workshop_id),
            workshop_id=str(workshop_id),
            title=title,
            app_id=app_id,
            game_name=game,
            operation="import",
        )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "")
    assert frozen, "Frozen internal_id must be minted by IdentityService"
    path = library / game / folder
    path.mkdir(parents=True)
    (path / "payload.txt").write_text("body", encoding="utf-8")
    write_info_sidecar(
        path,
        internal_id=frozen,
        title=title,
        external_id=str(workshop_id),
        workspace_id=str(created.workspace_id or workshop_id),
        app_id=app_id,
        game_name=game,
        extra={"updated_at": "2020-01-01T00:00:00+00:00", "author": "AuthorA"},
    )
    bind_managed_path(db, pk, path, game_name=game, title=title)
    db.update_mod_identity_fields(pk, workspace_id=str(created.workspace_id or workshop_id))
    sync_after_metadata_change(pk, path, "edit", wait=True)
    return path, pk, frozen


def _make_miss(folder: Path, pk: str) -> None:
    shutil.rmtree(folder)
    mark_missing(pk)
    assert not folder.exists()


def test_missing_dir_with_backup_is_miss(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db,
        library=library,
        game="GameA",
        folder="Alpha",
        workshop_id="88001",
        app_id=4242,
        title="KeepMe",
    )
    assert (backup_root(pk) / "metadata.json").is_file()
    _make_miss(folder, pk)
    assert entity_state(pk, db=db) == ENTITY_MISS
    rows = db.list_mod_list_items()
    assert any(str(r.get("internal_id")) == pk for r in rows)
    resolved = resolve_mod_metadata(pk, managed_path=str(folder))
    assert resolved is not None
    assert resolved.folder_present is False
    assert "KeepMe" in (resolved.display_name or resolved.title)
    row = db.get_mod_backup_row(pk)
    assert str(row.get("internal_id") or "") == frozen
    assert db.get_mod(pk) is not None


def test_missing_dir_without_backup_is_not_backup_managed_miss(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, _frozen = _create(
        db,
        library=library,
        game="GameA",
        folder="NoBak",
        workshop_id="88002",
        app_id=4242,
        title="Ghost",
    )
    shutil.rmtree(backup_root(pk), ignore_errors=True)
    _make_miss(folder, pk)
    assert entity_state(pk, db=db) == ENTITY_ABSENT
    assert entity_state(pk, db=db) != ENTITY_MISS
    assert any(str(r.get("internal_id")) == pk for r in db.list_mod_list_items())


def test_miss_cover_and_offline_from_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, _frozen = _create(
        db,
        library=library,
        game="GameA",
        folder="Assets",
        workshop_id="88003",
        app_id=4242,
        title="CoverMe",
    )
    png = tmp_path / "cover.png"
    png.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\r\x18\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    from services.importers.image_picker import apply_cover_to_mod

    apply_cover_to_mod(folder, png, mod_id=pk, update_db=True)
    offline = folder / INFO_DIR_NAME / "offline"
    offline.mkdir(parents=True, exist_ok=True)
    (offline / "index.html").write_text("<html>offline</html>", encoding="utf-8")
    from core.paths import asset_store_dir
    from services.asset_store import AssetStore
    from services.info_asset_runtime import finalize_live_offline_to_cas

    assert finalize_live_offline_to_cas(
        folder, store=AssetStore(root=asset_store_dir())
    ).ok
    sync_after_metadata_change(pk, folder, "edit", wait=True)
    _make_miss(folder, pk)
    cover = resolve_cover_path(pk, folder)
    assert cover is not None and cover.is_file()
    off = resolve_offline_page(pk, folder)
    assert off is not None and off.is_file()
    assert off.name == "index.html"
    assert "offline_view" in str(off).replace("\\", "/")
    bak = backup_offline_index(pk)
    assert bak is not None and bak.is_file()
    assert BACKUP_OFFLINE_DIR in str(bak)


def test_miss_edit_persists_backup_without_creating_folder(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db,
        library=library,
        game="GameA",
        folder="EditMe",
        workshop_id="88004",
        app_id=4242,
        title="Before",
    )
    _make_miss(folder, pk)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "AfterMISS",
            "custom_description": "edited-in-miss",
            "user_notes": "",
            "favorite": False,
            "platform": PLATFORM_STEAM,
            "source_url": "https://example.test/mod",
        },
    )
    assert persist_entity_metadata_to_backup(pk, db=db) is True
    assert not folder.exists()
    assert not (folder / INFO_DIR_NAME).exists()
    meta = json.loads((backup_root(pk) / "metadata.json").read_text(encoding="utf-8"))
    assert meta.get("display_name") == "AfterMISS"
    from services.mod_identity import read_entity_key

    assert read_entity_key(meta) == frozen
    resolved = resolve_mod_metadata(pk, managed_path=str(folder))
    assert resolved is not None
    assert resolved.display_name == "AfterMISS"
    info = db.get_mod_display_info(pk)
    assert info is not None
    assert info.user_display_name == "AfterMISS" or info.display_name == "AfterMISS"
    row = db.get_mod_backup_row(pk)
    assert str(row.get("internal_id") or "") == frozen


def test_miss_edit_survives_restart_reload(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, _frozen = _create(
        db,
        library=library,
        game="GameA",
        folder="Restart",
        workshop_id="88005",
        app_id=4242,
        title="Old",
    )
    _make_miss(folder, pk)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "Restarted",
            "custom_description": "keep",
            "user_notes": "",
            "favorite": False,
        },
    )
    persist_entity_metadata_to_backup(pk, db=db)
    resolved = resolve_mod_metadata(pk, managed_path=str(folder))
    assert resolved is not None
    assert resolved.display_name == "Restarted"


def test_miss_cover_write_does_not_create_mod_folder(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, _frozen = _create(
        db,
        library=library,
        game="GameA",
        folder="CoverWrite",
        workshop_id="88006",
        app_id=4242,
        title="C",
    )
    _make_miss(folder, pk)
    png = tmp_path / "c.png"
    png.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x00\x05\x18\r\x18\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    rel = persist_miss_cover(pk, png, db=db)
    assert rel
    assert not folder.exists()
    assert not (folder / INFO_DIR_NAME).exists()
    cover = resolve_cover_path(pk, folder)
    assert cover is not None and cover.is_file()


def test_action_gating_local_source_miss_disables_deploy(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    db.upsert_game(GameInfo(app_id=ANNO, name="Anno 1800", folder_name="Anno 1800"))
    db.update_game_deploy_config(
        ANNO,
        name="Anno 1800",
        install_path=str(tmp_path / "AnnoInstall"),
        deploy_type="folder_copy",
    )
    library = tmp_path / "mod"
    folder, pk, _frozen = _create(
        db,
        library=library,
        game="Anno 1800",
        folder="City",
        workshop_id="88010",
        app_id=ANNO,
        title="City",
    )
    _make_miss(folder, pk)
    cap = deployment_capability(pk, db=db)
    assert cap.source_kind == SOURCE_LOCAL_FOLDER
    assert cap.allowed is False
    proj = presence_projection(pk, db=db)
    assert proj.open_directory is False
    assert proj.edit_metadata is True
    assert proj.filesystem_actions is False
    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out.get("success") is False


def test_action_gating_workshop_available_allows_deploy(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    user_dir = tmp_path / "Paradox" / "Stellaris"
    workshop = tmp_path / "workshop" / "content" / str(STELLARIS)
    user_dir.mkdir(parents=True)
    workshop.mkdir(parents=True)
    (user_dir / "dlc_load.json").write_text(
        json.dumps({"enabled_mods": [], "disabled_dlcs": []}), encoding="utf-8"
    )
    db.upsert_game(GameInfo(app_id=STELLARIS, name="Stellaris", folder_name="Stellaris"))
    db.update_game_deploy_config(
        STELLARIS,
        name="Stellaris",
        install_path=str(tmp_path / "StellarisInstall"),
        mod_path=str(user_dir),
        workshop_path=str(workshop),
    )
    library = tmp_path / "mod"
    folder, pk, _frozen = _create(
        db,
        library=library,
        game="Stellaris",
        folder="Star",
        workshop_id="88011",
        app_id=STELLARIS,
        title="Star",
    )
    ws = workshop / "88011"
    ws.mkdir()
    (ws / "descriptor.mod").write_text('name="Star"\nremote_file_id="88011"\n', encoding="utf-8")
    _make_miss(folder, pk)
    cap = deployment_capability(pk, db=db)
    assert cap.source_kind == SOURCE_WORKSHOP
    assert cap.source_available is True
    assert cap.allowed is True
    proj = presence_projection(pk, db=db)
    assert proj.open_directory is False
    assert proj.edit_metadata is True
    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out.get("success") is True, out
    assert not folder.exists()


def test_action_gating_workshop_missing_blocks_deploy(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    user_dir = tmp_path / "Paradox" / "Stellaris"
    workshop = tmp_path / "workshop" / "content" / str(STELLARIS)
    user_dir.mkdir(parents=True)
    workshop.mkdir(parents=True)
    (user_dir / "dlc_load.json").write_text(
        json.dumps({"enabled_mods": []}), encoding="utf-8"
    )
    db.upsert_game(GameInfo(app_id=STELLARIS, name="Stellaris", folder_name="Stellaris"))
    db.update_game_deploy_config(
        STELLARIS,
        name="Stellaris",
        install_path=str(tmp_path / "StellarisInstall"),
        mod_path=str(user_dir),
        workshop_path=str(workshop),
    )
    library = tmp_path / "mod"
    folder, pk, _frozen = _create(
        db,
        library=library,
        game="Stellaris",
        folder="GoneWS",
        workshop_id="88012",
        app_id=STELLARIS,
        title="GoneWS",
    )
    _make_miss(folder, pk)
    cap = deployment_capability(pk, db=db)
    assert cap.source_kind == SOURCE_WORKSHOP
    assert cap.source_available is False
    assert cap.allowed is False
    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out.get("success") is False


def test_wh3_miss_workshop_available_allows_deploy(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    data = tmp_path / "WH3Data"
    workshop = tmp_path / "workshop" / "content" / str(WH3)
    data.mkdir()
    workshop.mkdir(parents=True)
    db.upsert_game(
        GameInfo(app_id=WH3, name="Total War: WARHAMMER III", folder_name="Warhammer3")
    )
    install = tmp_path / "WH3Install"
    install.mkdir()
    db.update_game_deploy_config(
        WH3,
        name="Total War: WARHAMMER III",
        install_path=str(install),
        mod_path=str(data),
        workshop_path=str(workshop),
    )
    library = tmp_path / "mod"
    folder, pk, _frozen = _create(
        db,
        library=library,
        game="Warhammer3",
        folder="Pack",
        workshop_id="88013",
        app_id=WH3,
        title="Pack",
    )
    ws = workshop / "88013"
    ws.mkdir()
    (ws / "mod.pack").write_bytes(b"PACK")
    _make_miss(folder, pk)
    cap = deployment_capability(pk, db=db)
    assert cap.allowed is True
    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out.get("success") is True, out


def test_recovery_two_evidence_becomes_live(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db,
        library=library,
        game="GameA",
        folder="Back",
        workshop_id="88020",
        app_id=4242,
        title="LiveAgain",
    )
    _make_miss(folder, pk)
    folder.mkdir(parents=True)
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title="LiveAgain",
        external_id="88020",
        workspace_id="88020",
        app_id=4242,
        game_name="GameA",
        extra={"updated_at": "2020-01-01T00:00:00+00:00"},
    )
    before = db.get_mod_backup_row(pk)
    result = attempt_recovery(pk, db=db)
    assert result.success is True
    assert result.internal_id == frozen
    assert entity_state(pk, db=db) == ENTITY_LIVE
    after = db.get_mod_backup_row(pk)
    assert str(after.get("internal_id") or "") == frozen
    assert str(before.get("internal_id") or "") == frozen
    assert db.get_mod(pk) is not None
    with db._lock:
        n = db._conn.execute("SELECT COUNT(*) AS n FROM mods").fetchone()["n"]
    assert int(n) == 1


def test_recovery_internal_id_only_not_enough(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db,
        library=library,
        game="GameA",
        folder="Weak",
        workshop_id="88021",
        app_id=4242,
        title="Weak",
    )
    _make_miss(folder, pk)
    folder.mkdir(parents=True)
    info = folder / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        json.dumps({"internal_id": frozen, "title": "Weak"}, ensure_ascii=False),
        encoding="utf-8",
    )
    result = attempt_recovery(pk, db=db)
    assert result.success is False
    assert entity_state(pk, db=db) == ENTITY_MISS
    assert str(db.get_mod_backup_row(pk).get("internal_id") or "") == frozen


def test_recovery_workspace_mismatch_remains_miss(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db,
        library=library,
        game="GameA",
        folder="Clash",
        workshop_id="88022",
        app_id=4242,
        title="Clash",
    )
    _make_miss(folder, pk)
    folder.mkdir(parents=True)
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title="OtherMod",
        external_id="99999",
        workspace_id="99999",
        app_id=4242,
        game_name="GameA",
    )
    result = attempt_recovery(pk, db=db)
    assert result.success is False
    assert result.reason == "workspace_mismatch"
    assert entity_state(pk, db=db) == ENTITY_MISS
    bak = json.loads((backup_root(pk) / "metadata.json").read_text(encoding="utf-8"))
    assert bak.get("workspace_id") != "99999" or bak.get("internal_id") == frozen
    assert str(db.get_mod_backup_row(pk).get("internal_id") or "") == frozen


def test_latest_information_wins_miss_edit_over_stale_info(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db,
        library=library,
        game="GameA",
        folder="Merge",
        workshop_id="88023",
        app_id=4242,
        title="Stale",
    )
    stale = (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    _make_miss(folder, pk)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "MissEdit",
            "custom_description": "from-miss",
            "user_notes": "",
            "favorite": False,
        },
    )
    persist_entity_metadata_to_backup(pk, db=db)
    folder.mkdir(parents=True)
    info = folder / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(stale, encoding="utf-8")
    result = attempt_recovery(pk, db=db)
    assert result.success is True
    info_after = read_info_metadata_dict(folder) or {}
    assert info_after.get("display_name") == "MissEdit" or info_after.get("title")
    display = db.get_mod_display_info(pk)
    assert display is not None
    assert "MissEdit" in (display.user_display_name or display.display_name)
    bak = json.loads((backup_root(pk) / "metadata.json").read_text(encoding="utf-8"))
    assert bak.get("display_name") == "MissEdit"
    assert str(db.get_mod_backup_row(pk).get("internal_id") or "") == frozen
    assert entity_state(pk, db=db) == ENTITY_LIVE


def test_latest_wins_current_entity_newer_than_backup(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, frozen = _create(
        db,
        library=library,
        game="GameA",
        folder="NewerDB",
        workshop_id="88024",
        app_id=4242,
        title="Seed",
    )
    _make_miss(folder, pk)
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "EntityNewest",
            "custom_description": "db-wins",
            "user_notes": "",
            "favorite": False,
        },
    )
    folder.mkdir(parents=True)
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title="Seed",
        external_id="88024",
        workspace_id="88024",
        app_id=4242,
        extra={"updated_at": "2019-01-01T00:00:00+00:00"},
    )
    result = attempt_recovery(pk, db=db)
    assert result.success is True
    display = db.get_mod_display_info(pk)
    assert "EntityNewest" in (display.user_display_name or display.display_name)
    bak = json.loads((backup_root(pk) / "metadata.json").read_text(encoding="utf-8"))
    assert bak.get("display_name") == "EntityNewest"


def test_reconcile_miss_does_not_create_folder(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk, _frozen = _create(
        db,
        library=library,
        game="GameA",
        folder="NoMkdir",
        workshop_id="88025",
        app_id=4242,
        title="NoMkdir",
    )
    _make_miss(folder, pk)
    reconcile_folder_presence(library)
    assert not folder.exists()
    assert not (folder / INFO_DIR_NAME).exists()
    assert entity_state(pk, db=db) == ENTITY_MISS


def test_relocation_strings_gone_from_source() -> None:
    root = Path(__file__).resolve().parents[1]
    blob = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (
            root / "ui" / "mod_detail_panel.py",
            root / "ui" / "library_view.py",
            root / "ui" / "mod_card.py",
        )
    )
    for needle in (
        "重新定位目录",
        "Relocate Mod",
        "Choose Mod Directory",
        "Rebind Missing Mod",
        "relocate_mod_folder",
    ):
        assert needle not in blob
    assert not (root / "services" / "mod_relocate.py").is_file()
