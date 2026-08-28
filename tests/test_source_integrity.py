"""Mod Source Integrity layer — unified DB / disk / deploy source checks."""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import zipfile
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import FILE_TYPE_MAIN, ModFileEntry, ModFilesBundle
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_errors import DeploySourceError
from services.deploy_rules.manifest import load_manifest
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.mod_source_integrity import (
    META_CONTENT_HASH,
    reconcile_source,
    resolve_deploy_source,
    validate_archive_content,
    validate_source,
)

HISTORY = "历史版本"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "source_integrity.db")
    manager.upsert_game(GameInfo(app_id=100, name="Game", folder_name="Game"))
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _write_zip(path: Path, *, inner: str = "mod.txt", data: bytes = b"payload") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(inner, data)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup_mod(
    tmp_path: Path,
    db: DatabaseManager,
    *,
    mid: str = "97001",
    folder: str = "IntegrityMod",
    app_id: int = 100,
) -> Path:
    library = tmp_path / "library"
    mod = library / "Game" / folder
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": folder,
                "app_id": app_id,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id=mid,
            title=folder,
            app_id=app_id,
            game_name="Game",
        )
    )
    db.update_mod_identity_fields(
        mid,
        folder_present=True,
        last_known_path=str(mod),
        library_status="healthy",
    )
    db.update_game_deploy_config(
        app_id,
        mod_path=str(tmp_path / "game_mods"),
    )
    (tmp_path / "game_mods").mkdir(parents=True, exist_ok=True)
    return mod


def _entry(
    *,
    path: str,
    filename: str = "",
    metadata: dict | None = None,
    enabled: bool = True,
    selected_for_deploy: bool | None = None,
) -> ModFileEntry:
    meta = dict(metadata or {})
    return ModFileEntry(
        name=filename or Path(path).name,
        filename=filename or Path(path).name,
        path=path,
        type=FILE_TYPE_MAIN,
        enabled=enabled,
        selected_for_deploy=selected_for_deploy,
        metadata=meta,
    )


def _set_files(db: DatabaseManager, mid: str, entries: list[ModFileEntry]) -> None:
    db.set_mod_files(mid, ModFilesBundle(files=entries))


