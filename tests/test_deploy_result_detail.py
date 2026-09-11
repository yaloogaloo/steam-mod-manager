"""DeployResult file-level detail recording (runtime only; not manifest)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.identity import (
    bind_managed_path,
    create_steam_test_mod,
    write_info_sidecar,
)
from tests.helpers.deploy import patch_apply_then_unlink_targets

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.models import ModMetadata
from services.deploy import ModDeployer, build_deploy_result_files
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry, load_manifest
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_status import CONTENT_HEALTHY


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_result_detail.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _make_mod(
    library: Path,
    *,
    game: str = "Game",
    folder: str = "DetailMod",
    mod_id: str = "93001",
    app_id: int = 424242,
    files: dict[str, str] | None = None,
) -> Path:
    mod_dir = library / game / folder
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True)
    payload = files if files is not None else {"file1.txt": "one"}
    for rel, text in payload.items():
        path = mod_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mod_id}",\n'
        f'  "title": "{folder}",\n'
        f'  "app_id": {app_id},\n'
        f'  "game_name": "{game}"\n'
        "}\n",
        encoding="utf-8",
    )
    return mod_dir


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


def _register_mod(
    db: DatabaseManager,
    *,
    mod_id: str,
    path: str,
    app_id: int = 424242,
) -> None:
    created = create_steam_test_mod(
        db, external_id=mod_id, title="DetailMod", app_id=app_id, game_name="Game"
    )
    folder = Path(path)
    write_info_sidecar(
        folder,
        internal_id=str(created.mod_id),
        title="DetailMod",
        external_id=str(mod_id),
        workspace_id=str(created.workspace_id or mod_id),
        app_id=app_id,
        game_name="Game",
    )
    bind_managed_path(db, created.mod_id, folder, title="DetailMod", game_name="Game")
    db.update_mod_content_status(
        mod_id, content_status=CONTENT_HEALTHY, library_status=CONTENT_HEALTHY
    )


def test_case1_single_file_deploy_records_detail(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    _install, mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(library, mod_id="93001", files={"only.txt": "hello"})
    _register_mod(db, mod_id="93001", path=str(mod_dir))

    out = ModDeployer(library_root=library, db=db).deploy_mod("93001")
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
    mod_dir = _make_mod(
        library,
        mod_id="93002",
        folder="MultiMod",
        files={"a.txt": "A", "sub/b.txt": "BB"},
    )
    _register_mod(db, mod_id="93002", path=str(mod_dir))

    out = ModDeployer(library_root=library, db=db).deploy_mod("93002")
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
    mod_dir = _make_mod(library, mod_id="93003")
    _register_mod(db, mod_id="93003", path=str(mod_dir))

    with patch_apply_then_unlink_targets():
        out = ModDeployer(library_root=library, db=db).deploy_mod("93003")

    assert out["success"] is False
    assert out.get("reason") == "missing_targets"
    assert "files" not in out
    assert load_manifest(mod_dir) is None
    info = db.get_mod_deploy_info("93003")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED
    assert str(info.deploy_error or "").strip()


def test_case4_backup_restore_unaffected_by_result_detail(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "library"
    _install, mods = _setup_game(db, tmp_path)
    mod_dir = _make_mod(
        library,
        mod_id="93004",
        files={"file1.txt": "NEW"},
    )
    _register_mod(db, mod_id="93004", path=str(mod_dir))

    prior = mods / "DetailMod" / "file1.txt"
    prior.parent.mkdir(parents=True)
    prior.write_text("ORIGINAL", encoding="utf-8")

    deployer = ModDeployer(library_root=library, db=db)
    out = deployer.deploy_mod("93004")
    assert out["success"] is True
    assert len(out.get("files") or []) == 1
    assert prior.read_text(encoding="utf-8") == "NEW"
    info = db.get_mod_deploy_info("93004")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED

    und = deployer.undeploy_mod("93004")
    assert und["success"] is True
    assert prior.read_text(encoding="utf-8") == "ORIGINAL"
    info2 = db.get_mod_deploy_info("93004")
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
