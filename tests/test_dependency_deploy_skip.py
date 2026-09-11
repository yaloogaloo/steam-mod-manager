"""Skip auto-deploy of dependencies that are already deployed."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_TYPE_FOLDER_COPY,
    RELATIONSHIP_DEPENDENCY,
    DatabaseManager,
)
from core.mod_platform import PLATFORM_NEXUS
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity, identity_create_scope

APP = 100


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "dep_skip.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed_mod(
    library: Path,
    db: DatabaseManager,
    *,
    external_id: str,
    title: str,
) -> str:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_NEXUS,
            external_id=str(external_id),
            source_url=f"https://www.nexusmods.com/stardewvalley/mods/{external_id}",
            title=title,
            app_id=APP,
            game_name="Game",
            operation="import",
        )
    pk = str(created.mod_id)
    folder = library / "Game" / title
    folder.mkdir(parents=True)
    (folder / "payload.txt").write_text(title, encoding="utf-8")
    info = folder / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "internal_id": "{pk}",\n'
        f'  "workspace_id": "{external_id}",\n'
        f'  "published_file_id": "{external_id}",\n'
        f'  "title": "{title}",\n'
        f'  "app_id": {APP},\n'
        f'  "platform": "nexus"\n'
        "}\n",
        encoding="utf-8",
    )
    db.update_mod_identity_fields(
        pk,
        last_known_path=str(folder),
        folder_present=True,
    )
    return pk


def _track_deploy_order(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []
    real = ModDeployer._deploy_with_context

    def _track(self, *, mid, log_prefix, ctx, early, relationship_warnings, **kwargs):
        order.append(str(mid))
        return real(
            self,
            mid=mid,
            log_prefix=log_prefix,
            ctx=ctx,
            early=early,
            relationship_warnings=relationship_warnings,
            **kwargs,
        )

    monkeypatch.setattr(ModDeployer, "_deploy_with_context", _track)
    return order


def test_undeployed_dependency_deploys_before_main(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    install = tmp_path / "game_mods"
    install.mkdir()
    dep_pk = _seed_mod(library, db, external_id="21001", title="DepMod")
    main_pk = _seed_mod(library, db, external_id="21002", title="MainMod")
    db.update_game_deploy_config(
        APP, name="Game", mod_path=str(install), deploy_type=DEPLOY_TYPE_FOLDER_COPY
    )
    db.add_mod_relationship(main_pk, dep_pk, RELATIONSHIP_DEPENDENCY)

    order = _track_deploy_order(monkeypatch)
    result = ModDeployer(library_root=library, db=db).deploy_mod(main_pk)
    assert result["success"] is True, result
    assert order == [dep_pk, main_pk]


def test_already_deployed_dependency_is_skipped(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    install = tmp_path / "game_mods"
    install.mkdir()
    dep_pk = _seed_mod(library, db, external_id="21011", title="DepMod")
    main_pk = _seed_mod(library, db, external_id="21012", title="MainMod")
    db.update_game_deploy_config(
        APP, name="Game", mod_path=str(install), deploy_type=DEPLOY_TYPE_FOLDER_COPY
    )
    db.add_mod_relationship(main_pk, dep_pk, RELATIONSHIP_DEPENDENCY)
    db.update_mod_deploy_status(
        dep_pk,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path=str(install / "DepMod"),
    )

    order = _track_deploy_order(monkeypatch)
    result = ModDeployer(library_root=library, db=db).deploy_mod(main_pk)
    assert result["success"] is True, result
    assert order == [main_pk]


def test_user_deploy_still_runs_when_already_deployed(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    install = tmp_path / "game_mods"
    install.mkdir()
    dep_pk = _seed_mod(library, db, external_id="21021", title="DepMod")
    db.update_game_deploy_config(
        APP, name="Game", mod_path=str(install), deploy_type=DEPLOY_TYPE_FOLDER_COPY
    )

    deployer = ModDeployer(library_root=library, db=db)
    first = deployer.deploy_mod(dep_pk)
    assert first["success"] is True, first
    assert db.get_mod_deploy_info(dep_pk).deploy_status == DEPLOY_STATUS_DEPLOYED

    order = _track_deploy_order(monkeypatch)
    second = deployer.deploy_mod(dep_pk)
    assert second["success"] is True, second
    assert order == [dep_pk]
