"""DeployResult file-level detail recording (runtime only; not manifest)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder
from tests.helpers.deploy import patch_apply_then_unlink_targets

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from services.deploy import ModDeployer, build_deploy_result_files
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry, load_manifest
from services.library_status import CONTENT_HEALTHY


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "deploy_result_detail.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _make_mod(
    library: Path,
    db: DatabaseManager,
    *,
    game: str = "Game",
    folder: str = "DetailMod",
    mod_id: str = "93001",
    app_id: int = 424242,
    files: dict[str, str] | None = None,
) -> tuple[Path, str]:
    mod_dir = library / game / folder
    mod_dir.mkdir(parents=True, exist_ok=True)
    payload = files if files is not None else {"file1.txt": "one"}
    for rel, text in payload.items():
        path = mod_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    created = create_steam_test_mod(
        db, external_id=mod_id, title=folder, app_id=app_id, game_name=game
    )
    pk = str(created.mod_id)
    prove_managed_folder(
        db, mod_dir, handle=pk, title=folder, app_id=app_id, game_name=game
    )
    db.update_mod_content_status(
        pk, content_status=CONTENT_HEALTHY, library_status=CONTENT_HEALTHY
    )
    return mod_dir, pk


def _setup_game(
    db: DatabaseManager,
    tmp_path: Path,
    *,
    app_id: int = 424242,
) -> tuple[Path, Path]:
    install = tmp_path / "fake_game"
    mods = install / "mods"
    mods.mkdir(parents=True)
    db.update_game_deploy_config(
        app_id,
        name="Game",
        install_path=str(install),
        mod_path=str(mods),
        deploy_type="folder_copy",
    )
    return install, mods


def test_case1_single_file_deploy_records_detail(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    _install, mods = _setup_game(db, tmp_path)
    _mod_dir, pk = _make_mod(library, db, mod_id="93001", files={"only.txt": "hello"})

    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out["success"] is True
    assert out["validated"] == 1
    files = out.get("files") or []
    assert len(files) == 1
    entry = files[0]
    assert Path(entry["target"]).name == "only.txt"
    assert entry["size"] == len("hello".encode("utf-8"))
    assert entry["source"]
    # Runtime detail omits hash by default (large-deploy perf); size/path remain.
    assert "hash" not in entry
    assert (mods / "DetailMod" / "only.txt").read_text(encoding="utf-8") == "hello"


def test_case2_multi_file_deploy_records_all(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    _install, mods = _setup_game(db, tmp_path)
    _mod_dir, pk = _make_mod(
        library,
        db,
        mod_id="93002",
        folder="MultiMod",
        files={"a.txt": "A", "sub/b.txt": "BB"},
    )

    out = ModDeployer(library_root=library, db=db).deploy_mod(pk)
    assert out["success"] is True
    assert out["validated"] == 2
    files = out.get("files") or []
    assert len(files) == 2
    by_name = {Path(f["target"]).name: f for f in files}
    assert set(by_name) == {"a.txt", "b.txt"}
    assert by_name["a.txt"]["size"] == 1
    assert by_name["b.txt"]["size"] == 2
    assert all("hash" not in f for f in files)
    assert (mods / "MultiMod" / "a.txt").is_file()
    assert (mods / "MultiMod" / "sub" / "b.txt").is_file()


def test_case3_validation_failure_has_no_result_files(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    _install, mods = _setup_game(db, tmp_path)
    mod_dir, pk = _make_mod(library, db, mod_id="93003")

    with patch_apply_then_unlink_targets():
        out = ModDeployer(library_root=library, db=db).deploy_mod(pk)

    assert out["success"] is False
    assert out.get("reason") == "missing_targets"
    assert "files" not in out
    assert load_manifest(mod_dir) is None
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED
    assert str(info.deploy_error or "").strip()


def test_case4_backup_restore_unaffected_by_result_detail(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    _install, mods = _setup_game(db, tmp_path)
    _mod_dir, pk = _make_mod(
        library,
        db,
        mod_id="93004",
        files={"file1.txt": "NEW"},
    )

    prior = mods / "DetailMod" / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    deployer = ModDeployer(library_root=library, db=db)
    out = deployer.deploy_mod(pk)
    assert out["success"] is True
    assert len(out.get("files") or []) == 1
    assert prior.read_text(encoding="utf-8") == "NEW"
    info = db.get_mod_deploy_info(pk)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED

    und = deployer.undeploy_mod(pk)
    assert und["success"] is True
    assert prior.read_text(encoding="utf-8") == "ORIGINAL"
    info2 = db.get_mod_deploy_info(pk)
    assert info2 is not None
    assert info2.deploy_status == DEPLOY_STATUS_NOT_DEPLOYED


def test_build_deploy_result_files_from_manifest(tmp_path: Path) -> None:
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc")
    manifest = DeployManifest(
        mod_id="1",
        deploy_time="t",
        deploy_type="folder_copy",
        files=[
            ManifestFileEntry(source=str(f), target=str(f), type="folder_copy"),
        ],
    )
    details = build_deploy_result_files(manifest)
    assert len(details) == 1
    assert details[0]["size"] == 3
    assert "hash" not in details[0]
    assert details[0]["target"] == str(f)
    hashed = build_deploy_result_files(manifest, include_hash=True)
    assert hashed[0].get("hash")
