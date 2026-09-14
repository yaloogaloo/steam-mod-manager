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
from services.deploy import ModDeployer
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


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
    from core.game_info import GameInfo

    manager.upsert_game(GameInfo(app_id=1, name="G", folder_name="G"))
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _steam(db: DatabaseManager, external_id: str, title: str, *, app_id: int = 0) -> str:
    created = create_steam_test_mod(
        db, external_id=external_id, title=title, app_id=app_id
    )
    return str(created.mod_id)


def test_create_dependency(db: DatabaseManager) -> None:
    pk_child = _steam(db, "1", "Child")
    pk_ue = _steam(db, "2", "UE4SS")
    rel = db.add_mod_relationship(pk_child, pk_ue, RELATIONSHIP_DEPENDENCY)
    assert rel.relationship_type == RELATIONSHIP_DEPENDENCY
    assert rel.target_mod_id == pk_ue
    grouped = db.get_mod_relationships(pk_child)
    assert len(grouped["dependencies"]) == 1
    assert grouped["dependencies"][0]["title"] == "UE4SS"
    assert grouped["conflicts"] == []
    assert grouped["addons"] == []
    assert grouped["patches"] == []


def test_duplicate_relationship_not_duplicated(db: DatabaseManager) -> None:
    pk_a = _steam(db, "10", "A")
    pk_b = _steam(db, "11", "B")
    a = db.add_mod_relationship(pk_a, pk_b, RELATIONSHIP_CONFLICT)
    b = db.add_mod_relationship(pk_a, pk_b, RELATIONSHIP_CONFLICT)
    assert a.id == b.id
    assert len(db.get_mod_relationships(pk_a)["conflicts"]) == 1


def test_remove_relationship(db: DatabaseManager) -> None:
    pk_a = _steam(db, "20", "A")
    pk_b = _steam(db, "21", "B")
    rel = db.add_mod_relationship(pk_a, pk_b, RELATIONSHIP_ADDON)
    assert db.remove_mod_relationship(rel.id) is True
    assert db.get_mod_relationships(pk_a)["addons"] == []
    assert db.remove_mod_relationship(rel.id) is False


def test_all_relationship_types(db: DatabaseManager) -> None:
    pk_base = _steam(db, "30", "Base")
    pks = {}
    for i, title in enumerate(("Dep", "Conf", "Add", "Pat"), start=31):
        pks[i] = _steam(db, str(i), title)
    db.add_mod_relationship(pk_base, pks[31], RELATIONSHIP_DEPENDENCY)
    db.add_mod_relationship(pk_base, pks[32], RELATIONSHIP_CONFLICT)
    db.add_mod_relationship(pk_base, pks[33], RELATIONSHIP_ADDON)
    db.add_mod_relationship(pk_base, pks[34], RELATIONSHIP_PATCH)
    g = db.get_mod_relationships(pk_base)
    assert [x["mod_id"] for x in g["dependencies"]] == [pks[31]]
    assert [x["mod_id"] for x in g["conflicts"]] == [pks[32]]
    assert [x["mod_id"] for x in g["addons"]] == [pks[33]]
    assert [x["mod_id"] for x in g["patches"]] == [pks[34]]


def test_counts_for_card_badge(db: DatabaseManager) -> None:
    pk_a = _steam(db, "40", "A")
    pk_b = _steam(db, "41", "B")
    pk_c = _steam(db, "42", "C")
    db.add_mod_relationship(pk_a, pk_b, RELATIONSHIP_DEPENDENCY)
    db.add_mod_relationship(pk_a, pk_c, RELATIONSHIP_CONFLICT)
    assert db.get_relationship_counts([pk_a])[pk_a] == (1, 1)


def test_deploy_dependency_disabled_warning(
    tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    library = tmp_path / "mod"
    folder = library / "G" / "50"
    folder.mkdir(parents=True)
    (folder / "a.txt").write_text("x", encoding="utf-8")
    db.update_game_deploy_config(
        1, name="G", install_path=str(tmp_path / "g"), mod_path=str(tmp_path / "g")
    )
    pk_child = _steam(db, "50", "Child", app_id=1)
    prove_managed_folder(db, folder, handle=pk_child, title="Child", app_id=1, game_name="G")
    pk_ue = _steam(db, "51", "UE4SS", app_id=1)
    db.add_mod_relationship(pk_child, pk_ue, RELATIONSHIP_DEPENDENCY)
    db.disable_mod(pk_ue)

    from services.deploy_rules.base import StrategyResult
    from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry

    man = DeployManifest(
        mod_id=pk_child,
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
    ws = str(db.get_mod_display_info(pk_child).workspace_id or "50")

    class Ctx:
        mod_id = pk_child
        internal_id = pk_child
        app_id = 1
        source = folder
        managed_path = folder
        deploy_type = "folder_copy"
        config = cfg
        allowed_rel_paths = None
        custom_deploy_path = ""
        workspace_id = ws

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

    out = ModDeployer(library_root=library, db=db).deploy_mod(pk_child)
    assert out.get("success") is True
    warns = out.get("relationship_warnings") or []
    assert any(w.get("type") == "dependency_disabled" for w in warns)
    assert "UE4SS" in (warns[0].get("message") or "")


def test_known_conflict_warning(db: DatabaseManager) -> None:
    pk_a = _steam(db, "60", "A")
    pk_b = _steam(db, "61", "Old Character Mod")
    db.add_mod_relationship(pk_a, pk_b, RELATIONSHIP_CONFLICT)
    warns = db.check_relationship_deploy_warnings(pk_a)
    assert len(warns) == 1
    assert warns[0]["type"] == "known_conflict"
    assert "Old Character Mod" in warns[0]["message"]


def test_detail_panel_shows_relationships(
    qapp: QApplication, tmp_path: Path, db: DatabaseManager, monkeypatch
) -> None:
    from ui.mod_detail_panel import ModDetailPanel

    monkeypatch.setattr("ui.mod_detail_panel.get_db", lambda: db)
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    library = tmp_path / "mod"
    folder = library / "G" / "70"
    folder.mkdir(parents=True)
    pk_main = _steam(db, "70", "Main", app_id=1)
    prove_managed_folder(db, folder, handle=pk_main, title="Main", app_id=1, game_name="G")
    pk_ue = _steam(db, "71", "UE4SS", app_id=1)
    pk_old = _steam(db, "72", "Old Character Mod", app_id=1)
    pk_costume = _steam(db, "73", "Costume Pack", app_id=1)
    pk_perf = _steam(db, "74", "Performance Fix", app_id=1)
    db.add_mod_relationship(pk_main, pk_ue, RELATIONSHIP_DEPENDENCY)
    db.add_mod_relationship(pk_main, pk_old, RELATIONSHIP_CONFLICT)
    db.add_mod_relationship(pk_main, pk_costume, RELATIONSHIP_ADDON)
    db.add_mod_relationship(pk_main, pk_perf, RELATIONSHIP_PATCH)

    panel = ModDetailPanel()
    panel.show_mod(folder, mod_id=pk_main)
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
    idx = manager._conn.execute(
        "PRAGMA index_list(mod_relationships)"
    ).fetchall()
    assert idx
    manager.close()
    DatabaseManager.reset_instance()
