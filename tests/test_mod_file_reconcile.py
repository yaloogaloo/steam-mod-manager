"""Mod file index reconciliation — DB vs managed folder."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from core.game_info import GameInfo
from core.db_manager import DatabaseManager
from core.mod_platform import FILE_TYPE_MAIN, ModFileEntry, ModFilesBundle
from services.file_ops import is_missing_mod_content
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
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
) -> tuple[Path, str]:
    library = tmp_path / "library"
    mod = library / "Game" / folder
    mod.mkdir(parents=True, exist_ok=True)
    db.upsert_game(GameInfo(app_id=100, name="Game", folder_name="Game"))
    created = create_steam_test_mod(
        db, external_id=mid, title=folder, app_id=100, game_name="Game"
    )
    pk = str(created.mod_id)
    prove_managed_folder(
        db, mod, handle=pk, title=folder, app_id=100, game_name="Game"
    )
    db.update_mod_identity_fields(
        pk,
        folder_present=True,
        last_known_path=str(mod),
    )
    db.update_mod_content_status(
        pk, content_status="healthy", library_status="healthy"
    )
    return mod, pk


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
    mod, pk = _setup_mod(tmp_path, db)
    _write_zip(mod / "pack.zip")
    _set_files(db, pk, [_entry(path="pack.zip")])

    missing = validate_mod_files(pk, managed_path=mod, db=db)
    assert missing == []

    result = reconcile_archive_source(pk, managed_path=mod, db=db)
    assert not result.updated
    assert result.auto_fixed == []
    assert db.get_mod_files(pk).files[0].path == "pack.zip"


def test_case2_zip_rename_hash_match_auto_fixes(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod, pk = _setup_mod(tmp_path, db, mid="96002", folder="RenameMod")
    _write_zip(mod / "new-name.zip", data=b"same-bytes-for-rename-test")
    hist = mod / "历史版本"
    hist.mkdir()
    shutil.copy2(mod / "new-name.zip", hist / "old-name.zip")

    _set_files(db, pk, [_entry(path="old-name.zip")])

    assert validate_mod_files(pk, managed_path=mod, db=db) == ["old-name.zip"]

    result = reconcile_archive_source(pk, managed_path=mod, db=db)
    assert result.updated
    assert result.auto_fixed == ["new-name.zip"]
    assert not result.replacement_candidates

    bundle = db.get_mod_files(pk)
    assert bundle.files[0].path == "new-name.zip"
    assert bundle.files[0].filename == "new-name.zip"
    assert validate_mod_files(pk, managed_path=mod, db=db) == []


def test_case3_hash_mismatch_yields_candidate_no_auto_replace(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod, pk = _setup_mod(tmp_path, db, mid="96003", folder="VersionMod")
    _write_zip(mod / "new-version.zip", data=b"version-two")
    hist = mod / "历史版本"
    hist.mkdir()
    _write_zip(hist / "old-version.zip", data=b"version-one")

    _set_files(db, pk, [_entry(path="old-version.zip")])

    result = reconcile_archive_source(pk, managed_path=mod, db=db)
    assert not result.updated
    assert result.auto_fixed == []
    assert len(result.replacement_candidates) == 1
    assert result.replacement_candidates[0].candidate_path == "new-version.zip"
    assert result.replacement_candidates[0].reference_hash != (
        result.replacement_candidates[0].candidate_hash
    )
    assert db.get_mod_files(pk).files[0].path == "old-version.zip"


def test_case4_history_zip_not_used_as_source(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod, pk = _setup_mod(tmp_path, db, mid="96004", folder="HistOnly")
    hist = mod / "历史版本"
    hist.mkdir()
    _write_zip(hist / "archived.zip", data=b"only-in-history")

    _set_files(db, pk, [_entry(path="archived.zip")])

    result = reconcile_archive_source(pk, managed_path=mod, db=db)
    assert not result.updated
    assert result.auto_fixed == []
    assert not result.replacement_candidates
    assert not has_deployable_source(mod, mod_id=pk, db=db)
    assert is_missing_mod_content(mod, mod_id=pk)


def test_case5_metadata_only_not_deployable(tmp_path: Path, db: DatabaseManager) -> None:
    mod, pk = _setup_mod(tmp_path, db, mid="96005", folder="MetaOnly")
    _set_files(db, pk, [_entry(path="ghost.zip")])

    assert not has_deployable_source(mod, mod_id=pk, db=db)
    assert is_missing_mod_content(mod, mod_id=pk)
    assert validate_mod_files(pk, managed_path=mod, db=db) == ["ghost.zip"]
