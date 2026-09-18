"""Workspace ID is the user input for adding Mod dependencies.

Internal ID is never a user-facing lookup token. Relationships persist PK.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.identity_service import (
    create_mod_identity,
    resolve_internal_id_from_workspace_id,
)
from services.mod_relationships import add_dependency_by_workspace_id

BG3 = 1086940
STARDEW = 413150


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "dep_ws_bind.db")
    manager.upsert_game(GameInfo(app_id=BG3, name="BG3", folder_name="BG3"))
    manager.upsert_game(
        GameInfo(app_id=STARDEW, name="Stardew", folder_name="Stardew")
    )
    yield manager
    DatabaseManager.reset_instance()


def _nexus(
    db: DatabaseManager,
    *,
    external_id: str,
    app_id: int,
    title: str,
) -> tuple[str, str]:
    if app_id == BG3:
        game = "BG3"
        slug = "baldursgate3"
    else:
        game = "Stardew"
        slug = "stardewvalley"
    ent = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=external_id,
        source_url=f"https://www.nexusmods.com/{slug}/mods/{external_id}",
        title=title,
        app_id=app_id,
        game_name=game,
        operation="import",
    )
    pk = str(ent.mod_id)
    ws = str(ent.workspace_id)
    assert ws == external_id
    assert pk != ws
    return pk, ws


def test_ui_prompt_is_workspace_id_not_internal_id() -> None:
    from ui.mod_detail_panel import ModDetailPanel

    src = inspect.getsource(ModDetailPanel._on_add_dependency_pill)
    assert "Workspace ID" in src
    assert "Internal ID" not in src
    assert "get_mod(" not in src
    assert "add_dependency_by_workspace_id" in src


def test_resolver_does_not_treat_internal_id_as_user_input(db: DatabaseManager) -> None:
    src = inspect.getsource(resolve_internal_id_from_workspace_id)
    assert "get_mod(" not in src
    assert "resolve_mod_id_by_scoped_workspace" in src

    owner_pk, owner_ws = _nexus(
        db, external_id="6190", app_id=BG3, title="Owner"
    )
    dep_pk, dep_ws = _nexus(
        db, external_id="899", app_id=BG3, title="Dep"
    )
    row = db.get_mod_backup_row(owner_pk) or {}
    plat = str(row.get("platform") or PLATFORM_NEXUS)
    aid = int(row.get("app_id") or BG3)
    dep_uuid = str((db.get_mod_backup_row(dep_pk) or {}).get("internal_id") or "")

    assert (
        resolve_internal_id_from_workspace_id(
            dep_ws, platform=plat, app_id=aid, db=db
        )
        == dep_uuid
    )
    # Internal ID is not a Workspace ID token — must not bind.
    assert (
        resolve_internal_id_from_workspace_id(
            dep_pk, platform=plat, app_id=aid, db=db
        )
        == ""
    )
    assert owner_ws != dep_pk


def test_workspace_id_input_binds_dependency_as_internal_id(
    db: DatabaseManager,
) -> None:
    owner_pk, _owner_ws = _nexus(
        db, external_id="6190", app_id=BG3, title="Translation"
    )
    dep_pk, dep_ws = _nexus(
        db, external_id="899", app_id=BG3, title="Appearance Edit"
    )

    bound = add_dependency_by_workspace_id(owner_pk, dep_ws, db=db)
    assert bound == dep_pk
    grouped = db.get_mod_relationships(owner_pk)
    deps = grouped.get("dependencies") or []
    assert [str(item.get("mod_id") or "") for item in deps] == [dep_pk]
    assert [str(item.get("target_mod_id") or "") for item in deps] == [dep_pk]


def test_internal_id_token_does_not_bind_dependency(db: DatabaseManager) -> None:
    owner_pk, _owner_ws = _nexus(
        db, external_id="6190", app_id=BG3, title="Translation"
    )
    dep_pk, _dep_ws = _nexus(
        db, external_id="899", app_id=BG3, title="Appearance Edit"
    )
    with pytest.raises(LookupError, match="Workspace ID"):
        add_dependency_by_workspace_id(owner_pk, dep_pk, db=db)
    assert db.get_mod_relationships(owner_pk)["dependencies"] == []


def test_same_workspace_id_other_game_does_not_bind(db: DatabaseManager) -> None:
    owner_pk, _ = _nexus(db, external_id="6190", app_id=BG3, title="BG3 Owner")
    _sd_pk, sd_ws = _nexus(
        db, external_id="899", app_id=STARDEW, title="Stardew Collision"
    )
    with pytest.raises(LookupError, match="Workspace ID"):
        add_dependency_by_workspace_id(owner_pk, sd_ws, db=db)
    assert db.get_mod_relationships(owner_pk)["dependencies"] == []
