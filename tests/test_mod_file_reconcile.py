"""Mod file index reconciliation — DB vs managed folder."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from core.game_info import GameInfo
from core.db_manager import DatabaseManager
from core.mod_platform import FILE_TYPE_MAIN, ModFileEntry, ModFilesBundle
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, is_missing_mod_content
from services.mod_file_reconciler import (
    has_deployable_source,
    reconcile_archive_source,
    validate_mod_files,
)


def _write_zip(path: Path, *, inner: str = "mod.txt", data: bytes = b"payload") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(inner, data)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "reconcile.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _setup_mod(
    tmp_path: Path,
    db: DatabaseManager,
    *,
    mid: str = "96001",
    folder: str = "ReconMod",
) -> Path:
    library = tmp_path / "library"
    mod = library / "Game" / folder
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "{folder}",\n'
        '  "app_id": 100\n'
        "}\n",
        encoding="utf-8",
    )
    db.upsert_game(GameInfo(app_id=100, name="Game", folder_name="Game"))
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title=folder, app_id=100, game_name="Game")
    )
    db.update_mod_identity_fields(
        mid,
        folder_present=True,
        last_known_path=str(mod),
        library_status="healthy",
    )
    return mod


def _set_files(db: DatabaseManager, mid: str, entries: list[ModFileEntry]) -> None:
    db.set_mod_files(mid, ModFilesBundle(files=entries))


def _entry(
    *,
    path: str,
    filename: str = "",
    metadata: dict | None = None,
) -> ModFileEntry:
    return ModFileEntry(
        name=filename or Path(path).name,
        filename=filename or Path(path).name,
        path=path,
        type=FILE_TYPE_MAIN,
        enabled=True,
        metadata=dict(metadata or {}),
    )


def test_case1_db_path_exists_unchanged(tmp_path: Path, db: DatabaseManager) -> None:
    mod = _setup_mod(tmp_path, db)
    _write_zip(mod / "pack.zip")
    _set_files(
        db,
        "96001",
        [_entry(path="pack.zip")],
    )

    missing = validate_mod_files("96001", managed_path=mod, db=db)
    assert missing == []

    result = reconcile_archive_source("96001", managed_path=mod, db=db)
    assert not result.updated
    assert result.auto_fixed == []
    assert db.get_mod_files("96001").files[0].path == "pack.zip"


def test_case2_zip_rename_hash_match_auto_fixes(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod = _setup_mod(tmp_path, db, mid="96002", folder="RenameMod")
    _write_zip(mod / "new-name.zip", data=b"same-bytes-for-rename-test")
    hist = mod / "历史版本"
    hist.mkdir()
    shutil.copy2(mod / "new-name.zip", hist / "old-name.zip")

    _set_files(
        db,
        "96002",
        [_entry(path="old-name.zip")],
    )

    assert validate_mod_files("96002", managed_path=mod, db=db) == ["old-name.zip"]

    result = reconcile_archive_source("96002", managed_path=mod, db=db)
    assert result.updated
    assert result.auto_fixed == ["new-name.zip"]
    assert not result.replacement_candidates

    bundle = db.get_mod_files("96002")
    assert bundle.files[0].path == "new-name.zip"
    assert bundle.files[0].filename == "new-name.zip"
    assert validate_mod_files("96002", managed_path=mod, db=db) == []


def test_case3_hash_mismatch_yields_candidate_no_auto_replace(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod = _setup_mod(tmp_path, db, mid="96003", folder="VersionMod")
    _write_zip(mod / "new-version.zip", data=b"version-two")
    hist = mod / "历史版本"
    hist.mkdir()
    _write_zip(hist / "old-version.zip", data=b"version-one")

    _set_files(
        db,
        "96003",
        [_entry(path="old-version.zip")],
    )

    result = reconcile_archive_source("96003", managed_path=mod, db=db)
    assert not result.updated
    assert result.auto_fixed == []
    assert len(result.replacement_candidates) == 1
    assert result.replacement_candidates[0].candidate_path == "new-version.zip"
    assert result.replacement_candidates[0].reference_hash != (
        result.replacement_candidates[0].candidate_hash
    )
    assert db.get_mod_files("96003").files[0].path == "old-version.zip"


def test_case4_history_zip_not_used_as_source(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod = _setup_mod(tmp_path, db, mid="96004", folder="HistOnly")
    hist = mod / "历史版本"
    hist.mkdir()
    _write_zip(hist / "archived.zip", data=b"only-in-history")

    _set_files(
        db,
        "96004",
        [_entry(path="archived.zip")],
    )

    result = reconcile_archive_source("96004", managed_path=mod, db=db)
    assert not result.updated
    assert result.auto_fixed == []
    assert not result.replacement_candidates
    assert not has_deployable_source(mod, mod_id="96004", db=db)
    assert is_missing_mod_content(mod, mod_id="96004")


def test_case5_metadata_only_not_deployable(tmp_path: Path, db: DatabaseManager) -> None:
    mod = _setup_mod(tmp_path, db, mid="96005", folder="MetaOnly")
    _set_files(db, "96005", [_entry(path="ghost.zip")])

    assert not has_deployable_source(mod, mod_id="96005", db=db)
    assert is_missing_mod_content(mod, mod_id="96005")
    assert validate_mod_files("96005", managed_path=mod, db=db) == ["ghost.zip"]
