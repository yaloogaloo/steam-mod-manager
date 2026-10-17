"""Deploy Closure Gate — naming, identity, overlay, cheap I/O.

These tests lock the default FolderCopy wrapper rule:

* ASCII basename → unchanged
* basename contains Han → ``mod_{workspace_id}``
* parent-path Han is ignored
* CustomPath keeps the user absolute directory
* SQLite ``mod_pk`` is never a deploy folder name
"""

from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.backup_manager import BackupManager
from services.conflict import ConflictDetector
from services.deploy import ModDeployer, prepare_deploy_content
from services.deploy_rules.generic import (
    FolderCopyStrategy,
    contains_han_characters,
    deploy_folder_name,
    deploy_wrapper_folder,
)
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    save_manifest,
)
from services.file_ops import INFO_DIR_NAME
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

DD_APP = 262060


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "closure_gate.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _configure_dd(db: DatabaseManager, mod_path: Path) -> Path:
    mod_path.mkdir(parents=True, exist_ok=True)
    db.update_game_deploy_config(
        DD_APP,
        name="Darkest Dungeon",
        install_path=str(mod_path.parent / "DDInstall"),
        mod_path=str(mod_path),
        deploy_type="folder_copy",
    )
    return mod_path


def _seed(
    db: DatabaseManager,
    library: Path,
    *,
    folder: str,
    workspace_id: str,
    parent: str = "Darkest Dungeon",
    filename: str = "project.xml",
) -> tuple[Path, str, str]:
    source = library / parent / folder
    source.mkdir(parents=True)
    (source / INFO_DIR_NAME).mkdir()
    (source / filename).write_text("<mod/>", encoding="utf-8")
    created = create_steam_test_mod(
        db,
        external_id=workspace_id,
        title=folder,
        app_id=DD_APP,
        game_name="Darkest Dungeon",
    )
    pk = prove_managed_folder(
        db,
        source,
        handle=created.mod_id,
        title=folder,
        app_id=DD_APP,
        game_name="Darkest Dungeon",
    )
    return source, str(pk), str(created.internal_id)


def test_naming_helper_ascii_and_han_basename_only() -> None:
    assert contains_han_characters("The Abigail Williams Class") is False
    assert contains_han_characters("死而复生 Resurrection event") is True
    assert contains_han_characters("测试 Mod Test") is True
    parent_and_ascii = str(Path("暗黑地牢") / "The Abigail Williams Class")
    assert contains_han_characters(parent_and_ascii) is False
    assert (
        deploy_wrapper_folder("The Abigail Williams Class", "3308841144")
        == "The Abigail Williams Class"
    )
    assert (
        deploy_wrapper_folder("死而复生 Resurrection event", "2511735990")
        == "mod_2511735990"
    )
    assert deploy_wrapper_folder("测试 Mod Test", "99") == "mod_99"
    assert deploy_folder_name("2511735990") == "mod_2511735990"
    assert "296" not in deploy_folder_name("3308841144")