def test_case1_rename_archive_hash_match_auto_fixes(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod = _setup_mod(tmp_path, db, mid="97001", folder="Rename")
    _write_zip(mod / "renamed.zip", data=b"rename-payload")
    hist = mod / HISTORY
    hist.mkdir()
    shutil.copy2(mod / "renamed.zip", hist / "original.zip")

    _set_files(db, "97001", [_entry(path="original.zip")])

    result = reconcile_source("97001", managed_path=mod, db=db)
    assert result.updated
    assert result.auto_fixed == ["renamed.zip"]
    assert db.get_mod_files("97001").files[0].path == "renamed.zip"
    validate_source("97001", managed_path=mod, db=db)


def test_case2_move_archive_hash_match_auto_fixes(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod = _setup_mod(tmp_path, db, mid="97002", folder="Move")
    _write_zip(mod / "at-root.zip", data=b"move-payload")
    hist = mod / HISTORY
    hist.mkdir()
    shutil.copy2(mod / "at-root.zip", hist / "old.zip")

    _set_files(db, "97002", [_entry(path="subdir/old.zip", filename="old.zip")])

    result = reconcile_source("97002", managed_path=mod, db=db)
    assert result.updated
    assert result.auto_fixed == ["at-root.zip"]
    assert db.get_mod_files("97002").files[0].path == "at-root.zip"


def test_case3_changed_hash_updates_metadata(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod = _setup_mod(tmp_path, db, mid="97003", folder="Changed")
    _write_zip(mod / "mod.zip", data=b"version-one")
    old_hash = _sha256(mod / "mod.zip")
    entry = _entry(
        path="mod.zip",
        metadata={META_CONTENT_HASH: old_hash},
    )
    _set_files(db, "97003", [entry])

    _write_zip(mod / "mod.zip", data=b"version-two-changed")

    out = validate_source("97003", managed_path=mod, db=db)
    assert "mod.zip" in out.source_changed
    stored = db.get_mod_files("97003").files[0].metadata[META_CONTENT_HASH]
    assert stored == _sha256(mod / "mod.zip")


def test_case4_multiple_versions_user_selection_not_auto_pick(
    tmp_path: Path, db: DatabaseManager
) -> None:
    mod = _setup_mod(tmp_path, db, mid="97004", folder="Multi")
    _write_zip(mod / "v1.zip", data=b"version-one")
    time.sleep(0.02)
    _write_zip(mod / "v2.zip", data=b"version-two-bigger-not-auto")

    selected = _entry(path="v1.zip", selected_for_deploy=True)
    other = _entry(path="v2.zip", selected_for_deploy=False)
    _set_files(db, "97004", [selected, other])

    resolved = resolve_deploy_source("97004", managed_path=mod, db=db)
    assert len(resolved.archive_paths) == 1
    assert resolved.archive_paths[0].name == "v1.zip"

    _set_files(
        db,
        "97004",
        [
            _entry(path="missing.zip", selected_for_deploy=True),
            _entry(path="v2.zip", selected_for_deploy=False),
        ],
    )
    hist = mod / HISTORY
    hist.mkdir()
    _write_zip(hist / "missing.zip", data=b"db-reference")
    with pytest.raises(DeploySourceError) as exc:
        validate_source("97004", managed_path=mod, db=db)
    assert exc.value.code == "replacement_required"


def test_case5_empty_zip_rejected(tmp_path: Path, db: DatabaseManager) -> None:
    mod = _setup_mod(tmp_path, db, mid="97005", folder="EmptyZip")
    empty = mod / "empty.zip"
    with zipfile.ZipFile(empty, "w"):
        pass
    _set_files(db, "97005", [_entry(path="empty.zip")])

    with pytest.raises(DeploySourceError) as exc:
        validate_archive_content(empty)
    assert exc.value.code == "empty_archive"

    with pytest.raises(DeploySourceError) as exc2:
        validate_source("97005", managed_path=mod, db=db, auto_reconcile=False)
    assert exc2.value.code in {"empty_archive", "no_deployable_source", "missing_files"}


def test_case6_invalid_zip_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not-a-zip")
    with pytest.raises(DeploySourceError) as exc:
        validate_archive_content(bad)
    assert exc.value.code == "invalid_archive"


def test_case7_history_version_excluded(tmp_path: Path, db: DatabaseManager) -> None:
    mod = _setup_mod(tmp_path, db, mid="97007", folder="HistoryOnly")
    hist = mod / HISTORY
    hist.mkdir()
    _write_zip(hist / "old.zip", data=b"history-only")
    _set_files(db, "97007", [_entry(path="old.zip")])

    with pytest.raises(DeploySourceError):
        validate_source("97007", managed_path=mod, db=db, auto_reconcile=False)


def test_case8_import_cache_excluded(tmp_path: Path, db: DatabaseManager) -> None:
    mod = _setup_mod(tmp_path, db, mid="97008", folder="CacheOnly")
    cache = mod / "import_cache"
    cache.mkdir()
    _write_zip(cache / "cached.zip", data=b"cache-only")
    _set_files(db, "97008", [_entry(path="cached.zip")])

    result = reconcile_source("97008", managed_path=mod, db=db)
    assert "cached.zip" in result.missing_files
    assert not result.auto_fixed

    with pytest.raises(DeploySourceError):
        validate_source("97008", managed_path=mod, db=db, auto_reconcile=False)


def test_case9_direct_deploy_without_refresh(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mod = _setup_mod(tmp_path, db, mid="97009", folder="DirectDeploy")
    _write_zip(mod / "live.zip", inner="content/mod.txt", data=b"deploy-me")
    hist = mod / HISTORY
    hist.mkdir()
    shutil.copy2(mod / "live.zip", hist / "stale.zip")
    _set_files(db, "97009", [_entry(path="stale.zip")])

    assert db.get_mod_files("97009").files[0].path == "stale.zip"

    deployer = ModDeployer(library_root=library, db=db)
    out = deployer.deploy_mod("97009")
    assert out["success"] is True
    assert db.get_mod_files("97009").files[0].path == "live.zip"

    target_root = tmp_path / "game_mods"
    assert any(target_root.rglob("mod.txt"))


def test_case10_deployed_source_changed_hash_in_manifest(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    mod = _setup_mod(tmp_path, db, mid="97010", folder="ManifestHash")
    (mod / "payload.txt").write_text("hello", encoding="utf-8")
    _set_files(db, "97010", [])

    deployer = ModDeployer(library_root=library, db=db)
    first = deployer.deploy_mod("97010")
    assert first["success"] is True

    manifest = load_manifest(mod, expected_mod_id="97010")
    assert manifest is not None
    assert manifest.files
    assert manifest.files[0].source_hash

    (mod / "payload.txt").write_text("changed", encoding="utf-8")
    out = validate_source("97010", managed_path=mod, db=db)
    assert out.ok
