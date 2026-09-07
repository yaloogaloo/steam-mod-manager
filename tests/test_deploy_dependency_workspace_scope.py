"""Dependency metadata must resolve workspace_id via (platform, app_id), not PK."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity

BG3 = 1086940
STARDEW = 413150


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "dep_workspace_scope.db")
    manager.upsert_game(GameInfo(app_id=STARDEW, name="Stardew", folder_name="Stardew"))
    manager.upsert_game(GameInfo(app_id=BG3, name="BG3", folder_name="BG3"))
    yield manager
    DatabaseManager.reset_instance()


def _prove(
    db: DatabaseManager,
    folder: Path,
    *,
    pk: str,
    workspace_id: str,
    app_id: int,
    title: str,
    platform: str = PLATFORM_NEXUS,
    dependencies: list[str] | None = None,
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "payload.txt").write_text(title, encoding="utf-8")
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "internal_id": str(pk),
        "workspace_id": str(workspace_id),
        "title": title,
        "app_id": app_id,
        "platform": platform,
        "game_name": "BG3" if app_id == BG3 else "Stardew",
    }
    if dependencies is not None:
        payload["dependencies"] = list(dependencies)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    db.update_mod_identity_fields(
        pk,
        internal_id=str(pk),
        last_known_path=str(folder),
        folder_present=True,
    )


def test_case1_metadata_workspace_resolves_to_same_game_pk_not_cross_game(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """BG3 workspace_id=899 → BG3 PK; must not hit Stardew PK=899."""
    library = tmp_path / "library"
    bg3_dep = library / "BG3" / "AppearanceEdit"
    bg3_main = library / "BG3" / "AppearanceEditCHS"
    sd_collide = library / "Stardew" / "Fishing"

    bg3_dep_ent = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="899",
        source_url="https://www.nexusmods.com/baldursgate3/mods/899",
        title="Appearance Edit Enhanced",
        app_id=BG3,
        game_name="BG3",
        operation="import",
    )
    bg3_main_ent = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="6190",
        source_url="https://www.nexusmods.com/baldursgate3/mods/6190",
        title="Appearance Edit Enhanced - Chinese Translation",
        app_id=BG3,
        game_name="BG3",
        operation="import",
    )
    sd_ent = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="3623",
        source_url="https://www.nexusmods.com/stardewvalley/mods/3623",
        title="Fishing Made Easy",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )

    bg3_dep_pk = str(bg3_dep_ent.mod_id)
    bg3_main_pk = str(bg3_main_ent.mod_id)
    sd_pk = str(sd_ent.mod_id)
    assert str(bg3_dep_ent.workspace_id) == "899"
    assert str(bg3_main_ent.workspace_id) == "6190"

    _prove(
        db,
        bg3_dep,
        pk=bg3_dep_pk,
        workspace_id="899",
        app_id=BG3,
        title="Appearance Edit Enhanced",
    )
    _prove(
        db,
        bg3_main,
        pk=bg3_main_pk,
        workspace_id="6190",
        app_id=BG3,
        title="CHS",
        dependencies=["899"],
    )
    _prove(
        db,
        sd_collide,
        pk=sd_pk,
        workspace_id="3623",
        app_id=STARDEW,
        title="Fishing",
    )

    # Force a Stardew row at PK 899 when free — reproduces production collision.
    if sd_pk != "899" and db.get_mod("899") is None:
        with db._lock:
            db._conn.execute(
                """
                INSERT INTO mods (
                    mod_id, app_id, title, platform, workspace_id,
                    external_id, internal_id, folder_present,
                    last_known_path, updated_at
                )
                VALUES (
                    899, ?, 'Stardew Collide', ?, '3623-x', '3623-x',
                    'collide-899', 1, ?, datetime('now')
                )
                """,
                (STARDEW, PLATFORM_NEXUS, str(sd_collide)),
            )
            db._conn.commit()
        collide = db.get_mod("899")
        assert collide is not None
        assert int(collide.app_id) == STARDEW

    deps = ModDeployer(library_root=library, db=db)._dependency_mod_ids_for_deploy(
        bg3_main_pk
    )
    assert deps == [bg3_dep_pk]
    if db.get_mod("899") is not None and int(db.get_mod("899").app_id) == STARDEW:
        assert "899" not in deps


def test_case2_same_workspace_different_app_id_isolated(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    bg3_folder = library / "BG3" / "Lib"
    sd_folder = library / "Stardew" / "Lib"
    main_folder = library / "BG3" / "Main"

    bg3 = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/baldursgate3/mods/1333",
        title="BG3 Lib",
        app_id=BG3,
        game_name="BG3",
        operation="import",
    )
    sd = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
        title="Stardew Lib",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    main = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="9001",
        source_url="https://www.nexusmods.com/baldursgate3/mods/9001",
        title="BG3 Main",
        app_id=BG3,
        game_name="BG3",
        operation="import",
    )

    _prove(
        db, bg3_folder, pk=str(bg3.mod_id), workspace_id="1333", app_id=BG3, title="BG3 Lib"
    )
    _prove(
        db, sd_folder, pk=str(sd.mod_id), workspace_id="1333", app_id=STARDEW, title="SD Lib"
    )
    _prove(
        db,
        main_folder,
        pk=str(main.mod_id),
        workspace_id="9001",
        app_id=BG3,
        title="BG3 Main",
        dependencies=["1333"],
    )

    deps = ModDeployer(library_root=library, db=db)._dependency_mod_ids_for_deploy(
        str(main.mod_id)
    )
    assert deps == [str(bg3.mod_id)]
    assert str(sd.mod_id) not in deps


def test_case3_resolved_dependencies_are_pks_only(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    dep_folder = library / "BG3" / "Dep"
    main_folder = library / "BG3" / "Main"

    dep = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="42",
        source_url="https://www.nexusmods.com/baldursgate3/mods/42",
        title="Dep",
        app_id=BG3,
        game_name="BG3",
        operation="import",
    )
    main = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="99",
        source_url="https://www.nexusmods.com/baldursgate3/mods/99",
        title="Main",
        app_id=BG3,
        game_name="BG3",
        operation="import",
    )
    _prove(db, dep_folder, pk=str(dep.mod_id), workspace_id="42", app_id=BG3, title="Dep")
    _prove(
        db,
        main_folder,
        pk=str(main.mod_id),
        workspace_id="99",
        app_id=BG3,
        title="Main",
        dependencies=["42"],
    )

    deps = ModDeployer(library_root=library, db=db)._dependency_mod_ids_for_deploy(
        str(main.mod_id)
    )
    assert deps == [str(dep.mod_id)]
    assert all(x.isdigit() for x in deps)
    hit = db.get_mod(deps[0])
    assert hit is not None
    assert int(hit.app_id) == BG3
    # Nested deploy uses PK only — workspace token must not remain.
    if str(dep.mod_id) != "42":
        assert "42" not in deps


def test_scoped_workspace_api_isolates_games(db: DatabaseManager) -> None:
    a = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="777",
        source_url="https://www.nexusmods.com/baldursgate3/mods/777",
        title="A",
        app_id=BG3,
        game_name="BG3",
        operation="import",
    )
    b = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="777",
        source_url="https://www.nexusmods.com/stardewvalley/mods/777",
        title="B",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    assert (
        db.resolve_mod_id_by_scoped_workspace(
            platform=PLATFORM_NEXUS, app_id=BG3, workspace_id="777"
        )
        == str(a.mod_id)
    )
    assert (
        db.resolve_mod_id_by_scoped_workspace(
            platform=PLATFORM_NEXUS, app_id=STARDEW, workspace_id="777"
        )
        == str(b.mod_id)
    )
    assert (
        db.resolve_mod_id_by_scoped_workspace(
            platform=PLATFORM_NEXUS, app_id=BG3, workspace_id="999999"
        )
        is None
    )
