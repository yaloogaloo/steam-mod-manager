"""Selected nested archive must not replace the managed outer tree."""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import ModFileEntry, ModFilesBundle
from services.deploy import ModDeployer
from services.deploy_rules.manifest import load_manifest
from services.file_ops import INFO_DIR_NAME
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "nested_archive_outer.db")
    manager.upsert_game(
        GameInfo(app_id=100, name="SomeGame", folder_name="SomeGame")
    )
    yield manager
    DatabaseManager.reset_instance()


def _write_zip(path: Path, mapping: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in mapping.items():
            zf.writestr(name, data)
    return path


def test_selected_archive_keeps_outer_files_and_extracted_files(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    install_mods = tmp_path / "GameMods"
    install_mods.mkdir()
    managed = library / "SomeGame" / "NestedMod"
    managed.mkdir(parents=True)
    (managed / INFO_DIR_NAME).mkdir()
    (managed / "outer_file.txt").write_text("outer-keep", encoding="utf-8")
    (managed / "outer_dir").mkdir()
    (managed / "outer_dir" / "keep.bin").write_bytes(b"keep")
    _write_zip(managed / "inner.zip", {"inner_file.txt": b"from-archive"})

    created = create_steam_test_mod(
        db, external_id="3308841144", title="NestedMod", app_id=100
    )
    pk = prove_managed_folder(
        db,
        managed,
        handle=created.mod_id,
        title="NestedMod",
        app_id=100,
        game_name="SomeGame",
    )
    db.set_mod_files(
        pk,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    filename="inner.zip",
                    path="inner.zip",
                    selected_for_deploy=True,
                    enabled=True,
                )
            ]
        ),
    )
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(install_mods))

    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is True, result

    dest = Path(result["target"]).resolve()
    assert dest == (install_mods / "NestedMod").resolve()
    assert dest.name == "NestedMod"
    assert (dest / "outer_file.txt").read_text(encoding="utf-8") == "outer-keep"
    assert (dest / "outer_dir" / "keep.bin").read_bytes() == b"keep"
    assert (dest / "inner_file.txt").read_bytes() == b"from-archive"
    assert not (dest / "inner.zip").exists()
    names = {p.name for p in dest.rglob("*") if p.is_file()}
    assert "outer_file.txt" in names
    assert "keep.bin" in names
    assert "inner_file.txt" in names
    assert "inner.zip" not in names

    manifest = load_manifest(managed)
    assert manifest is not None
    outer_src = [
        Path(e.source).resolve()
        for e in manifest.files
        if Path(e.source).name == "outer_file.txt"
    ]
    assert outer_src
    assert outer_src[0] == (managed / "outer_file.txt").resolve()
    assert "import_cache" not in str(outer_src[0])
    inner_src = [
        Path(e.source).resolve()
        for e in manifest.files
        if Path(e.source).name == "inner_file.txt"
    ]
    assert inner_src
    assert inner_src[0] != (managed / "inner_file.txt")
    assert (managed / "inner.zip").is_file()


def test_outer_file_wins_same_relative_path_conflict(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    install_mods = tmp_path / "GameModsConflict"
    install_mods.mkdir()
    managed = library / "SomeGame" / "ConflictMod"
    managed.mkdir(parents=True)
    (managed / INFO_DIR_NAME).mkdir()
    (managed / "shared.txt").write_text("from-outer", encoding="utf-8")
    _write_zip(managed / "inner.zip", {"shared.txt": b"from-archive", "only_inner.txt": b"inner"})

    created = create_steam_test_mod(
        db, external_id="3308841145", title="ConflictMod", app_id=100
    )
    pk = prove_managed_folder(
        db,
        managed,
        handle=created.mod_id,
        title="ConflictMod",
        app_id=100,
        game_name="SomeGame",
    )
    db.set_mod_files(
        pk,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    filename="inner.zip",
                    path="inner.zip",
                    selected_for_deploy=True,
                    enabled=True,
                )
            ]
        ),
    )
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(install_mods))

    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is True, result
    dest = Path(result["target"]).resolve()
    assert dest.name == "ConflictMod"
    assert (dest / "shared.txt").read_text(encoding="utf-8") == "from-outer"
    assert (dest / "only_inner.txt").read_bytes() == b"inner"


