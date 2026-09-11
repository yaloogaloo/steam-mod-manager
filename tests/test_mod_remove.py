"""Safe ModRemover: undeploy → library folder → DB."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.mod_remove import ModRemover
from tests.helpers.identity import bind_managed_path, create_steam_test_mod, write_info_sidecar


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "remove.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_remove_mod_deletes_library_and_db(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    folder = library / "Game" / "9901"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (folder / "file.txt").write_text("x", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        '{"published_file_id":"9901","title":"R","app_id":1,"game_name":"Game"}',
        encoding="utf-8",
    )
    other = library / "Game" / "9902"
    other.mkdir(parents=True)
    (other / "keep.txt").write_text("keep", encoding="utf-8")

    db.update_game_deploy_config(
        1, name="Game", install_path=str(tmp_path / "g"), mod_path=str(tmp_path / "g")
    )
    created = create_steam_test_mod(db, external_id="9901", title="R", app_id=1)
    write_info_sidecar(
        folder,
        internal_id=str(created.mod_id),
        title="R",
        external_id="9901",
        workspace_id=str(created.workspace_id or "9901"),
        app_id=1,
        game_name="Game",
    )
    bind_managed_path(db, created.mod_id, folder, title="R", game_name="Game")

    db.add_category_tag(9901, "Fix")

    out = ModRemover(library, db=db).remove_mod(9901)
    assert out["success"] is True
    assert not folder.exists()
    assert other.exists()
    assert db.get_mod(9901) is None
    assert db.get_mod_tags(9901) == []
