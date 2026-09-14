"""Deploy dependencies before the primary Mod."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import RELATIONSHIP_DEPENDENCY, DatabaseManager
from services.deploy import ModDeployer
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    write_info_sidecar,
)

APP = 100


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "dep_order.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed_mod(
    db: DatabaseManager,
    library: Path,
    *,
    mid: str,
    title: str,
    workspace_id: str | None = None,
) -> tuple[Path, str]:
    """Create Entity + proven folder. Returns (folder, mods.mod_id PK)."""
    folder = library / "Game" / title
    folder.mkdir(parents=True)
    (folder / "payload.txt").write_text(title, encoding="utf-8")
    created = create_steam_test_mod(db, external_id=mid, title=title, app_id=APP)
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "")
    wid = workspace_id or str(created.workspace_id or mid)
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title=title,
        external_id=mid,
        workspace_id=wid,
        app_id=APP,
        game_name="Game",
    )
    bind_managed_path(db, pk, folder, title=title, game_name="Game")
    return folder, pk


def test_deploy_runs_dependency_before_main(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    install = tmp_path / "game_mods"
    install.mkdir()
    db.update_game_deploy_config(
        APP, name="Game", mod_path=str(install), deploy_type="folder_copy"
    )
    _folder_dep, pk_dep = _seed_mod(
        db, library, mid="2001", title="DepMod", workspace_id="ws-dep"
    )
    _folder_main, pk_main = _seed_mod(
        db, library, mid="2002", title="MainMod", workspace_id="ws-main"
    )

    db.add_mod_relationship(pk_main, pk_dep, RELATIONSHIP_DEPENDENCY)

    order: list[str] = []
    real = ModDeployer._deploy_with_context

    def _track(self, *, mid, log_prefix, ctx, early, relationship_warnings, **_kwargs):
        order.append(str(mid))
        return real(
            self,
            mid=mid,
            log_prefix=log_prefix,
            ctx=ctx,
            early=early,
            relationship_warnings=relationship_warnings,
            **_kwargs,
        )

    monkeypatch.setattr(ModDeployer, "_deploy_with_context", _track)

    result = ModDeployer(library_root=library, db=db).deploy_mod(pk_main)
    assert result["success"] is True, result
    assert order == [pk_dep, pk_main]
    assert (install / "DepMod" / "payload.txt").is_file()
    assert (install / "MainMod" / "payload.txt").is_file()
    _ = _folder_dep, _folder_main
