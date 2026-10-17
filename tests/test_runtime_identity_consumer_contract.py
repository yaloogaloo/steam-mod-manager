"""Runtime consumer identity contract — Internal ID is the only entity path."""

from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.deploy_paths import resolve_deploy_managed_path
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, ModFileManager
from services.identity_service import create_mod_identity
from services.importers.materialize import materialize_imported_mod
from services.info_sidecar import ensure_registration_info_proof, read_info_metadata_dict
from services.mod_identity import read_internal_id
from services.path_lifecycle import resolve_managed_folder

ROOT = Path(__file__).resolve().parents[1]
STARDEW = 413150
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    from services.managed_path_cache import invalidate_managed_path_cache

    invalidate_managed_path_cache()
    manager = DatabaseManager.instance(tmp_path / "runtime_consumer.db")
    manager.upsert_game(GameInfo(app_id=STARDEW, name="Stardew", folder_name="Stardew"))
    manager.upsert_game(GameInfo(app_id=BG3, name="BG3", folder_name="BG3"))
    yield manager
    invalidate_managed_path_cache()
    DatabaseManager.reset_instance()


def _write_info(folder: Path, payload: dict) -> Path:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "content.bin").write_bytes(b"x")
    return folder


def _call_attrs(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def test_1_deploy_disallows_workspace_lookup() -> None:
    src = (ROOT / "services" / "deploy_paths.py").read_text(encoding="utf-8")
    assert "find_mod_by_workspace_id" not in src
    assert "find_by_published_id" not in src
    body = src.split("def resolve_deploy_managed_path", 1)[1].split("def iter_typed", 1)[0]
    assert "resolve_managed_folder" in body
    assert "workspace_id" not in body or "Never scans by workspace_id" in src


def test_2_same_workspace_different_app_id_no_cross_bind(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    a = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/baldursgate3/mods/1333",
        title="BG3 Lib",
        app_id=BG3,
        game_name="BG3",
        operation="import",
    )
    b = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
        title="Carry",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    assert a.mod_id != b.mod_id
    db.update_mod_identity_fields(str(a.mod_id), internal_id=str(a.mod_id))
    db.update_mod_identity_fields(str(b.mod_id), internal_id=str(b.mod_id))
    folder_a = _write_info(
        library / "BG3" / "Lib",
        {
            "internal_id": str(a.mod_id),
            "workspace_id": "1333",
            "platform": PLATFORM_NEXUS,
            "app_id": BG3,
            "title": "BG3 Lib",
        },
    )
    folder_b = _write_info(
        library / "Stardew" / "Carry",
        {
            "internal_id": str(b.mod_id),
            "workspace_id": "1333",
            "platform": PLATFORM_NEXUS,
            "app_id": STARDEW,
            "title": "Carry",
        },
    )
    db.update_mod_identity_fields(
        str(a.mod_id), last_known_path=str(folder_a.resolve()), folder_present=True
    )
    db.update_mod_identity_fields(
        str(b.mod_id), last_known_path=str(folder_b.resolve()), folder_present=True
    )
    resolved_a = resolve_managed_folder(str(a.mod_id), library_root=library, db=db)
    resolved_b = resolve_managed_folder(str(b.mod_id), library_root=library, db=db)
    assert resolved_a.path is not None and resolved_b.path is not None
    assert resolved_a.path.resolve() == folder_a.resolve()
    assert resolved_b.path.resolve() == folder_b.resolve()
    assert resolved_a.path.resolve() != resolved_b.path.resolve()


def test_3_deleted_dir_rebounds_via_new_info_internal_id(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="4242",
        source_url="https://www.nexusmods.com/stardewvalley/mods/4242",
        title="Rebind",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    mid = str(created.mod_id)
    db.update_mod_identity_fields(mid, internal_id=mid)
    old = _write_info(
        library / "Stardew" / "Old",
        {"internal_id": mid, "workspace_id": "4242", "platform": PLATFORM_NEXUS, "app_id": STARDEW},
    )
    db.update_mod_identity_fields(mid, last_known_path=str(old.resolve()), folder_present=True)
    shutil.rmtree(old)
    new = _write_info(
        library / "Stardew" / "New",
        {"internal_id": mid, "workspace_id": "4242", "platform": PLATFORM_NEXUS, "app_id": STARDEW},
    )
    resolved = resolve_managed_folder(mid, library_root=library, db=db)
    assert resolved.path is not None
    assert resolved.path.resolve() == new.resolve()
    deploy_path = resolve_deploy_managed_path(mid, db=db, library_root=library)
    assert deploy_path is not None
    assert deploy_path.resolve() == new.resolve()


def test_4_numeric_folder_name_cannot_locate_mod(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "Stardew" / "1333"
    folder.mkdir(parents=True)
    (folder / "x.bin").write_bytes(b"x")
    # No .info — must not resolve
    assert resolve_managed_folder("1333", library_root=library, db=db).path is None
    assert ModFileManager(library).find_by_internal_id("1333") is None
    assert resolve_deploy_managed_path("1333", db=db, library_root=library) is None


def test_5_published_file_id_cannot_locate_entity(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = _write_info(
        library / "Stardew" / "PubOnly",
        {
            "published_file_id": "88888",
            "workspace_id": "88888",
            "title": "PubOnly",
            "platform": PLATFORM_NEXUS,
            "app_id": STARDEW,
        },
    )
    mgr = ModFileManager(library)
    assert mgr.find_by_published_id("88888") is None
    assert resolve_managed_folder("88888", library_root=library, db=db).path is None
    assert folder.is_dir()


def test_6_import_create_writes_info_with_internal_id(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="5555",
        source_url="https://www.nexusmods.com/stardewvalley/mods/5555",
        title="ImportProof",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    mid = str(created.mod_id)
    dest = materialize_imported_mod(
        library_root=library,
        internal_id=mid,
        title="ImportProof",
        game_name="Stardew",
        context={"app_id": STARDEW, "game_name": "Stardew", "platform": PLATFORM_NEXUS},
    )
    data = read_info_metadata_dict(dest) or {}
    assert read_internal_id(data)
    assert str(data.get("workspace_id") or "") == "5555"
    ensure_registration_info_proof(dest, mid, db=db)


def test_deploy_source_forbids_legacy_lookups() -> None:
    deploy = (ROOT / "services" / "deploy.py").read_text(encoding="utf-8")
    # recover must not use folder.name as identity
    recover = deploy.split("def recover_stale_deploy_transactions", 1)[1].split(
        "def deploy_mod", 1
    )[0]
    assert "folder.name" not in recover
    paths = (ROOT / "services" / "deploy_paths.py").read_text(encoding="utf-8")
    assert "resolve_managed_folder" in _call_attrs(paths) or "resolve_managed_folder" in paths
