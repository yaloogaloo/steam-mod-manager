"""Identity Closure Gate — Frozen UUID must never enter PK-only DAL APIs."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    PROVIDER_NEXUS_MANUAL_IMPORT,
)
from services.collection import add_mod_to_collection, create_collection, list_collection_member_ids
from services.deploy_file_plan import DeployFilePlan
from services.deploy_identity import is_frozen_internal_uuid
from services.deploy_rules.base import DeployContext
from services.file_ops import INFO_DIR_NAME, ModFileManager
from services.identity_service import allocate_internal_id, resolve_internal_id_from_workspace_id
from services.mod_library_cache import dal_mod_pk
from services.offline.manager import attach_nexus_offline_page
from tests.helpers.identity import (
    bind_managed_path,
    create_test_mod_identity,
    write_info_sidecar,
)

PALWORLD = 1623730
NEXUS_HTML = """<!DOCTYPE html><html><head>
<meta property="og:url" content="https://www.nexusmods.com/palworld/mods/{nid}">
<meta property="og:title" content="{title}">
</head><body><h1>{title}</h1><p>offline body</p></body></html>
"""


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "identity_closure.db")
    manager.upsert_game(GameInfo(app_id=PALWORLD, name="Palworld", folder_name="Palworld"))
    yield manager
    DatabaseManager.reset_instance()


def _seed(
    tmp_path: Path,
    db: DatabaseManager,
    *,
    nexus_id: str,
    title: str,
    platform: str = PLATFORM_NEXUS,
) -> tuple[str, str, Path]:
    created = create_test_mod_identity(
        db,
        platform=platform,
        title=title,
        external_id=nexus_id,
        source_url=f"https://www.nexusmods.com/palworld/mods/{nexus_id}",
        app_id=PALWORLD,
        game_name="Palworld",
    )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "").strip()
    assert is_frozen_internal_uuid(frozen)
    assert frozen != pk
    folder = tmp_path / "library" / "Palworld" / title
    folder.mkdir(parents=True)
    (folder / "mod.pak").write_bytes(b"pak")
    bind_managed_path(db, pk, folder, game_name="Palworld", title=title)
    return pk, frozen, folder


def test_gate1_dal_mod_pk_uuid_to_pk(tmp_path: Path, db: DatabaseManager) -> None:
    pk, frozen, _folder = _seed(tmp_path, db, nexus_id="40101", title="Gate1")
    assert dal_mod_pk(frozen) == pk
    assert dal_mod_pk(pk) == pk
    assert dal_mod_pk("not-an-id") == ""


def test_gate2_pk_only_dal_rejects_uuid(tmp_path: Path, db: DatabaseManager) -> None:
    pk, frozen, _folder = _seed(tmp_path, db, nexus_id="40102", title="Gate2")
    assert db.get_mod(pk) is not None
    assert db.get_mod_display_info(pk) is not None
    with pytest.raises((TypeError, ValueError)):
        db.get_mod(frozen)
    assert db.get_mod_display_info(frozen) is None
    with pytest.raises((TypeError, ValueError)):
        int(frozen)
    with pytest.raises((TypeError, ValueError)):
        db.update_mod_offline_status(frozen, status=OFFLINE_STATUS_ARCHIVED)
    db.update_mod_offline_status(pk, status=OFFLINE_STATUS_ARCHIVED)
    db.update_mod_deploy_status(pk, deploy_status="not_deployed")
    dal_src = (
        Path(__file__).resolve().parents[1] / "core" / "db_manager.py"
    ).read_text(encoding="utf-8")
    deploy_fn = dal_src.split("def update_mod_deploy_status(")[1].split("\n    def ")[0]
    assert "mid = int(mod_id)" in deploy_fn
    get_mod_fn = dal_src.split("def get_mod(")[1].split("\n    def ")[0]
    assert "int(mod_id)" in get_mod_fn


def test_gate3_file_ops_never_calls_get_mod_with_uuid(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen, _folder = _seed(tmp_path, db, nexus_id="40103", title="Gate3Title")
    looked_up: list[str] = []
    original = DatabaseManager.get_mod

    def _spy(self: DatabaseManager, mod_id: int | str) -> object:
        looked_up.append(str(mod_id))
        return original(self, mod_id)

    monkeypatch.setattr(DatabaseManager, "get_mod", _spy)
    meta = ModMetadata(
        published_file_id="",
        internal_id=frozen,
        mod_pk="",
        title="12345",
    )
    ModFileManager(tmp_path / "library").enrich_title_from_db(meta)
    assert frozen not in looked_up
    assert pk in looked_up
    assert meta.title == "Gate3Title"


def test_gate4_nexus_detail_offline_html_uuid(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen, folder = _seed(tmp_path, db, nexus_id="40104", title="Gate4Html")
    html = tmp_path / "40104.html"
    html.write_text(NEXUS_HTML.format(nid="40104", title="Gate4Html"), encoding="utf-8")
    looked_up: list[str] = []
    original = DatabaseManager.get_mod_display_info

    def _spy(self: DatabaseManager, mod_id: int | str) -> object:
        looked_up.append(str(mod_id))
        return original(self, mod_id)

    monkeypatch.setattr(DatabaseManager, "get_mod_display_info", _spy)
    attach = attach_nexus_offline_page(
        frozen,
        html,
        managed_path=folder,
        library_root=tmp_path / "library",
    )
    assert attach.status == OFFLINE_STATUS_ARCHIVED
    assert attach.provider == PROVIDER_NEXUS_MANUAL_IMPORT
    assert str(attach.mod_id) == pk
    assert frozen not in looked_up
    assert pk in looked_up
    assert (folder / INFO_DIR_NAME / "offline" / "index.html").is_file()


def test_gate4_nexus_import_pk_still_works(tmp_path: Path, db: DatabaseManager) -> None:
    pk, frozen, folder = _seed(tmp_path, db, nexus_id="40105", title="Gate4Pk")
    html = tmp_path / "40105.html"
    html.write_text(NEXUS_HTML.format(nid="40105", title="Gate4Pk"), encoding="utf-8")
    attach = attach_nexus_offline_page(
        pk,
        html,
        managed_path=folder,
        library_root=tmp_path / "library",
    )
    assert attach.status == OFFLINE_STATUS_ARCHIVED
    assert str(attach.mod_id) == pk
    assert str((db.get_mod_backup_row(pk) or {}).get("internal_id") or "") == frozen


def test_gate5_steam_sync_uses_pk_when_present(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen, _folder = _seed(
        tmp_path, db, nexus_id="40106", title="Gate5Pk", platform=PLATFORM_STEAM
    )
    looked_up: list[str] = []
    original = DatabaseManager.get_mod

    def _spy(self: DatabaseManager, mod_id: int | str) -> object:
        looked_up.append(str(mod_id))
        return original(self, mod_id)

    monkeypatch.setattr(DatabaseManager, "get_mod", _spy)
    meta = ModMetadata(
        published_file_id="40106",
        internal_id=frozen,
        mod_pk=pk,
        title="",
    )
    ModFileManager(tmp_path / "library").enrich_title_from_db(meta)
    assert looked_up == [pk]
    assert frozen not in looked_up


def test_gate5_steam_sync_uuid_resolves_before_get_mod(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen, _folder = _seed(
        tmp_path, db, nexus_id="40107", title="Gate5Uuid", platform=PLATFORM_STEAM
    )
    looked_up: list[str] = []
    original = DatabaseManager.get_mod

    def _spy(self: DatabaseManager, mod_id: int | str) -> object:
        looked_up.append(str(mod_id))
        return original(self, mod_id)

    monkeypatch.setattr(DatabaseManager, "get_mod", _spy)
    meta = ModMetadata(
        published_file_id="40107",
        internal_id=frozen,
        mod_pk="",
        title="",
    )
    ModFileManager(tmp_path / "library").enrich_title_from_db(meta)
    assert looked_up == [pk]
    assert frozen not in looked_up


def test_gate5_numeric_folder_does_not_guess_folder_name_as_pk(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    pk, frozen, _existing = _seed(
        tmp_path, db, nexus_id="40108", title="RealTitle", platform=PLATFORM_STEAM
    )
    library = tmp_path / "library"
    numeric = library / "Palworld" / "40108"
    numeric.mkdir(parents=True)
    (numeric / "mod.pak").write_bytes(b"pak")
    write_info_sidecar(
        numeric,
        internal_id=frozen,
        title="999",
        external_id="40108",
        workspace_id="40108",
        app_id=PALWORLD,
        game_name="Palworld",
        platform=PLATFORM_STEAM,
    )
    bind_managed_path(db, pk, numeric, game_name="Palworld", title="RealTitle")
    looked_up: list[str] = []
    original = DatabaseManager.get_mod

    def _spy(self: DatabaseManager, mod_id: int | str) -> object:
        looked_up.append(str(mod_id))
        return original(self, mod_id)

    monkeypatch.setattr(DatabaseManager, "get_mod", _spy)
    ModFileManager(library).migrate_numeric_mod_folders()
    assert "40108" not in looked_up
    assert frozen not in looked_up
    assert pk in looked_up


def test_gate5_numeric_folder_without_identity_skips_get_mod(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    orphan = library / "Palworld" / "77777"
    orphan.mkdir(parents=True)
    (orphan / "mod.pak").write_bytes(b"pak")
    looked_up: list[str] = []
    original = DatabaseManager.get_mod

    def _spy(self: DatabaseManager, mod_id: int | str) -> object:
        looked_up.append(str(mod_id))
        return original(self, mod_id)

    monkeypatch.setattr(DatabaseManager, "get_mod", _spy)
    ModFileManager(library).migrate_numeric_mod_folders()
    assert "77777" not in looked_up
    assert looked_up == []


def test_gate6_collection_uuid_writes_pk_fk(tmp_path: Path, db: DatabaseManager) -> None:
    pk, frozen, _folder = _seed(tmp_path, db, nexus_id="40109", title="Gate6")
    rec = create_collection(PALWORLD, "Pack", db=db)
    assert add_mod_to_collection(rec.collection_id, frozen, db=db) is True
    members = list_collection_member_ids(rec.collection_id, db=db)
    assert members == [pk]
    row = db._conn.execute(
        "SELECT mod_id FROM collection_mods WHERE collection_id = ?",
        (rec.collection_id,),
    ).fetchone()
    assert str(row["mod_id"]) == pk
    assert str(row["mod_id"]) != frozen


def test_gate6_collection_ui_converts_uuid_before_dal() -> None:
    from ui.library_view import ModLibraryView

    src = inspect.getsource(ModLibraryView._on_set_collections_requested)
    assert "_dal_mod_pk" in src
    assert "membership_check_states" in src
    assert "apply_collection_memberships" in src


def test_gate7_deploy_context_and_file_plan_split_identity() -> None:
    from core.db_manager import GameDeployConfig

    frozen = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    cfg = GameDeployConfig(app_id=PALWORLD, install_path="C:/game", mod_path="mods")
    ctx = DeployContext(
        internal_id=frozen,
        source=Path("."),
        app_id=PALWORLD,
        config=cfg,
        deploy_type="copy",
        mod_pk=42,
    )
    assert ctx.internal_id == frozen
    assert is_frozen_internal_uuid(ctx.internal_id)
    assert ctx.mod_pk == 42
    plan = DeployFilePlan(internal_id=frozen, mod_pk=42)
    assert plan.internal_id == frozen
    assert is_frozen_internal_uuid(plan.internal_id)
    assert plan.mod_pk == 42


def test_gate7_deploy_resolve_writes_uuid_and_pk() -> None:
    from services.deploy import ModDeployer

    src = inspect.getsource(ModDeployer._resolve_context)
    assert "internal_id=frozen" in src
    assert "mod_pk=int(pk)" in src


def test_backup_write_root_is_frozen_uuid(tmp_path: Path, db: DatabaseManager) -> None:
    from services.backup_identity import is_frozen_backup_uuid, write_backup_root_for

    pk, frozen, _folder = _seed(tmp_path, db, nexus_id="40110", title="GateBackup")
    root = write_backup_root_for(pk, db=db)
    assert is_frozen_backup_uuid(root.name)
    assert root.name == frozen
    assert root.name != pk


def test_resolvers_keep_published_contract(tmp_path: Path, db: DatabaseManager) -> None:
    pk, frozen, _folder = _seed(tmp_path, db, nexus_id="40111", title="GateResolvers")
    minted = allocate_internal_id(db)
    assert is_frozen_internal_uuid(minted)
    assert minted != pk
    resolved = resolve_internal_id_from_workspace_id(
        "40111", platform=PLATFORM_NEXUS, app_id=PALWORLD, db=db
    )
    assert resolved == frozen
    assert is_frozen_internal_uuid(resolved)
    assert resolved != pk
