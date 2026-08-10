"""Anno 1800 deploy targets ``<install>/mods/<mod>/``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_rules import DEPLOY_TYPE_ANNO_1800, resolve_deploy_type
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME

ANNO_APP = 916440


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "anno_deploy.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_resolve_anno_deploy_type() -> None:
    assert resolve_deploy_type(ANNO_APP, "folder_copy") == DEPLOY_TYPE_ANNO_1800
    assert resolve_deploy_type(1623730, "folder_copy") == "palworld_pak"


def test_anno_deploy_creates_mods_and_copies(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "library"
    install = tmp_path / "AnnoInstall"
    install.mkdir()
    # mods/ must be created by strategy
    assert not (install / "mods").exists()

    mod = library / "Anno 1800" / "BiggerHarbour"
    mod.mkdir(parents=True)
    (mod / "data").mkdir()
    (mod / "data" / "config.xml").write_text("<ok/>", encoding="utf-8")
    info = mod / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        "{\n"
        '  "published_file_id": "91601",\n'
        '  "title": "BiggerHarbour",\n'
        f'  "app_id": {ANNO_APP},\n'
        '  "game_name": "Anno 1800"\n'
        "}\n",
        encoding="utf-8",
    )

    db.update_game_deploy_config(
        ANNO_APP,
        name="Anno 1800",
        install_path=str(install),
        deploy_type="folder_copy",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="91601",
            title="BiggerHarbour",
            app_id=ANNO_APP,
        )
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod("91601")
    assert result["success"] is True, result
    assert result["deploy_type"] == DEPLOY_TYPE_ANNO_1800
    target = Path(result["target"])
    assert target == (install / "mods" / "BiggerHarbour").resolve()
    assert (target / "data" / "config.xml").is_file()
    assert (install / "mods").is_dir()


def test_anno_deploy_directory_mod_with_stale_unselected_files(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Pure directory Mod: stale DB file entries must not cause fake deploy."""
    library = tmp_path / "library"
    install = tmp_path / "AnnoInstall"
    install.mkdir()

    mod = library / "Anno 1800" / "LooseMod"
    mod.mkdir(parents=True)
    (mod / "data").mkdir()
    (mod / "data" / "mod.json").write_text("{}", encoding="utf-8")
    info = mod / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": "91602",
                "title": "LooseMod",
                "app_id": ANNO_APP,
                "game_name": "Anno 1800",
            }
        ),
        encoding="utf-8",
    )

    db.update_game_deploy_config(
        ANNO_APP,
        name="Anno 1800",
        install_path=str(install),
        deploy_type="folder_copy",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="91602",
            title="LooseMod",
            app_id=ANNO_APP,
        )
    )
    # Stale entries from old scanner — none selected for deploy.
    from core.mod_platform import ModFileEntry, ModFilesBundle

    db.set_mod_files(
        "91602",
        ModFilesBundle(
            files=[
                ModFileEntry(
                    id="a",
                    filename="data/mod.json",
                    path="data/mod.json",
                    selected_for_deploy=False,
                ),
                ModFileEntry(
                    id="b",
                    filename="other.pak",
                    path="other.pak",
                    selected_for_deploy=False,
                ),
            ]
        ),
    )

    from services.deploy_rules import load_manifest

    result = ModDeployer(library_root=library, db=db).deploy_mod("91602")
    assert result["success"] is True, result
    target = Path(result["target"])
    assert (target / "data" / "mod.json").is_file()
    assert result["copied_files"] >= 1

    manifest = load_manifest(mod)
    assert manifest is not None
    assert len(manifest.files) >= 1
    assert any(Path(f.target).name == "mod.json" for f in manifest.files)


def test_anno_deploy_extracts_zip_preserves_inner_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """mod.io zip: extract into mods/ root, keep in-archive folder names."""
    import zipfile

    from services.deploy_rules import load_manifest

    library = tmp_path / "library"
    install = tmp_path / "AnnoInstall"
    install.mkdir()

    managed_name = "仓库装卸坡道（2个）"
    mod = library / "Anno 1800" / managed_name
    mod.mkdir(parents=True)
    info = mod / INFO_DIR_NAME
    info.mkdir()
    inner = "[Addon] 仓库坡道"
    with zipfile.ZipFile(mod / "mod.zip", "w") as zf:
        zf.writestr(f"{inner}/modinfo.json", "{}", compress_type=zipfile.ZIP_STORED)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": "91603",
                "title": managed_name,
                "app_id": ANNO_APP,
                "game_name": "Anno 1800",
            }
        ),
        encoding="utf-8",
    )

    db.update_game_deploy_config(
        ANNO_APP,
        name="Anno 1800",
        install_path=str(install),
        deploy_type="folder_copy",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id="91603",
            title=managed_name,
            app_id=ANNO_APP,
        )
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod("91603")
    assert result["success"] is True, result
    mods_root = (install / "mods").resolve()
    assert Path(result["target"]).resolve() == mods_root
    assert (mods_root / inner / "modinfo.json").is_file()
    assert not (mods_root / managed_name).exists()
    assert not (mods_root / "mod.zip").exists()

    manifest = load_manifest(mod)
    assert manifest is not None
    assert len(manifest.files) >= 1
    assert any(
        Path(f.target).resolve() == (mods_root / inner / "modinfo.json").resolve()
        for f in manifest.files
    )