def test_archive_only_managed_folder_stays_extract_payload(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    install_mods = tmp_path / "GameModsZipOnly"
    install_mods.mkdir()
    managed = library / "SomeGame" / "ZipOnly"
    managed.mkdir(parents=True)
    (managed / INFO_DIR_NAME).mkdir()
    _write_zip(managed / "mod.zip", {"mod.dll": b"MZ", "config.ini": b"a=1"})

    created = create_steam_test_mod(
        db, external_id="8801", title="ZipOnly", app_id=100
    )
    pk = prove_managed_folder(
        db,
        managed,
        handle=created.mod_id,
        title="ZipOnly",
        app_id=100,
        game_name="SomeGame",
    )
    db.set_mod_files(
        pk,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    filename="mod.zip",
                    path="mod.zip",
                    selected_for_deploy=True,
                    enabled=True,
                )
            ]
        ),
    )
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(install_mods))

    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is True, result
    dest = Path(result["target"]).resolve()
    assert dest.name == "ZipOnly"
    assert (dest / "mod.dll").is_file()
    assert (dest / "config.ini").is_file()
    assert not (dest / "mod.zip").exists()


def test_outer_payload_is_not_copied_into_import_cache(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    install_mods = tmp_path / "GameModsOnce"
    install_mods.mkdir()
    managed = library / "SomeGame" / "OnceMod"
    managed.mkdir(parents=True)
    (managed / INFO_DIR_NAME).mkdir()
    blob = os.urandom(2 * 1024 * 1024)
    (managed / "big.bin").write_bytes(blob)
    _write_zip(managed / "inner.zip", {"inner_file.txt": b"x"})

    created = create_steam_test_mod(
        db, external_id="3308841199", title="OnceMod", app_id=100
    )
    pk = prove_managed_folder(
        db,
        managed,
        handle=created.mod_id,
        title="OnceMod",
        app_id=100,
        game_name="SomeGame",
    )
    db.set_mod_files(
        pk,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    filename="inner.zip",
                    path="inner.zip",
                    selected_for_deploy=True,
                    enabled=True,
                )
            ]
        ),
    )
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(install_mods))

    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is True, result
    dest = Path(result["target"]).resolve()
    assert dest.name == "OnceMod"
    assert (dest / "big.bin").read_bytes() == blob
    assert (dest / "inner_file.txt").read_bytes() == b"x"
    manifest = load_manifest(managed)
    assert manifest is not None
    big_src = next(
        Path(e.source).resolve()
        for e in manifest.files
        if Path(e.source).name == "big.bin"
    )
    assert big_src == (managed / "big.bin").resolve()
    assert "import_cache" not in str(big_src).replace("\\", "/")


def test_prepare_does_not_copy_outer_tree_into_stage(
    tmp_path: Path, db: DatabaseManager
) -> None:
    from services.deploy import prepare_deploy_content
    from services.importers.archive import cleanup_import_cache

    library = tmp_path / "library"
    managed = library / "SomeGame" / "StageMod"
    managed.mkdir(parents=True)
    (managed / INFO_DIR_NAME).mkdir()
    blob = os.urandom(3 * 1024 * 1024)
    (managed / "big.bin").write_bytes(blob)
    _write_zip(managed / "inner.zip", {"inner_file.txt": b"only-extract"})

    created = create_steam_test_mod(
        db, external_id="3308841200", title="StageMod", app_id=100
    )
    pk = prove_managed_folder(
        db,
        managed,
        handle=created.mod_id,
        title="StageMod",
        app_id=100,
        game_name="SomeGame",
    )
    db.set_mod_files(
        pk,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    filename="inner.zip",
                    path="inner.zip",
                    selected_for_deploy=True,
                    enabled=True,
                )
            ]
        ),
    )

    content, _allowed, cleanup, overlays = prepare_deploy_content(pk, managed, db=db)
    try:
        assert content.resolve() == managed.resolve()
        assert overlays
        assert not any(
            path.name == "big.bin"
            for root in overlays
            for path in root.rglob("*")
            if path.is_file()
        )
        stage_bytes = 0
        if cleanup is not None and cleanup.exists():
            stage_bytes = sum(
                path.stat().st_size for path in cleanup.rglob("*") if path.is_file()
            )
        assert stage_bytes < len(blob)
        assert stage_bytes < 1024 * 1024
    finally:
        if cleanup is not None:
            cleanup_import_cache(cleanup)
