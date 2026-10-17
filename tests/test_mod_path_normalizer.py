"""Shared Mod folder-name normalizer (ASCII deploy names, uniqueness)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.deploy import ModDeployer
from services.deploy_rules.base import DeployContext
from services.deploy_rules.game_capabilities import supports_game_capability
from services.deploy_rules.generic import FolderCopyStrategy, deploy_folder_name
from services.file_ops import INFO_DIR_NAME
from services.mod_path_normalizer import (
    contains_chinese,
    ensure_unique_mod_folder_name,
    normalize_mod_folder_name,
)
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

DD_APP = 262060
assert DD_APP == 262060


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "mod_path_normalizer.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _block_pypinyin(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import sys

    real_import = builtins.__import__

    def blocked(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if name == "pypinyin" or str(name).startswith("pypinyin."):
            raise ImportError("No module named 'pypinyin'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked)
    for key in list(sys.modules):
        if key == "pypinyin" or key.startswith("pypinyin."):
            monkeypatch.delitem(sys.modules, key, raising=False)
    import services.mod_path_normalizer as mod

    monkeypatch.setattr(mod, "_PYPINYIN_MISSING_LOGGED", False)


def test_english_name_unchanged() -> None:
    assert normalize_mod_folder_name("BetterMod") == "BetterMod"
    assert normalize_mod_folder_name("Better_Swords") == "Better_Swords"


def test_pypinyin_present_keeps_pinyin() -> None:
    pytest.importorskip("pypinyin")
    assert normalize_mod_folder_name("暗黑地牢增强") == "anheidilaozengqiang"


def test_pypinyin_missing_still_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_pypinyin(monkeypatch)
    from services.mod_path_normalizer import normalize_mod_folder_name as normalize

    result = normalize("暗黑地牢增强")
    assert result
    assert not contains_chinese(result)
    assert result.isascii()
    assert "暗黑地牢增强" not in result


def test_chinese_name_is_non_chinese_and_stable() -> None:
    original = "暗黑地牢增强"
    first = normalize_mod_folder_name(original)
    second = normalize_mod_folder_name(original)
    assert first == "anheidilaozengqiang"
    assert first == second
    assert not contains_chinese(first)
    assert first.isascii()


def test_mixed_name_is_stable_and_legal() -> None:
    original = "Dark暗黑MOD"
    first = normalize_mod_folder_name(original)
    second = normalize_mod_folder_name(original)
    assert first == "Dark_anhei_MOD"
    assert first == second
    assert not contains_chinese(first)
    assert first.isascii()
    assert "/" not in first
    assert "\\" not in first


def test_duplicate_normalized_names_do_not_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "mods"
    target.mkdir()
    a = normalize_mod_folder_name("测试MOD")
    b = normalize_mod_folder_name("测试_MOD")
    first = ensure_unique_mod_folder_name(a, target)
    (target / first).mkdir()
    second = ensure_unique_mod_folder_name(b, target)
    assert first != second
    assert not (target / second).exists()
    (target / second).mkdir()
    assert (target / first).is_dir()
    assert (target / second).is_dir()
    third = ensure_unique_mod_folder_name(a, target)
    assert third not in {first, second}


def test_ensure_unique_suffix_sequence() -> None:
    occupied = ["test_mod", Path("other") / "test_mod_1"]
    assert ensure_unique_mod_folder_name("test_mod", occupied) == "test_mod_2"
    assert ensure_unique_mod_folder_name("free_mod", occupied) == "free_mod"


def test_single_chinese_folder_implementation() -> None:
    hits = []
    for path in Path("services").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "contains_chinese":
                hits.append(path.as_posix())
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (
                        isinstance(item, ast.FunctionDef)
                        and item.name == "contains_chinese"
                    ):
                        hits.append(path.as_posix())
    assert hits == ["services/mod_path_normalizer.py"]


def _function_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            names.extend(
                item.name
                for item in node.body
                if isinstance(item, ast.FunctionDef)
            )
    return names


def test_no_darkest_dungeon_specific_rename_function() -> None:
    generic_path = Path("services/deploy_rules/generic.py")
    names = _function_names(generic_path) + _function_names(
        Path("services/mod_path_normalizer.py")
    )
    lowered = [name.lower() for name in names]
    assert not any("darkest" in name for name in lowered)
    assert not any("rename_chinese" in name for name in lowered)
    generic_src = generic_path.read_text(encoding="utf-8")
    assert 'if game == "Darkest Dungeon"' not in generic_src
    assert "ASCII_DEPLOY_FOLDER_APP_IDS" not in generic_src
    assert "DARKEST_DUNGEON_APP_ID" not in generic_src
    assert "262060" not in generic_src
    assert "289070" not in generic_src
    assert "is_civilization_vi_game" not in generic_src
    from services.deploy_rules.generic import _deploy_folder_name

    deploy_src = inspect.getsource(_deploy_folder_name)
    assert "deploy_wrapper_folder" in deploy_src
    assert "workspace_id" in deploy_src
    assert "normalize_mod_folder_name" not in deploy_src
    assert "_deploy_folder_name(" in inspect.getsource(FolderCopyStrategy.plan)
    assert deploy_folder_name("2511735990") == "mod_2511735990"


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


def test_darkest_dungeon_deploy_uses_workspace_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_dd(db, tmp_path / "DDMods")
    folder = "暗黑地牢增强"
    workspace_id = "262060001"
    expected = deploy_folder_name(workspace_id)

    source = library / "Darkest Dungeon" / folder
    source.mkdir(parents=True)
    (source / INFO_DIR_NAME).mkdir()
    (source / "project.xml").write_text("<mod/>", encoding="utf-8")
    created = create_steam_test_mod(
        db,
        external_id="262060001",
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

    result = ModDeployer(library_root=library, db=db).deploy_mod(
        created.internal_id
    )
    assert result["success"] is True, result
    target = Path(result["target"]).resolve()
    assert target == (mods / expected).resolve()
    assert not contains_chinese(target.name)
    assert (target / "project.xml").is_file()
    assert source.name == folder
    assert (source / "project.xml").is_file()
    assert not (mods / folder).exists()

    cfg = db.get_game_deploy_config(DD_APP)
    assert cfg is not None
    planned = FolderCopyStrategy().plan(
        DeployContext(
            internal_id=pk,
            workspace_id=str(created.workspace_id or ""),
            app_id=DD_APP,
            source=source,
            deploy_type="folder_copy",
            config=cfg,
            managed_path=source,
        )
    )
    assert planned.success is True
    assert Path(planned.target).name == expected


def test_darkest_dungeon_english_folder_keeps_name(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = _configure_dd(db, tmp_path / "DDModsEN")
    folder = "BetterMod"
    source = library / "Darkest Dungeon" / folder
    source.mkdir(parents=True)
    (source / INFO_DIR_NAME).mkdir()
    (source / "project.xml").write_text("<mod/>", encoding="utf-8")
    created = create_steam_test_mod(
        db,
        external_id="262060002",
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
    result = ModDeployer(library_root=library, db=db).deploy_mod(
        created.internal_id
    )
    assert result["success"] is True, result
    expected = folder
    assert Path(result["target"]).resolve() == (mods / expected).resolve()
    assert Path(result["target"]).name == "BetterMod"
    assert source.name == folder


def test_chinese_mod_deploys_without_pypinyin(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block_pypinyin(monkeypatch)
    folder = "暗黑地牢增强"
    expected = deploy_folder_name("262060003")
    assert expected == "mod_262060003"

    library = tmp_path / "library"
    mods = _configure_dd(db, tmp_path / "DDModsNoPinyin")
    source = library / "Darkest Dungeon" / folder
    source.mkdir(parents=True)
    (source / INFO_DIR_NAME).mkdir()
    (source / "project.xml").write_text("<mod/>", encoding="utf-8")
    created = create_steam_test_mod(
        db,
        external_id="262060003",
        title=folder,
        app_id=DD_APP,
        game_name="Darkest Dungeon",
    )
    prove_managed_folder(
        db,
        source,
        handle=created.mod_id,
        title=folder,
        app_id=DD_APP,
        game_name="Darkest Dungeon",
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(
        created.internal_id
    )
    assert result["success"] is True, result
    target = Path(result["target"]).resolve()
    assert target == (mods / expected).resolve()
    assert not contains_chinese(target.name)
    assert target.name != folder
    assert (target / "project.xml").is_file()
    assert not (mods / folder).exists()


def test_folder_copy_uses_workspace_id_for_resurrection_event(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Lock 2511735990 to mod_2511735990 — never pinyin / uXXXX / Chinese."""
    library = tmp_path / "library"
    mods = _configure_dd(db, tmp_path / "DDModsResurrection")
    folder = "死而复生 Resurrection event"
    expected = deploy_folder_name("2511735990")
    assert expected == "mod_2511735990"
    assert not contains_chinese(expected)
    assert expected != folder

    source = library / "Darkest Dungeon" / folder
    source.mkdir(parents=True)
    (source / INFO_DIR_NAME).mkdir()
    (source / "project.xml").write_text("<mod/>", encoding="utf-8")
    (source / "campaign").mkdir()
    (source / "campaign" / "event.json").write_text("{}", encoding="utf-8")
    created = create_steam_test_mod(
        db,
        external_id="2511735990",
        title=folder,
        app_id=DD_APP,
        game_name="Darkest Dungeon",
    )
    prove_managed_folder(
        db,
        source,
        handle=created.mod_id,
        title=folder,
        app_id=DD_APP,
        game_name="Darkest Dungeon",
    )
    deployer = ModDeployer(library_root=library, db=db)
    result = deployer.deploy_mod(created.internal_id)
    assert result["success"] is True, result

    planned_target = Path(result["target"]).resolve()
    assert planned_target.name == expected
    assert planned_target == (mods / expected).resolve()
    assert not contains_chinese(planned_target.name)
    assert (planned_target / "project.xml").is_file()
    assert (planned_target / "campaign" / "event.json").is_file()
    assert not (mods / folder).exists()
    chinese_dirs = [p.name for p in mods.iterdir() if p.is_dir() and contains_chinese(p.name)]
    assert chinese_dirs == []

    cfg = db.get_game_deploy_config(DD_APP)
    assert cfg is not None
    planned = FolderCopyStrategy().plan(
        DeployContext(
            internal_id=str(created.internal_id),
            workspace_id=str(created.workspace_id or "2511735990"),
            app_id=DD_APP,
            source=source,
            deploy_type="folder_copy",
            config=cfg,
            managed_path=source,
        )
    )
    assert planned.success is True
    assert Path(planned.target).resolve() == planned_target
    assert Path(planned.target).name == expected
