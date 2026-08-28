"""End-to-end deploy pipeline integrity (source → extract → validate → result)."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import FILE_TYPE_MAIN, ModFileEntry, ModFilesBundle
from services.deploy import ModDeployer
from services.deploy_rules.generic import FolderCopyStrategy
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry, load_manifest
from services.deploy_rules.stardew_valley import STARDEW_VALLEY_APP_ID
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_status import CONTENT_HEALTHY


BG3_APP_ID = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "pipeline_integrity.db")
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


def _write_meta(mod_dir: Path, *, mid: str, title: str, app_id: int) -> None:
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "{title}",\n'
        f'  "app_id": {app_id}\n'
        "}\n",
        encoding="utf-8",
    )


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
) -> None:
    db.upsert_mod(
        ModMetadata(
            published_file_id=mid,
            title=title,
            app_id=app_id,
            game_name="SomeGame",
        )
    )
    db.update_mod_identity_fields(
        mid,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=path,
        library_status=CONTENT_HEALTHY,
    )


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
    _write_meta(managed, mid="94001", title="ZipMod", app_id=100)
    _register(db, mid="94001", path=str(managed), title="ZipMod")
    _set_archive_entry(db, "94001", "pack.zip")

    out = ModDeployer(library_root=library, db=db).deploy_mod("94001")
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
    _write_meta(managed, mid="94002", title="LooseMod", app_id=100)
    _register(db, mid="94002", path=str(managed), title="LooseMod")
    # DB still lists a zip that is no longer on disk (already extracted).
    _set_archive_entry(db, "94002", "WASD-781-1-9-8-1758653752.zip")

    out = ModDeployer(library_root=library, db=db).deploy_mod("94002")
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
    _write_meta(managed, mid="94003", title="ZipOnly", app_id=100)
    _register(db, mid="94003", path=str(managed), title="ZipOnly")
    _set_archive_entry(db, "94003", "WASD-781-1-9-8-1758653752.zip")

    out = ModDeployer(library_root=library, db=db).deploy_mod("94003")
    assert out["success"] is False, out
    assert "files" not in out or not out.get("files")
    assert load_manifest(managed) is None
    info = db.get_mod_deploy_info("94003")
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
    _write_meta(managed, mid="94004", title="CopyFail", app_id=100)
    _register(db, mid="94004", path=str(managed), title="CopyFail")

    with patch(
        "services.deploy_rules.generic.shutil.copytree",
        side_effect=OSError("simulated copy failure"),
    ):
        out = ModDeployer(library_root=library, db=db).deploy_mod("94004")

    assert out["success"] is False, out
    assert load_manifest(managed) is None
    info = db.get_mod_deploy_info("94004")
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
    _write_meta(managed, mid="94005", title="Ghost", app_id=100)
    _register(db, mid="94005", path=str(managed), title="Ghost")

    ghost = mods / "Ghost" / "missing.txt"

    def _lie(self: FolderCopyStrategy, ctx: object):
        from services.deploy_rules.base import StrategyResult

        entries = [
            ManifestFileEntry(
                source=str(managed / "a.txt"),
                target=str(ghost),
                type="folder_copy",
            )
        ]
        manifest = DeployManifest(
            mod_id="94005",
            deploy_time="2026-01-01T00:00:00+00:00",
            deploy_type="folder_copy",
            files=entries,
        )
        return StrategyResult(
            success=True,
            target=str(mods / "Ghost"),
            copied_files=1,
            deploy_type="folder_copy",
            deploy_time=manifest.deploy_time,
            files=entries,
            manifest=manifest,
        )

    with patch.object(FolderCopyStrategy, "deploy", _lie):
        out = ModDeployer(library_root=library, db=db).deploy_mod("94005")

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
    _write_meta(managed, mid="94006", title="NativeLoader", app_id=BG3_APP_ID)
    _register(
        db, mid="94006", path=str(managed), app_id=BG3_APP_ID, title="NativeLoader"
    )
    _set_archive_entry(db, "94006", "loader.zip")
    db.update_mod_user_metadata(
        94006,
        {
            "display_name": "NativeLoader",
            "custom_description": "",
            "user_notes": "",
            "favorite": False,
            "custom_deploy_path": str(game_root),
        },
    )

    out = ModDeployer(library_root=library, db=db).deploy_mod("94006")
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
    _write_meta(
        managed, mid=mid, title="CoolMod", app_id=STARDEW_VALLEY_APP_ID
    )
    _register(
        db,
        mid=mid,
        path=str(managed),
        app_id=STARDEW_VALLEY_APP_ID,
        title="CoolMod",
    )
    _set_archive_entry(db, mid, "CoolMod.zip")

    out = ModDeployer(library_root=library, db=db).deploy_mod(mid)
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
    _write_meta(managed, mid="94008", title="Rollback", app_id=100)
    _register(db, mid="94008", path=str(managed), title="Rollback")

    prior = mods / "Rollback" / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    real_deploy = FolderCopyStrategy.deploy

    def _deploy_then_delete(self: FolderCopyStrategy, ctx: object):
        real = real_deploy(self, ctx)
        assert real.success and real.manifest is not None
        for entry in real.manifest.files:
            Path(entry.target).unlink(missing_ok=True)
        return real

    with patch.object(FolderCopyStrategy, "deploy", _deploy_then_delete):
        out = ModDeployer(library_root=library, db=db).deploy_mod("94008")

    assert out["success"] is False
    assert prior.read_text(encoding="utf-8") == "ORIGINAL"
    assert load_manifest(managed) is None
    info = db.get_mod_deploy_info("94008")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_NOT_DEPLOYED
