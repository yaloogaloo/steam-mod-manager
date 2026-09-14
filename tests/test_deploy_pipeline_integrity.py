"""End-to-end deploy pipeline integrity (source → extract → validate → result)."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import FILE_TYPE_MAIN, ModFileEntry, ModFilesBundle
from services.deploy import ModDeployer
from services.deploy_rules.generic import FolderCopyStrategy
from tests.helpers.deploy import patch_apply_then_unlink_targets
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry, load_manifest
from services.deploy_rules.stardew_valley import STARDEW_VALLEY_APP_ID
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_status import CONTENT_HEALTHY
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


BG3_APP_ID = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "pipeline_integrity.db")
    manager.upsert_game(GameInfo(app_id=100, name="SomeGame", folder_name="SomeGame"))
    manager.upsert_game(
        GameInfo(app_id=BG3_APP_ID, name="Baldur's Gate 3", folder_name="BG3")
    )
    manager.upsert_game(
        GameInfo(
            app_id=STARDEW_VALLEY_APP_ID,
            name="Stardew Valley",
            folder_name="Stardew Valley",
        )
    )
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _make_zip(path: Path, mapping: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in mapping.items():
            zf.writestr(name, data)
    return path


def _register(
    db: DatabaseManager,
    *,
    mid: str,
    path: str,
    app_id: int = 100,
    title: str = "PipeMod",
    game_name: str = "SomeGame",
) -> str:
    created = create_steam_test_mod(
        db, external_id=mid, title=title, app_id=app_id, game_name=game_name
    )
    pk = str(created.mod_id)
    prove_managed_folder(
        db,
        Path(path),
        handle=pk,
        title=title,
        app_id=app_id,
        game_name=game_name,
    )
    db.update_mod_content_status(
        pk, content_status=CONTENT_HEALTHY, folder_present=True
    )
    return pk


def _set_archive_entry(db: DatabaseManager, mid: str, filename: str) -> None:
    db.set_mod_files(
        mid,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    name=filename,
                    filename=filename,
                    path=filename,
                    type=FILE_TYPE_MAIN,
                    enabled=True,
                )
            ]
        ),
    )


# ---------------------------------------------------------------------------
# Case 1 — zip mod extracts and deploys
# ---------------------------------------------------------------------------


def test_case1_zip_mod_extracts_and_deploys(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = tmp_path / "GameMods"
    mods.mkdir()
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(mods))

    managed = library / "SomeGame" / "ZipMod"
    managed.mkdir(parents=True)
    _make_zip(
        managed / "pack.zip",
        {"mod.dll": b"MZ", "config.ini": b"a=1"},
    )
    pk = _register(db, mid="94001", path=str(managed), title="ZipMod")
    _set_archive_entry(db, pk, "pack.zip")

    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out["success"] is True, out
    dest = mods / "ZipMod"
    assert (dest / "mod.dll").is_file()
    assert (dest / "config.ini").is_file()
    assert not (dest / "pack.zip").exists()
    assert out.get("validated", 0) >= 2
    assert load_manifest(managed) is not None


# ---------------------------------------------------------------------------
# Case 2 — archive missing, managed has legal loose content → allow
# ---------------------------------------------------------------------------


def test_case2_missing_archive_but_loose_content_allowed(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = tmp_path / "GameMods"
    mods.mkdir()
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(mods))

    managed = library / "SomeGame" / "LooseMod"
    managed.mkdir(parents=True)
    (managed / "logic.dll").write_bytes(b"MZDATA")
    (managed / "readme.txt").write_text("ok", encoding="utf-8")
    pk = _register(db, mid="94002", path=str(managed), title="LooseMod")
    # DB still lists a zip that is no longer on disk (already extracted).
    _set_archive_entry(db, pk, "WASD-781-1-9-8-1758653752.zip")

    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out["success"] is True, out
    assert (mods / "LooseMod" / "logic.dll").is_file()
    assert (mods / "LooseMod" / "readme.txt").is_file()
    # Must not invent a success with zero files.
    assert (out.get("files") or []) and out.get("validated", 0) >= 1


# ---------------------------------------------------------------------------
# Case 3 — archive missing, managed only has zip → reject
# ---------------------------------------------------------------------------


def test_case3_missing_archive_archives_only_rejected(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = tmp_path / "GameMods"
    mods.mkdir()
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(mods))

    managed = library / "SomeGame" / "ZipOnly"
    managed.mkdir(parents=True)
    # Leftover unrelated archive; listed source zip is gone.
    _make_zip(managed / "leftover.zip", {"inside.dll": b"MZ"})
    pk = _register(db, mid="94003", path=str(managed), title="ZipOnly")
    _set_archive_entry(db, pk, "WASD-781-1-9-8-1758653752.zip")

    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out["success"] is False, out
    assert "files" not in out or not out.get("files")
    assert load_manifest(managed) is None
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status != DEPLOY_STATUS_DEPLOYED
    # Must not copy leftover.zip into game mods as a "successful" deploy.
    dest = mods / "ZipOnly"
    assert not dest.exists() or not any(dest.rglob("*"))


# ---------------------------------------------------------------------------
# Case 4 — copy failure cannot report success
# ---------------------------------------------------------------------------


def test_case4_copy_failure_not_success(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = tmp_path / "GameMods"
    mods.mkdir()
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(mods))

    managed = library / "SomeGame" / "CopyFail"
    managed.mkdir(parents=True)
    (managed / "a.txt").write_text("a", encoding="utf-8")
    pk = _register(db, mid="94004", path=str(managed), title="CopyFail")

    with patch(
        "services.deploy_apply.shutil.copy2",
        side_effect=OSError("simulated copy failure"),
    ):
        out = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert out["success"] is False, out
    assert load_manifest(managed) is None
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status != DEPLOY_STATUS_DEPLOYED


# ---------------------------------------------------------------------------
# Case 5 — manifest target missing → not success
# ---------------------------------------------------------------------------


def test_case5_missing_manifest_target_not_success(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = tmp_path / "GameMods"
    mods.mkdir()
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(mods))

    managed = library / "SomeGame" / "Ghost"
    managed.mkdir(parents=True)
    (managed / "a.txt").write_text("a", encoding="utf-8")
    pk = _register(db, mid="94005", path=str(managed), title="Ghost")

    with patch_apply_then_unlink_targets():
        out = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert out["success"] is False
    assert out.get("reason") == "missing_targets"
    assert load_manifest(managed) is None


# ---------------------------------------------------------------------------
# Case 6 — BG3 CustomPath keeps bin/ layout
# ---------------------------------------------------------------------------


def test_case6_bg3_custom_path_preserves_bin(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    game_root = tmp_path / "Baldurs Gate 3"
    (game_root / "bin").mkdir(parents=True)
    (game_root / "Data").mkdir(parents=True)
    db.update_game_deploy_config(
        BG3_APP_ID,
        name="Baldur's Gate 3",
        install_path=str(game_root),
        mod_path=str(tmp_path / "unused_mods"),
        deploy_type="folder_copy",
    )

    managed = library / "BG3" / "NativeLoader"
    managed.mkdir(parents=True)
    _make_zip(
        managed / "loader.zip",
        {
            "bin/bink2w64.dll": b"DLL1",
            "bin/bink2w64_original.dll": b"DLL2",
        },
    )
    pk = _register(
        db, mid="94006", path=str(managed), app_id=BG3_APP_ID, title="NativeLoader", game_name="Baldur's Gate 3"
    )
    _set_archive_entry(db, pk, "loader.zip")
    db.update_mod_user_metadata(
        pk,
        {
            "display_name": "NativeLoader",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
            "custom_deploy_path": str(game_root),
        },
    )

    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out["success"] is True, out
    assert (game_root / "bin" / "bink2w64.dll").read_bytes() == b"DLL1"
    assert (game_root / "bin" / "bink2w64_original.dll").read_bytes() == b"DLL2"
    # Must NOT flatten into game root.
    assert not (game_root / "bink2w64.dll").exists()


# ---------------------------------------------------------------------------
# Case 7 — Stardew zip mod still works (preserve layout, no false reject)
# ---------------------------------------------------------------------------


def test_case7_stardew_zip_mod_not_regressed(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods_dir = tmp_path / "StardewMods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    db.update_game_deploy_config(
        STARDEW_VALLEY_APP_ID,
        name="Stardew Valley",
        install_path=str(tmp_path / "StardewInstall"),
        mod_path=str(mods_dir),
        deploy_type="stardew_valley",
    )

    managed = library / "Stardew Valley" / "CoolMod"
    managed.mkdir(parents=True)
    _make_zip(
        managed / "CoolMod.zip",
        {
            "CoolMod/manifest.json": b'{"Name":"CoolMod","Version":"1.0.0"}',
            "CoolMod/CoolMod.dll": b"MZ",
        },
    )
    mid = "94007"
    pk = _register(
        db,
        mid=mid,
        path=str(managed),
        app_id=STARDEW_VALLEY_APP_ID,
        title="CoolMod",
        game_name="Stardew Valley",
    )
    _set_archive_entry(db, pk, "CoolMod.zip")

    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out["success"] is True, out
    assert (mods_dir / "CoolMod" / "manifest.json").is_file()
    assert (mods_dir / "CoolMod" / "CoolMod.dll").is_file()


# ---------------------------------------------------------------------------
# Case 8 — deploy failure restores backup
# ---------------------------------------------------------------------------


def test_case8_failure_rolls_back_backup(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mods = tmp_path / "GameMods"
    mods.mkdir()
    db.update_game_deploy_config(100, name="SomeGame", mod_path=str(mods))

    managed = library / "SomeGame" / "Rollback"
    managed.mkdir(parents=True)
    (managed / "file1.txt").write_text("NEW", encoding="utf-8")
    pk = _register(db, mid="94008", path=str(managed), title="Rollback")

    prior = mods / "Rollback" / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    with patch_apply_then_unlink_targets():
        out = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert out["success"] is False
    assert prior.read_text(encoding="utf-8") == "ORIGINAL"
    assert load_manifest(managed) is None
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED
    assert str(info.deploy_error or "").strip()
