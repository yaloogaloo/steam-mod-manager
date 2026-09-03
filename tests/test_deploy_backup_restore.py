"""Safe overwrite backup + restore on undeploy."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core.db_manager import (
    DEPLOY_TYPE_FOLDER_COPY,
    DatabaseManager,
)
from core.models import ModMetadata
from services.backup_manager import BACKUPS_DIRNAME, BackupManager
from services.conflict import ConflictDetector, ConflictType
from services.deploy import ModDeployer
from services.deploy_rules import load_manifest, save_manifest
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestBackupInfo,
    ManifestFileEntry,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "backup_restore.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _write_meta(mod_dir: Path, *, mid: str, title: str, app_id: int, game: str) -> None:
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": title,
                "app_id": app_id,
                "game_name": game,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _setup_game(db: DatabaseManager, tmp_path: Path, *, app_id: int = 4242) -> Path:
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    db.update_game_deploy_config(
        app_id,
        name="SomeGame",
        mod_path=str(mods_root),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    return mods_root


def _add_mod(
    library: Path,
    db: DatabaseManager,
    *,
    mid: str,
    title: str,
    app_id: int = 4242,
    files: dict[str, str] | None = None,
) -> Path:
    mod = library / "SomeGame" / title
    mod.mkdir(parents=True)
    payload = files or {"a.txt": "MOD-A"}
    for rel, text in payload.items():
        path = mod / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _write_meta(mod, mid=mid, title=title, app_id=app_id, game="SomeGame")
    db.upsert_mod(ModMetadata(published_file_id=mid, title=title, app_id=app_id))
    return mod


def test_case1_no_prior_file_undeploy_deletes(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    source = _add_mod(library, db, mid="92001", title="FreshMod")

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("92001")["success"] is True

    target = mods_root / "FreshMod" / "a.txt"
    assert target.read_text(encoding="utf-8") == "MOD-A"
    manifest = load_manifest(source)
    assert manifest is not None
    assert all(f.backup is None for f in manifest.files)

    und = deployer.undeploy_mod("92001")
    assert und["success"] is True
    assert not target.exists()
    assert not (source / INFO_DIR_NAME / BACKUPS_DIRNAME).exists()


def test_case2_restore_game_original(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    source = _add_mod(library, db, mid="92002", title="OverwriteMe")

    # Pre-existing game file at the deploy target
    prior = mods_root / "OverwriteMe" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("GAME-ORIGINAL", encoding="utf-8")

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("92002")["success"] is True
    assert prior.read_text(encoding="utf-8") == "MOD-A"

    manifest = load_manifest(source)
    assert manifest is not None
    backed = [f for f in manifest.files if f.backup is not None]
    assert len(backed) == 1
    assert Path(source / backed[0].backup.path).is_file()  # type: ignore[union-attr]
    assert (source / INFO_DIR_NAME / BACKUPS_DIRNAME).is_dir()

    und = deployer.undeploy_mod("92002")
    assert und["success"] is True
    assert prior.read_text(encoding="utf-8") == "GAME-ORIGINAL"
    assert not (source / INFO_DIR_NAME / BACKUPS_DIRNAME).exists()


def test_case3_multi_file_restore(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    source = _add_mod(
        library,
        db,
        mid="92003",
        title="Multi",
        files={"a.txt": "MA", "sub/b.txt": "MB"},
    )

    (mods_root / "Multi").mkdir(parents=True)
    (mods_root / "Multi" / "a.txt").write_text("GA", encoding="utf-8")
    (mods_root / "Multi" / "sub").mkdir(parents=True)
    (mods_root / "Multi" / "sub" / "b.txt").write_text("GB", encoding="utf-8")

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("92003")["success"] is True
    assert (mods_root / "Multi" / "a.txt").read_text(encoding="utf-8") == "MA"
    assert (mods_root / "Multi" / "sub" / "b.txt").read_text(encoding="utf-8") == "MB"

    manifest = load_manifest(source)
    assert manifest is not None
    assert sum(1 for f in manifest.files if f.backup is not None) == 2

    assert deployer.undeploy_mod("92003")["success"] is True
    assert (mods_root / "Multi" / "a.txt").read_text(encoding="utf-8") == "GA"
    assert (mods_root / "Multi" / "sub" / "b.txt").read_text(encoding="utf-8") == "GB"


def test_case4_deploy_failure_auto_restore(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    _add_mod(library, db, mid="92004", title="FailMod")

    prior = mods_root / "FailMod" / "a.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("KEEP-ME", encoding="utf-8")

    deployer = ModDeployer(library_root=library, db=db)

    def boom(self, ctx):  # noqa: ANN001
        # Simulate partial overwrite then hard failure
        target = mods_root / "FailMod" / "a.txt"
        target.write_text("PARTIAL-MOD", encoding="utf-8")
        raise RuntimeError("simulated deploy failure")

    with patch(
        "services.deploy_rules.generic.FolderCopyStrategy.deploy",
        boom,
    ):
        with pytest.raises(RuntimeError, match="simulated deploy failure"):
            deployer.deploy_mod("92004")

    assert prior.read_text(encoding="utf-8") == "KEEP-ME"


def test_case5_legacy_manifest_without_backup(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    source = _add_mod(library, db, mid="92005", title="Legacy")

    target = mods_root / "Legacy" / "a.txt"
    target.parent.mkdir(parents=True)
    target.write_text("MOD-DEPLOYED", encoding="utf-8")

    # Old-style manifest: no backup field
    legacy = DeployManifest(
        mod_id="92005",
        deploy_time="2020-01-01T00:00:00+00:00",
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        files=[
            ManifestFileEntry(
                source=str(source / "a.txt"),
                target=str(target.resolve()),
            )
        ],
    )
    # Force-write without backup keys
    path = source / INFO_DIR_NAME / "deploy_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mod_id": "92005",
                "deploy_time": legacy.deploy_time,
                "deploy_type": DEPLOY_TYPE_FOLDER_COPY,
                "files": [{"source": legacy.files[0].source, "target": legacy.files[0].target}],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    db.update_mod_deploy_status(
        "92005",
        deploy_status="deployed",
        deploy_path=str(mods_root / "Legacy"),
        deploy_time=legacy.deploy_time,
        deploy_error="",
        app_id=4242,
    )

    loaded = load_manifest(source)
    assert loaded is not None
    assert loaded.files[0].backup is None

    und = ModDeployer(library_root=library, db=db).undeploy_mod("92005")
    assert und["success"] is True
    assert not target.exists()


def test_case6_two_mods_same_target_conflict_unchanged(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = _setup_game(db, tmp_path)
    a = _add_mod(library, db, mid="92006", title="ModA", files={"shared.txt": "A"})
    b = _add_mod(library, db, mid="92007", title="ModB", files={"shared.txt": "B"})

    # Plant matching absolute targets in both manifests (simulates shared claim)
    shared = (mods_root / "shared.txt").resolve()
    shared.write_text("GAME", encoding="utf-8")

    for mid, mod, content in (
        ("92006", a, "A"),
        ("92007", b, "B"),
    ):
        save_manifest(
            mod,
            DeployManifest(
                mod_id=mid,
                deploy_time="2020-01-01T00:00:00+00:00",
                deploy_type=DEPLOY_TYPE_FOLDER_COPY,
                files=[
                    ManifestFileEntry(
                        source=str(mod / "shared.txt"),
                        target=str(shared),
                        backup=ManifestBackupInfo(
                            path=str(mod / INFO_DIR_NAME / BACKUPS_DIRNAME / "x.original"),
                            hash="abc",
                            created_at="2020-01-01T00:00:00+00:00",
                        )
                        if mid == "92006"
                        else None,
                    )
                ],
            ),
        )
        # Ensure backup file exists for ModA so restore path is valid if used
        if mid == "92006":
            bak = mod / INFO_DIR_NAME / BACKUPS_DIRNAME
            bak.mkdir(parents=True, exist_ok=True)
            (bak / "x.original").write_text("GAME", encoding="utf-8")

    report_map = ConflictDetector(library, db=db).check_all_mods(persist=False)
    overwrite = []
    for report in report_map.values():
        overwrite.extend(
            [
                c
                for c in report.conflicts
                if c.conflict_type == ConflictType.FILE_OVERWRITE.value
            ]
        )
    assert overwrite
    assert sorted(overwrite[0].mods) == ["92006", "92007"]

    # Preview still reports FILE_OVERWRITE diagnostic; deploy path remains warn-only
    preview = ConflictDetector(library, db=db).preview_targets(
        "92007", [str(shared)]
    )
    assert preview.status == "none"
    assert preview.conflicts
    assert preview.conflicts[0].conflict_type == ConflictType.FILE_OVERWRITE.value


def test_backup_manager_unique_names(tmp_path: Path) -> None:
    managed = tmp_path / "modfolder"
    managed.mkdir()
    target = tmp_path / "game" / "foo.dll"
    target.parent.mkdir(parents=True)
    target.write_text("v1", encoding="utf-8")

    mgr = BackupManager(managed)
    prep1 = mgr.prepare_overwrite([target])
    info1 = prep1.by_target[str(target.resolve())]
    assert info1 is not None
    p1 = mgr.resolve_backup_file(info1)
    # Second backup of same path must not clobber the first file
    target.write_text("v2", encoding="utf-8")
    prep2 = mgr.prepare_overwrite([target])
    info2 = prep2.by_target[str(target.resolve())]
    assert info2 is not None
    p2 = mgr.resolve_backup_file(info2)
    assert p1 != p2
    assert p1.is_file() and p2.is_file()
    assert p1.read_text(encoding="utf-8") == "v1"
    assert p2.read_text(encoding="utf-8") == "v2"
