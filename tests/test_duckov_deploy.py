"""Duckov (AppID 3167020) deploy — info.ini anchor + zip layout."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.deploy import ModDeployer
from services.deploy_rules.duckov import (
    DUCKOV_APP_ID,
    find_duckov_mod_root,
    validate_duckov_target,
)
from services.file_ops import INFO_DIR_NAME
from services.importers.archive import extract_archive
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "duckov_deploy.db")
    yield manager
    DatabaseManager.reset_instance()


def _configure_duckov(db: DatabaseManager, tmp_path: Path) -> Path:
    mod_path = tmp_path / "DuckovMods"
    mod_path.mkdir(parents=True)
    db.update_game_deploy_config(
        DUCKOV_APP_ID,
        name="Escape from Duckov",
        install_path=str(tmp_path / "Game"),
        mod_path=str(mod_path),
        deploy_type="folder_copy",
    )
    return mod_path


def _seed_mod(
    library: Path,
    db: DatabaseManager,
    *,
    folder: str,
    external_id: str,
    files: dict[str, bytes | str],
) -> tuple[Path, str]:
    created = create_steam_test_mod(
        db,
        external_id=str(external_id),
        title=folder,
        app_id=DUCKOV_APP_ID,
        game_name="DuckovGame",
    )
    mod_dir = library / "DuckovGame" / folder
    mod_dir.mkdir(parents=True)
    for rel, data in files.items():
        path = mod_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
    pk = prove_managed_folder(
        db,
        mod_dir,
        handle=created.mod_id,
        title=folder,
        app_id=DUCKOV_APP_ID,
        game_name="DuckovGame",
    )
    return mod_dir, pk


def _make_zip(path: Path, mapping: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in mapping.items():
            zf.writestr(name, data)


def test_case1_plain_directory_deploy(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mod_path = _configure_duckov(db, tmp_path)
    folder = "Pokemon Mod"
    _, pk = _seed_mod(
        library,
        db,
        folder=folder,
        external_id="90001",
        files={
            "info.ini": "[Mod]\nname=Pokemon\n",
            "a.dll": b"MZ",
            "assets/x.txt": "ok",
        },
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result

    target = mod_path / folder
    assert (target / "info.ini").is_file()
    assert (target / "a.dll").is_file()
    assert (target / "assets" / "x.txt").is_file()
    assert not (target / INFO_DIR_NAME).exists()


def test_case2_zip_flat_contents(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mod_path = _configure_duckov(db, tmp_path)
    folder = "ZipFlat"
    _, pk = _seed_mod(
        library,
        db,
        folder=folder,
        external_id="90002",
        files={
            "Mod.zip": b"",  # placeholder overwritten below
        },
    )
    zip_path = library / "DuckovGame" / folder / "Mod.zip"
    _make_zip(
        zip_path,
        {
            "info.ini": b"[Mod]\n",
            "a.dll": b"MZ",
            "assets/x.txt": b"ok",
        },
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    target = mod_path / folder
    assert (target / "info.ini").is_file()
    assert not (target / "Mod.zip").exists()
    assert not (target / folder / "info.ini").exists()


def test_case3_zip_single_wrapper_unwrapped(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mod_path = _configure_duckov(db, tmp_path)
    folder = "Wrapped Mod"
    _, pk = _seed_mod(
        library,
        db, folder=folder, external_id="90003", files={})
    zip_path = library / "DuckovGame" / folder / "content.zip"
    _make_zip(
        zip_path,
        {
            "Wrapped Mod/info.ini": b"[Mod]\n",
            "Wrapped Mod/a.dll": b"MZ",
        },
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    target = mod_path / folder
    assert (target / "info.ini").is_file()
    assert not (target / folder / "info.ini").exists()


def test_case4_missing_info_ini_fails(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    _configure_duckov(db, tmp_path)
    folder = "NoIni"
    _, pk = _seed_mod(
        library,
        db,
        folder=folder,
        external_id="90004",
        files={"a.dll": b"MZ"},
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is False
    assert "info.ini" in str(result.get("error") or "")


def test_case5_wrong_nesting_not_success(tmp_path: Path) -> None:
    target = tmp_path / "MyMod"
    target.mkdir()
    nested = target / "MyMod"
    nested.mkdir()
    (nested / "info.ini").write_text("[Mod]\n", encoding="utf-8")
    err = validate_duckov_target(target, folder_name="MyMod")
    assert err is not None
    assert "info.ini" in err


def test_case6_zip_slip_rejected(tmp_path: Path) -> None:
    zpath = tmp_path / "evil.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("../../evil.txt", b"bad")
    with pytest.raises(RuntimeError, match="不安全的压缩包路径"):
        extract_archive(zpath, dest_dir=tmp_path / "out")


def test_case7_missing_mod_path(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    db.update_game_deploy_config(
        DUCKOV_APP_ID,
        name="Escape from Duckov",
        install_path=str(tmp_path / "Game"),
        mod_path="",
        deploy_type="folder_copy",
    )
    folder = "Any"
    _, pk = _seed_mod(
        library,
        db,
        folder=folder,
        external_id="90007",
        files={"info.ini": "[Mod]\n"},
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is False
    assert "Mod 部署目录" in str(result.get("error") or "")


def test_case8_uses_game_mod_path_not_hardcoded(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    custom_mod_path = tmp_path / "Configured" / "Mods"
    custom_mod_path.mkdir(parents=True)
    db.update_game_deploy_config(
        DUCKOV_APP_ID,
        name="Escape from Duckov",
        install_path=str(tmp_path / "Game"),
        mod_path=str(custom_mod_path),
        deploy_type="folder_copy",
    )
    folder = "CfgMod"
    _, pk = _seed_mod(
        library,
        db,
        folder=folder,
        external_id="90008",
        files={"info.ini": "[Mod]\n", "x.dll": b"1"},
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert (custom_mod_path / folder / "info.ini").is_file()


def test_case9_other_game_folder_copy_regression(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    game_mods = tmp_path / "OtherMods"
    game_mods.mkdir()
    db.update_game_deploy_config(
        99999,
        name="OtherGame",
        mod_path=str(game_mods),
        deploy_type="folder_copy",
    )
    folder = "PlainMod"
    created = create_steam_test_mod(
        db, external_id="8800", title=folder, app_id=99999, game_name="OtherGame"
    )
    mod_dir = library / "OtherGame" / folder
    mod_dir.mkdir(parents=True)
    (mod_dir / "file.txt").write_text("ok", encoding="utf-8")
    pk = prove_managed_folder(
        db,
        mod_dir,
        handle=created.mod_id,
        title=folder,
        app_id=99999,
        game_name="OtherGame",
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert (game_mods / folder / "file.txt").is_file()


def test_empty_marker_file_does_not_fail_deploy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mod_path = _configure_duckov(db, tmp_path)
    folder = "Daily Interest"
    _, pk = _seed_mod(
        library,
        db,
        folder=folder,
        external_id="90010",
        files={
            "info.ini": "[Mod]\nname=Daily\n",
            "NODEBUG_DAILY_INTEREST": b"",
            "DailyInterest.dll": b"MZ",
        },
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result

    target = mod_path / folder
    marker = target / "NODEBUG_DAILY_INTEREST"
    assert marker.is_file()
    assert marker.stat().st_size == 0
    assert (target / "info.ini").is_file()


def test_empty_directory_does_not_fail_deploy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mod_path = _configure_duckov(db, tmp_path)
    folder = "EmptyDirMod"
    mod_dir, pk = _seed_mod(
        library,
        db,
        folder=folder,
        external_id="90011",
        files={
            "info.ini": "[Mod]\n",
            "assets/x.txt": "ok",
        },
    )
    (mod_dir / "assets").mkdir(exist_ok=True)
    result = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert result["success"] is True, result
    assert (mod_path / folder / "info.ini").is_file()
    assert (mod_path / folder / "assets" / "x.txt").is_file()


def test_find_duckov_mod_root_prefers_shallow_info_ini(tmp_path: Path) -> None:
    root = tmp_path / "extract"
    root.mkdir()
    deep = root / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "info.ini").write_text("[Mod]\n", encoding="utf-8")
    found = find_duckov_mod_root(root)
    assert found == deep
