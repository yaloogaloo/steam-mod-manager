"""JSON game-capability config drives deploy folder-name rules."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.deploy import ModDeployer
from services.deploy_rules.game_capabilities import (
    CAPABILITY_NORMALIZE_MOD_FOLDER_NAME,
    reset_game_capabilities_cache,
    set_game_capabilities_config_path,
    supports_game_capability,
)
from services.file_ops import INFO_DIR_NAME
from services.deploy_rules.generic import deploy_folder_name
from services.mod_path_normalizer import contains_chinese
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

DD_APP = 262060
CIV6 = 289070
CAP = CAPABILITY_NORMALIZE_MOD_FOLDER_NAME


@pytest.fixture(autouse=True)
def _reset_capability_cache() -> None:
    set_game_capabilities_config_path(None)
    reset_game_capabilities_cache()
    yield
    set_game_capabilities_config_path(None)
    reset_game_capabilities_cache()


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "game_capabilities.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _write_caps(tmp_path: Path, games: dict) -> Path:
    path = tmp_path / "game_capabilities.json"
    path.write_text(
        json.dumps({"games": games}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    set_game_capabilities_config_path(path)
    return path


def _production_capabilities() -> dict:
    path = Path("config/game_capabilities.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    games = payload.get("games")
    assert isinstance(games, dict)
    return games


def test_configured_game_supports_normalize_mod_folder_name() -> None:
    assert supports_game_capability(DD_APP, CAP) is True
    assert supports_game_capability("262060", CAP) is True
    assert supports_game_capability(CIV6, CAP) is True
    assert supports_game_capability("289070", CAP) is True


def test_production_json_lists_darkest_dungeon_and_civ6() -> None:
    games = _production_capabilities()
    assert games["262060"][CAP] is True
    assert games["289070"][CAP] is True


def test_unconfigured_game_returns_false() -> None:
    assert supports_game_capability(123456, CAP) is False
    assert supports_game_capability(DD_APP, "another_feature") is False


def test_capability_false_is_disabled(tmp_path: Path) -> None:
    _write_caps(tmp_path, {str(DD_APP): {CAP: False}})
    assert supports_game_capability(DD_APP, CAP) is False


def test_missing_file_returns_false(tmp_path: Path) -> None:
    set_game_capabilities_config_path(tmp_path / "missing.json")
    assert supports_game_capability(DD_APP, CAP) is False


def test_config_is_cached_across_multiple_app_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write_caps(
        tmp_path,
        {
            str(DD_APP): {CAP: True},
            "111": {CAP: False},
        },
    )
    reads = {"n": 0}
    original = Path.read_text

    def _counted(self: Path, *args: object, **kwargs: object) -> str:
        if self.resolve() == path.resolve():
            reads["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _counted)
    reset_game_capabilities_cache()
    assert supports_game_capability(DD_APP, CAP) is True
    assert supports_game_capability(111, CAP) is False
    assert supports_game_capability(123456, CAP) is False
    assert supports_game_capability(DD_APP, CAP) is True
    assert reads["n"] == 1


def test_in_place_false_reloads_after_cache_reset(tmp_path: Path) -> None:
    """Same JSON file flipped to false must not keep the old True after cache drop."""
    path = _write_caps(tmp_path, {str(DD_APP): {CAP: True}})
    assert supports_game_capability(DD_APP, CAP) is True
    path.write_text(
        json.dumps({"games": {str(DD_APP): {CAP: False}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert supports_game_capability(DD_APP, CAP) is True  # in-process cache
    reset_game_capabilities_cache()
    assert supports_game_capability(DD_APP, CAP) is False


def test_services_have_no_ascii_folder_app_id_whitelist() -> None:
    hits: list[str] = []
    for path in Path("services").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        rel = path.as_posix()
        if "ASCII_DEPLOY_FOLDER_APP_IDS" in text:
            hits.append(f"{rel}: ASCII_DEPLOY_FOLDER_APP_IDS")
        if "== 262060" in text or "app_id == 262060" in text:
            hits.append(f"{rel}: app_id == 262060")
        if "== 289070" in text or "app_id == 289070" in text:
            hits.append(f"{rel}: app_id == 289070")
    assert hits == []


def test_generic_deploy_folder_uses_workspace_id() -> None:
    from services.deploy_rules.generic import _deploy_folder_name, deploy_folder_name

    src = inspect.getsource(_deploy_folder_name)
    assert "deploy_wrapper_folder" in src
    assert "workspace_id" in src
    assert "normalize_mod_folder_name" not in src
    assert deploy_folder_name("2511735990") == "mod_2511735990"


def test_generic_py_has_no_game_whitelist() -> None:
    src = Path("services/deploy_rules/generic.py").read_text(encoding="utf-8")
    assert "ASCII_DEPLOY_FOLDER_APP_IDS" not in src
    assert "DARKEST_DUNGEON_APP_ID" not in src
    assert "262060" not in src
    assert "289070" not in src
    assert "is_civilization_vi_game" not in src
    assert "normalize_mod_folder_name" not in src
    assert "pypinyin" not in src
    assert "json.load" not in src
    assert "game_capabilities.json" not in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            dump = ast.dump(node)
            assert "262060" not in dump
            assert "289070" not in dump


def _configure_folder_copy(
    db: DatabaseManager,
    mod_path: Path,
    *,
    app_id: int,
    name: str,
) -> Path:
    mod_path.mkdir(parents=True, exist_ok=True)
    db.update_game_deploy_config(
        app_id,
        name=name,
        install_path=str(mod_path.parent / f"{name}Install"),
        mod_path=str(mod_path),
        deploy_type="folder_copy",
    )
    return mod_path


def _seed_and_register(
    db: DatabaseManager,
    library: Path,
    *,
    app_id: int,
    game_name: str,
    folder: str,
    external_id: str,
    filename: str = "project.xml",
) -> tuple[Path, str]:
    source = library / game_name / folder
    source.mkdir(parents=True)
    (source / INFO_DIR_NAME).mkdir()
    (source / filename).write_text("<mod/>", encoding="utf-8")
    created = create_steam_test_mod(
        db,
        external_id=external_id,
        title=folder,
        app_id=app_id,
        game_name=game_name,
    )
    pk = prove_managed_folder(
        db,
        source,
        handle=created.mod_id,
        title=folder,
        app_id=app_id,
        game_name=game_name,
    )
    return source, pk


def _frozen(db: DatabaseManager, pk: str) -> str:
    meta = db.get_mod(pk)
    assert meta is not None
    return str(meta.internal_id)


def test_darkest_dungeon_deploy_uses_workspace_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    folder = "暗黑地牢增强"
    workspace_id = "262060101"
    expected = deploy_folder_name(workspace_id)
    mods = _configure_folder_copy(
        db, tmp_path / "DDMods", app_id=DD_APP, name="Darkest Dungeon"
    )
    source, pk = _seed_and_register(
        db,
        tmp_path / "library",
        app_id=DD_APP,
        game_name="Darkest Dungeon",
        folder=folder,
        external_id=workspace_id,
    )
    result = ModDeployer(library_root=tmp_path / "library", db=db).deploy_mod(
        _frozen(db, pk)
    )
    assert result["success"] is True, result
    target = Path(result["target"]).resolve()
    assert target.name == expected
    assert target == (mods / expected).resolve()
    assert not contains_chinese(target.name)
    assert source.name == folder
    assert not (mods / folder).exists()


def test_capability_disabled_still_uses_workspace_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    _write_caps(tmp_path, {str(DD_APP): {CAP: False}})
    folder = "暗黑地牢增强"
    workspace_id = "262060102"
    expected = deploy_folder_name(workspace_id)
    mods = _configure_folder_copy(
        db, tmp_path / "DDOff", app_id=DD_APP, name="Darkest Dungeon"
    )
    source, pk = _seed_and_register(
        db,
        tmp_path / "library",
        app_id=DD_APP,
        game_name="Darkest Dungeon",
        folder=folder,
        external_id=workspace_id,
    )
    result = ModDeployer(library_root=tmp_path / "library", db=db).deploy_mod(
        _frozen(db, pk)
    )
    assert result["success"] is True, result
    target = Path(result["target"]).resolve()
    assert target.name == expected
    assert target == (mods / expected).resolve()
    assert source.name == folder
    assert not contains_chinese(target.name)


def test_unconfigured_game_uses_workspace_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    app_id = 424242
    folder = "中文目录"
    workspace_id = "555666777"
    expected = deploy_folder_name(workspace_id)
    mods = _configure_folder_copy(
        db, tmp_path / "OtherMods", app_id=app_id, name="SomeGame"
    )
    source, pk = _seed_and_register(
        db,
        tmp_path / "library",
        app_id=app_id,
        game_name="SomeGame",
        folder=folder,
        external_id=workspace_id,
        filename="a.txt",
    )
    result = ModDeployer(library_root=tmp_path / "library", db=db).deploy_mod(
        _frozen(db, pk)
    )
    assert result["success"] is True, result
    target = Path(result["target"]).resolve()
    assert target == (mods / expected).resolve()
    assert source.name == folder
    assert not contains_chinese(target.name)


def test_disable_after_cache_reset_still_uses_workspace_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    path = _write_caps(tmp_path, {str(DD_APP): {CAP: True}})
    assert supports_game_capability(DD_APP, CAP) is True
    path.write_text(
        json.dumps(
            {"games": {str(DD_APP): {CAP: False}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reset_game_capabilities_cache()
    assert supports_game_capability(DD_APP, CAP) is False

    folder = "暗黑地牢增强"
    workspace_id = "262060103"
    expected = deploy_folder_name(workspace_id)
    mods = _configure_folder_copy(
        db, tmp_path / "DDRestart", app_id=DD_APP, name="Darkest Dungeon"
    )
    source, pk = _seed_and_register(
        db,
        tmp_path / "library",
        app_id=DD_APP,
        game_name="Darkest Dungeon",
        folder=folder,
        external_id=workspace_id,
    )
    result = ModDeployer(library_root=tmp_path / "library", db=db).deploy_mod(
        _frozen(db, pk)
    )
    assert result["success"] is True, result
    target = Path(result["target"]).resolve()
    assert target.name == expected
    assert target == (mods / expected).resolve()
    assert source.name == folder
    assert not (mods / "anheidilaozengqiang").exists()


def test_darkest_dungeon_occultist_folder_uses_workspace_id(
    tmp_path: Path, db: DatabaseManager
) -> None:
    folder = "神秘学者增强"
    workspace_id = "262060104"
    expected = deploy_folder_name(workspace_id)
    mods = _configure_folder_copy(
        db, tmp_path / "DDOccultist", app_id=DD_APP, name="Darkest Dungeon"
    )
    source, pk = _seed_and_register(
        db,
        tmp_path / "library",
        app_id=DD_APP,
        game_name="Darkest Dungeon",
        folder=folder,
        external_id=workspace_id,
    )
    result = ModDeployer(library_root=tmp_path / "library", db=db).deploy_mod(
        _frozen(db, pk)
    )
    assert result["success"] is True, result
    target = Path(result["target"]).resolve()
    assert target.name == expected
    assert target == (mods / expected).resolve()
    assert source.name == folder
    assert not (mods / folder).exists()


def test_unknown_game_uses_workspace_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    app_id = 424242
    folder = "测试MOD"
    workspace_id = "555666778"
    expected = deploy_folder_name(workspace_id)
    mods = _configure_folder_copy(
        db, tmp_path / "UnknownMods", app_id=app_id, name="SomeGame"
    )
    source, pk = _seed_and_register(
        db,
        tmp_path / "library",
        app_id=app_id,
        game_name="SomeGame",
        folder=folder,
        external_id=workspace_id,
        filename="a.txt",
    )
    result = ModDeployer(library_root=tmp_path / "library", db=db).deploy_mod(
        _frozen(db, pk)
    )
    assert result["success"] is True, result
    target = Path(result["target"]).resolve()
    assert target.name == expected
    assert target == (mods / expected).resolve()
    assert source.name == folder
    assert not (mods / "ceshi_MOD").exists()


def _assert_civ6_deploys_workspace_folder(
    tmp_path: Path,
    db: DatabaseManager,
    *,
    mods_dir: str,
    folder: str,
    external_id: str,
) -> None:
    expected = deploy_folder_name(external_id)
    mods = _configure_folder_copy(db, tmp_path / mods_dir, app_id=CIV6, name="文明Ⅵ")
    source, pk = _seed_and_register(
        db,
        tmp_path / "library",
        app_id=CIV6,
        game_name="文明Ⅵ",
        folder=folder,
        external_id=external_id,
        filename="file1.modinfo",
    )
    result = ModDeployer(library_root=tmp_path / "library", db=db).deploy_mod(
        _frozen(db, pk)
    )
    assert result["success"] is True, result
    target = Path(result["target"]).resolve()
    assert target == (mods / expected).resolve()
    assert target.name == expected
    assert target.name != pk
    meta = db.get_mod(pk)
    assert meta is not None
    assert str(meta.internal_id) != target.name
    assert target.name != f"mod_{pk}"
    assert source.name == folder
    assert not (mods / folder).exists()
    assert not (mods / external_id).exists()


def test_civ6_chinese_folder_uses_workspace_id(
    tmp_path: Path, db: DatabaseManager
) -> None:
    _write_caps(tmp_path, {str(CIV6): {CAP: True}, str(DD_APP): {CAP: True}})
    _assert_civ6_deploys_workspace_folder(
        tmp_path,
        db,
        mods_dir="CivCaps",
        folder="三国全面战争",
        external_id="123456789",
    )


def test_civ6_production_json_uses_workspace_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Production deploy folder is mod_{workspace_id}, never pinyin or PK."""
    _assert_civ6_deploys_workspace_folder(
        tmp_path,
        db,
        mods_dir="CivProd",
        folder="三国全面战争",
        external_id="123456790",
    )
