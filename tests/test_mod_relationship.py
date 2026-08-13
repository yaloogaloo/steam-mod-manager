"""Mod relationships (dependency / conflict / addon / patch)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import (
    RELATIONSHIP_ADDON,
    RELATIONSHIP_CONFLICT,
    RELATIONSHIP_DEPENDENCY,
    RELATIONSHIP_PATCH,
    DatabaseManager,
)
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "rel.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_create_dependency(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="1", title="Child"))
    db.upsert_mod(ModMetadata(published_file_id="2", title="UE4SS"))
    rel = db.add_mod_relationship(1, 2, RELATIONSHIP_DEPENDENCY)
    assert rel.relationship_type == RELATIONSHIP_DEPENDENCY
    assert rel.target_mod_id == "2"
    grouped = db.get_mod_relationships(1)
    assert len(grouped["dependencies"]) == 1
    assert grouped["dependencies"][0]["title"] == "UE4SS"
    assert grouped["conflicts"] == []
    assert grouped["addons"] == []
    assert grouped["patches"] == []


def test_duplicate_relationship_not_duplicated(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="10", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="11", title="B"))
    a = db.add_mod_relationship(10, 11, RELATIONSHIP_CONFLICT)
    b = db.add_mod_relationship(10, 11, RELATIONSHIP_CONFLICT)
    assert a.id == b.id
    assert len(db.get_mod_relationships(10)["conflicts"]) == 1


def test_remove_relationship(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="20", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="21", title="B"))
    rel = db.add_mod_relationship(20, 21, RELATIONSHIP_ADDON)
    assert db.remove_mod_relationship(rel.id) is True
    assert db.get_mod_relationships(20)["addons"] == []
    assert db.remove_mod_relationship(rel.id) is False


def test_all_relationship_types(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="30", title="Base"))
    for i, title in enumerate(("Dep", "Conf", "Add", "Pat"), start=31):
        db.upsert_mod(ModMetadata(published_file_id=str(i), title=title))
    db.add_mod_relationship(30, 31, RELATIONSHIP_DEPENDENCY)
    db.add_mod_relationship(30, 32, RELATIONSHIP_CONFLICT)
    db.add_mod_relationship(30, 33, RELATIONSHIP_ADDON)
    db.add_mod_relationship(30, 34, RELATIONSHIP_PATCH)
    g = db.get_mod_relationships(30)
    assert [x["mod_id"] for x in g["dependencies"]] == ["31"]
    assert [x["mod_id"] for x in g["conflicts"]] == ["32"]
    assert [x["mod_id"] for x in g["addons"]] == ["33"]
    assert [x["mod_id"] for x in g["patches"]] == ["34"]


def test_counts_for_card_badge(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="40", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="41", title="B"))
    db.upsert_mod(ModMetadata(published_file_id="42", title="C"))
    db.add_mod_relationship(40, 41, RELATIONSHIP_DEPENDENCY)
    db.add_mod_relationship(40, 42, RELATIONSHIP_CONFLICT)
    assert db.get_relationship_counts([40])["40"] == (1, 1)


def test_deploy_dependency_disabled_warning(
    tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    library = tmp_path / "mod"
    folder = library / "G" / "50"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (folder / "a.txt").write_text("x", encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        '{"published_file_id":"50","title":"Child","app_id":1,"game_name":"G"}',
        encoding="utf-8",
    )
    db.update_game_deploy_config(
        1, name="G", install_path=str(tmp_path / "g"), mod_path=str(tmp_path / "g")
    )
    db.upsert_mod(ModMetadata(published_file_id="50", title="Child", app_id=1))
    db.upsert_mod(ModMetadata(published_file_id="51", title="UE4SS", app_id=1))
    db.add_mod_relationship(50, 51, RELATIONSHIP_DEPENDENCY)
    db.disable_mod(51)

    # Avoid real deploy — only exercise warning attachment via early path after
    # relationship check but before strategy by mocking resolve + strategy.
    from services.deploy_rules.base import StrategyResult
    from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry

    man = DeployManifest(
        mod_id="50",
        deploy_time="t",
        deploy_type="folder_copy",
        files=[ManifestFileEntry(source="a.txt", target=str(tmp_path / "g" / "a.txt"))],
    )
    result = StrategyResult(
        success=True,
        target=str(tmp_path / "g"),
        copied_files=1,
        deploy_type="folder_copy",
        deploy_time="t",
        files=list(man.files),
        manifest=man,
    )

    class FakeStrategy:
        def plan(self, ctx):
            return StrategyResult(success=True, files=list(man.files))

        def deploy(self, ctx):
            return result

        def undeploy(self, ctx, manifest):
            return result

    monkeypatch.setattr(
        "services.deploy.get_strategy", lambda *a, **k: FakeStrategy()
    )
    cfg = db.get_game_deploy_config(1)

    class Ctx:
        mod_id = "50"
        app_id = 1
        source = folder
        managed_path = folder
        deploy_type = "folder_copy"
        config = cfg
        allowed_rel_paths = None
        custom_deploy_path = ""
        workspace_id = "50"

        def content_root(self):
            return folder

        def library_folder(self):
            return folder

    monkeypatch.setattr(
        ModDeployer,
        "_resolve_context",
        lambda self, mid, require_target_exists=False, prepare_archives=True: (
            Ctx(),
            None,
            None,
        ),
    )

    out = ModDeployer(library_root=library, db=db).deploy_mod(50)
    assert out.get("success") is True
    warns = out.get("relationship_warnings") or []
    assert any(w.get("type") == "dependency_disabled" for w in warns)
    assert "UE4SS" in (warns[0].get("message") or "")


def test_known_conflict_warning(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="60", title="A"))
    db.upsert_mod(ModMetadata(published_file_id="61", title="Old Character Mod"))
    db.add_mod_relationship(60, 61, RELATIONSHIP_CONFLICT)
    warns = db.check_relationship_deploy_warnings(60)
    assert len(warns) == 1
    assert warns[0]["type"] == "known_conflict"
    assert "Old Character Mod" in warns[0]["message"]


def test_detail_panel_shows_relationships(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    from ui.mod_detail_panel import ModDetailPanel

    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    library = tmp_path / "mod"
    folder = library / "G" / "70"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        '{"published_file_id":"70","title":"Main","app_id":1,"game_name":"G"}',
        encoding="utf-8",
    )
    db.upsert_mod(ModMetadata(published_file_id="70", title="Main"))
    db.upsert_mod(ModMetadata(published_file_id="71", title="UE4SS"))
    db.upsert_mod(ModMetadata(published_file_id="72", title="Old Character Mod"))
    db.upsert_mod(ModMetadata(published_file_id="73", title="Costume Pack"))
    db.upsert_mod(ModMetadata(published_file_id="74", title="Performance Fix"))
    db.add_mod_relationship(70, 71, RELATIONSHIP_DEPENDENCY)
    db.add_mod_relationship(70, 72, RELATIONSHIP_CONFLICT)
    db.add_mod_relationship(70, 73, RELATIONSHIP_ADDON)
    db.add_mod_relationship(70, 74, RELATIONSHIP_PATCH)

    panel = ModDetailPanel()
    panel.show_mod(folder)
    assert panel._rel_lists["dependencies"].count() == 1
    assert "UE4SS" in panel._rel_lists["dependencies"].item(0).text()
    assert "Old Character Mod" in panel._rel_lists["conflicts"].item(0).text()
    assert "Costume Pack" in panel._rel_lists["addons"].item(0).text()
    assert "Performance Fix" in panel._rel_lists["patches"].item(0).text()


def test_migration_creates_relationships_table(tmp_path: Path) -> None:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "mig.db")
    rows = manager._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='mod_relationships'"
    ).fetchall()
    assert rows
    # unique index / constraint present
    idx = manager._conn.execute(
        "PRAGMA index_list(mod_relationships)"
    ).fetchall()
    assert idx
    manager.close()
    DatabaseManager.reset_instance()
