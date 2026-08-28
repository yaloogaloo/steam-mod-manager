"""Deploy dependencies before the primary Mod."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import RELATIONSHIP_DEPENDENCY, DatabaseManager
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME

APP = 100


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "dep_order.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _seed_mod(
    library: Path,
    *,
    mid: str,
    title: str,
    workspace_id: str | None = None,
) -> Path:
    folder = library / "Game" / title
    folder.mkdir(parents=True)
    (folder / "payload.txt").write_text(title, encoding="utf-8")
    info = folder / INFO_DIR_NAME
    info.mkdir()
    wid = workspace_id or mid
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "workspace_id": "{wid}",\n'
        f'  "title": "{title}",\n'
        f'  "app_id": {APP}\n'
        "}\n",
        encoding="utf-8",
    )
    return folder


def test_deploy_runs_dependency_before_main(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    install = tmp_path / "game_mods"
    install.mkdir()
    _seed_mod(library, mid="2001", title="DepMod", workspace_id="ws-dep")
    _seed_mod(library, mid="2002", title="MainMod", workspace_id="ws-main")

    db.update_game_deploy_config(
        APP, name="Game", mod_path=str(install), deploy_type="folder_copy"
    )
    db.upsert_mod(ModMetadata(published_file_id="2001", title="DepMod", app_id=APP))
    db.upsert_mod(ModMetadata(published_file_id="2002", title="MainMod", app_id=APP))
    db.add_mod_relationship("2002", "2001", RELATIONSHIP_DEPENDENCY)

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

    result = ModDeployer(library_root=library, db=db).deploy_mod("2002")
    assert result["success"] is True, result
    assert order == ["2001", "2002"]
    assert (install / "DepMod" / "payload.txt").is_file()
    assert (install / "MainMod" / "payload.txt").is_file()
