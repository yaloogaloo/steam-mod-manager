"""Stardew Valley deploy: SMAPI manifest.json → configured Mods directory."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_rules import (
    DEPLOY_TYPE_STARDEW_VALLEY,
    load_manifest,
    resolve_deploy_type,
)
from services.deploy_rules.stardew_valley import (
    STARDEW_VALLEY_APP_ID,
    find_smapi_mod_roots,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME

SV_APP = STARDEW_VALLEY_APP_ID


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "stardew_deploy.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _write_smapi_manifest(folder: Path, *, unique_id: str = "Test.Mod") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.json").write_text(
        json.dumps(
            {
                "Name": unique_id,
                "Author": "Tester",
                "Version": "1.0.0",
                "Description": "test",
                "UniqueID": unique_id,
                "MinimumApiVersion": "3.0.0",
                "EntryDll": "Mod.dll",
            }
        ),
        encoding="utf-8",
    )
    (folder / "Mod.dll").write_bytes(b"MZ")


def _seed_managed_with_zip(
    library: Path,
    *,
    mid: str,
    title: str,
    zip_name: str,
    zip_members: dict[str, bytes],
) -> Path:
    mod = library / "星露谷物语" / title
    mod.mkdir(parents=True)
    archive = mod / zip_name
    with zipfile.ZipFile(archive, "w") as zf:
        for name, data in zip_members.items():
            zf.writestr(name, data)
    info = mod / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "{title}",\n'
        f'  "app_id": {SV_APP},\n'
        '  "game_name": "星露谷物语"\n'
        "}\n",
        encoding="utf-8",
    )
    return mod


def test_resolve_stardew_deploy_type() -> None:
    assert resolve_deploy_type(SV_APP, "folder_copy") == DEPLOY_TYPE_STARDEW_VALLEY
    assert resolve_deploy_type(1623730, "folder_copy") == "palworld_pak"


def test_find_smapi_mod_roots_nested(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    _write_smapi_manifest(pack / "aaa" / "bbb", unique_id="A.B")
    _write_smapi_manifest(pack / "aaa" / "ccc", unique_id="A.C")
    roots = find_smapi_mod_roots(pack)
    names = sorted(p.name for p in roots)
    assert names == ["bbb", "ccc"]


def test_stardew_flat_archive_uses_zip_stem(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Case 1: zip root has manifest.json → Mods/<zip_stem>/."""
    library = tmp_path / "library"
    mods_dir = tmp_path / "StardewMods"  # intentionally missing until deploy

    mid = "41315001"
    mod = _seed_managed_with_zip(
        library,
        mid=mid,
        title="FlatMod",
        zip_name="CoolFlatMod.zip",
        zip_members={
            "manifest.json": b'{"Name":"Flat","UniqueID":"Flat.Mod"}',
            "config.json": b"{}",
            "assets/icon.png": b"PNG",
        },
    )

    db.update_game_deploy_config(
        SV_APP,
        name="星露谷物语",
        mod_path=str(mods_dir),
        deploy_type="folder_copy",
    )
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title="FlatMod", app_id=SV_APP)
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(mid)
    assert result["success"] is True, result
    assert result["deploy_type"] == DEPLOY_TYPE_STARDEW_VALLEY
    assert mods_dir.is_dir()
    dest = mods_dir / "CoolFlatMod"
    assert (dest / "manifest.json").is_file()
    assert (dest / "config.json").is_file()
    assert (dest / "assets" / "icon.png").is_file()
    assert not (mods_dir / "manifest.json").exists()

    man = load_manifest(mod)
    assert man is not None
    assert man.deploy_type == DEPLOY_TYPE_STARDEW_VALLEY
    assert any(Path(f.target).name == "manifest.json" for f in man.files)


def test_stardew_single_folder_archive(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Case 2: zip contains xxx/manifest.json → Mods/xxx/."""
    library = tmp_path / "library"
    mods_dir = tmp_path / "Mods"
    mods_dir.mkdir()

    mid = "41315002"
    _seed_managed_with_zip(
        library,
        mid=mid,
        title="WrappedMod",
        zip_name="WrappedMod-1.0.zip",
        zip_members={
            "ContentPatcher/manifest.json": b'{"Name":"CP","UniqueID":"CP"}',
            "ContentPatcher/ContentPatcher.dll": b"MZ",
            "ContentPatcher/config.json": b"{}",
        },
    )

    db.update_game_deploy_config(
        SV_APP, name="星露谷物语", mod_path=str(mods_dir)
    )
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title="WrappedMod", app_id=SV_APP)
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(mid)
    assert result["success"] is True, result
    dest = mods_dir / "ContentPatcher"
    assert (dest / "manifest.json").is_file()
    assert (dest / "ContentPatcher.dll").is_file()
    assert not (mods_dir / "WrappedMod-1.0" / "manifest.json").exists()
    assert not (mods_dir / "manifest.json").exists()


def test_stardew_multi_mod_pack(tmp_path: Path, db: DatabaseManager) -> None:
    """Case 3: pack with several manifest roots → deploy each Mod folder."""
    library = tmp_path / "library"
    mods_dir = tmp_path / "Mods"
    mods_dir.mkdir()

    mid = "41315003"
    _seed_managed_with_zip(
        library,
        mid=mid,
        title="MultiPack",
        zip_name="MultiPack.zip",
        zip_members={
            "aaa/bbb/manifest.json": b'{"Name":"B","UniqueID":"B"}',
            "aaa/bbb/ModB.dll": b"B",
            "aaa/ccc/manifest.json": b'{"Name":"C","UniqueID":"C"}',
            "aaa/ccc/ModC.dll": b"C",
        },
    )

    db.update_game_deploy_config(
        SV_APP, name="星露谷物语", mod_path=str(mods_dir)
    )
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title="MultiPack", app_id=SV_APP)
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(mid)
    assert result["success"] is True, result
    assert (mods_dir / "bbb" / "manifest.json").is_file()
    assert (mods_dir / "bbb" / "ModB.dll").is_file()
    assert (mods_dir / "ccc" / "manifest.json").is_file()
    assert (mods_dir / "ccc" / "ModC.dll").is_file()
    assert not (mods_dir / "aaa").exists()