def test_gate_ascii_basename_unchanged(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    mods = _configure_dd(db, tmp_path / "DDModsA")
    source, pk, frozen = _seed(
        db,
        library,
        folder="The Abigail Williams Class",
        workspace_id="3308841144",
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    target = Path(result["target"]).resolve()
    assert target.name == "The Abigail Williams Class"
    assert target == (mods / "The Abigail Williams Class").resolve()
    assert not (mods / "mod_3308841144").exists()
    assert not (mods / f"mod_{pk}").exists()
    assert source.name == "The Abigail Williams Class"


def test_gate_han_basename_uses_workspace_id(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_dd(db, tmp_path / "DDModsB")
    source, pk, frozen = _seed(
        db,
        library,
        folder="死而复生 Resurrection event",
        workspace_id="2511735990",
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    target = Path(result["target"]).resolve()
    assert target.name == "mod_2511735990"
    assert target == (mods / "mod_2511735990").resolve()
    assert not (mods / source.name).exists()
    assert not (mods / f"mod_{pk}").exists()
    assert "-" not in target.name


def test_gate_chinese_parent_ascii_basename_unchanged(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_dd(db, tmp_path / "DDModsC")
    source, _pk, frozen = _seed(
        db,
        library,
        folder="The Abigail Williams Class",
        workspace_id="3308841144",
        parent="暗黑地牢",
    )
    assert "暗黑地牢" in str(source)
    assert source.name == "The Abigail Williams Class"
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    assert Path(result["target"]).name == "The Abigail Williams Class"
    assert (mods / "The Abigail Williams Class" / "project.xml").is_file()
    assert not (mods / "mod_3308841144").exists()


def test_gate_mixed_han_basename_renames(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    mods = _configure_dd(db, tmp_path / "DDModsD")
    _source, _pk, frozen = _seed(
        db, library, folder="测试 Mod Test", workspace_id="424242"
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    assert Path(result["target"]).name == "mod_424242"
    assert not (mods / "测试 Mod Test").exists()


def test_gate_custom_path_not_forced_to_mod_workspace(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_dd(db, tmp_path / "DDModsCustom")
    custom = tmp_path / "UserChosen" / "AbsoluteDest"
    custom.mkdir(parents=True)
    source, pk, frozen = _seed(
        db,
        library,
        folder="The Abigail Williams Class",
        workspace_id="3308841144",
    )
    db.update_mod_user_metadata(pk, {"custom_deploy_path": str(custom)})
    result = ModDeployer(library_root=library, db=db).deploy_mod(frozen)
    assert result["success"] is True, result
    assert Path(result["target"]).resolve() == custom.resolve()
    assert (custom / "project.xml").is_file()
    assert not (mods / "mod_3308841144").exists()
    assert Path(result["target"]).name != "mod_3308841144"


def test_gate_identity_tokens_are_not_folder_names() -> None:
    src = inspect.getsource(FolderCopyStrategy.plan)
    assert "_deploy_folder_name(" in src
    helper = inspect.getsource(deploy_wrapper_folder)
    assert "workspace_id" in helper
    assert "mod_pk" not in helper
    prep = inspect.getsource(prepare_deploy_content)
    assert "SQLite" in prep or "mod_pk" in prep
    assert "must not be used as a deploy folder name" in prep


def test_gate_backup_missing_tree_does_not_stat_each_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    dest = tmp_path / "game" / "The Abigail Williams Class"
    targets = [dest / "f" / f"{i}.dat" for i in range(400)]
    calls = {"is_file": 0}
    real_is_file = Path.is_file

    def _count_is_file(self: Path) -> bool:
        calls["is_file"] += 1
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", _count_is_file)
    t0 = time.perf_counter()
    prep = BackupManager(managed).prepare_overwrite(targets)
    elapsed = time.perf_counter() - t0
    assert prep.backup_for(targets[0]) is None
    assert calls["is_file"] < 20
    assert elapsed < 1.0


def test_gate_conflict_preview_skips_self_manifest(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    _configure_dd(db, tmp_path / "DDModsConflict")
    source, pk, _frozen = _seed(
        db,
        library,
        folder="The Abigail Williams Class",
        workspace_id="3308841144",
    )
    huge = [
        ManifestFileEntry(source=f"s{i}", target=str(tmp_path / "dest" / f"{i}.dat"))
        for i in range(800)
    ]
    save_manifest(
        source,
        DeployManifest(
            mod_id=pk,
            deploy_time="t",
            deploy_type="folder_copy",
            files=huge,
        ),
    )
    other, pk_b, _fb = _seed(
        db,
        library,
        folder="OtherMod",
        workspace_id="1",
        filename="other.xml",
    )
    shared = str((tmp_path / "dest" / "shared.dat").resolve())
    save_manifest(
        other,
        DeployManifest(
            mod_id=pk_b,
            deploy_time="t",
            deploy_type="folder_copy",
            files=[ManifestFileEntry(source="s", target=shared)],
        ),
    )
    preview = ConflictDetector(library, db=db).preview_targets(
        pk, [shared], managed=source
    )
    assert any(c.file == shared or shared in c.file for c in preview.conflicts)
    assert all(pk not in (c.mods or []) or pk_b in c.mods for c in preview.conflicts)


def test_gate_no_outer_tree_materialize_helper() -> None:
    from services import deploy as deploy_mod

    src = inspect.getsource(deploy_mod._build_extracted_deploy_content)
    assert "materialized_outer_bytes=0" in src
    assert "Never copy the outer managed tree" in src
    assert "_has_plain_managed_files" in inspect.getsource(deploy_mod)
