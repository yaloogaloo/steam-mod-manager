"""Deploy result integrity validation and custom-path layout protection."""



from __future__ import annotations



from pathlib import Path

from unittest.mock import patch



import pytest

from tests.helpers.identity import bind_managed_path, create_steam_test_mod, write_info_sidecar
from tests.helpers.deploy import patch_apply_then_unlink_targets


from core.db_manager import (

    DEPLOY_STATUS_DEPLOYED,

    DEPLOY_STATUS_FAILED,

    DEPLOY_STATUS_NOT_DEPLOYED,

    DatabaseManager,

)

from core.models import ModMetadata

from services.deploy import (

    ModDeployer,

    _choose_archive_extract_root,

    validate_deploy_result,

)

from services.deploy_errors import DeployValidationError

from services.deploy_rules.base import StrategyResult

from services.deploy_rules.generic import FolderCopyStrategy

from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry, load_manifest

from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME

from services.library_status import CONTENT_HEALTHY





@pytest.fixture()

def db(tmp_path: Path) -> DatabaseManager:

    DatabaseManager.reset_instance()

    manager = DatabaseManager(tmp_path / "deploy_validation.db")

    yield manager

    manager.close()

    DatabaseManager.reset_instance()





def _make_mod(

    library: Path,

    *,

    game: str = "Game",

    folder: str = "TestMod",

    mod_id: str = "92001",

    app_id: int = 424242,

) -> Path:

    mod_dir = library / game / folder

    info = mod_dir / INFO_DIR_NAME

    info.mkdir(parents=True)

    (mod_dir / "file1.txt").write_text("one", encoding="utf-8")

    (mod_dir / "file2.txt").write_text("two", encoding="utf-8")

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
    mod_id: str = "92001",
    app_id: int = 424242,
    path: str = "",
) -> None:
    created = create_steam_test_mod(
        db, external_id=mod_id, title="TestMod", app_id=app_id, game_name="Game"
    )
    if path:
        folder = Path(path)
        write_info_sidecar(
            folder,
            internal_id=str(created.mod_id),
            title="TestMod",
            external_id=str(mod_id),
            workspace_id=str(created.workspace_id or mod_id),
            app_id=app_id,
            game_name="Game",
        )
        bind_managed_path(db, created.mod_id, folder, title="TestMod", game_name="Game")
    db.update_mod_content_status(
        mod_id, content_status=CONTENT_HEALTHY, library_status=CONTENT_HEALTHY
    )





def _fake_success_with_targets(

    targets: list[Path],

    *,

    mod_id: str = "92001",

    deploy_type: str = "folder_copy",

    target_root: str = "",

) -> StrategyResult:

    entries = [

        ManifestFileEntry(

            source=str(t),

            target=str(t),

            type=deploy_type,

        )

        for t in targets

    ]

    manifest = DeployManifest(

        mod_id=mod_id,

        deploy_time="2026-01-01T00:00:00+00:00",

        deploy_type=deploy_type,

        files=entries,

    )

    return StrategyResult(

        success=True,

        target=target_root or (str(targets[0].parent) if targets else ""),

        copied_files=len(entries),

        deploy_type=deploy_type,

        deploy_time=manifest.deploy_time,

        files=entries,

        manifest=manifest,

    )





# ---------------------------------------------------------------------------

# Case 1 — normal deploy, targets exist → success

# ---------------------------------------------------------------------------





def test_case1_normal_deploy_targets_exist_success(

    tmp_path: Path, db: DatabaseManager

) -> None:

    library = tmp_path / "library"

    _install, mods = _setup_game(db, tmp_path)

    mod_dir = _make_mod(library)

    _register_mod(db, path=str(mod_dir))



    out = ModDeployer(library_root=library, db=db).deploy_mod("92001")

    assert out["success"] is True

    assert out.get("validated") == 2

    target = mods / "TestMod"

    assert (target / "file1.txt").is_file()

    assert (target / "file2.txt").is_file()

    info = db.get_mod_deploy_info("92001")

    assert info is not None

    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED





# ---------------------------------------------------------------------------

# Case 2 — strategy reports success but targets missing → fail

# ---------------------------------------------------------------------------





def test_case2_missing_targets_fail_deploy(

    tmp_path: Path, db: DatabaseManager

) -> None:

    library = tmp_path / "library"

    _install, mods = _setup_game(db, tmp_path)

    mod_dir = _make_mod(library)

    _register_mod(db, path=str(mod_dir))



    # Real plan→apply, then wipe targets so verify fails (Core never calls Strategy.deploy).
    with patch_apply_then_unlink_targets():
        out = ModDeployer(library_root=library, db=db).deploy_mod("92001")

    assert out["success"] is False
    assert out.get("reason") == "missing_targets"
    missing = out.get("missing_targets") or []
    assert len(missing) >= 1
    assert any("file1.txt" in t or "file2.txt" in t for t in missing)

    info = db.get_mod_deploy_info("92001")

    assert info is not None

    assert info.deploy_status == DEPLOY_STATUS_FAILED

    assert str(info.deploy_error or "").strip()

    assert load_manifest(mod_dir) is None





def test_validate_deploy_result_raises_when_target_missing(tmp_path: Path) -> None:

    ghost = tmp_path / "nope.bin"

    result = _fake_success_with_targets([ghost])

    with pytest.raises(DeployValidationError) as caught:

        validate_deploy_result(result)

    assert str(ghost) in caught.value.missing_targets





# ---------------------------------------------------------------------------

# Case 3 — multiple targets, one missing → whole deploy fails

# ---------------------------------------------------------------------------





def test_case3_partial_missing_fails_entire_deploy(

    tmp_path: Path, db: DatabaseManager

) -> None:

    library = tmp_path / "library"

    _install, mods = _setup_game(db, tmp_path)

    mod_dir = _make_mod(library)

    _register_mod(db, path=str(mod_dir))



    # After real apply, remove one planned target → entire deploy fails verify.
    victim = mods / "TestMod" / "file2.txt"
    with patch_apply_then_unlink_targets(only=victim):
        out = ModDeployer(library_root=library, db=db).deploy_mod("92001")

    assert out["success"] is False
    assert out.get("reason") == "missing_targets"
    assert len(out.get("missing_targets") or []) == 1
    assert any("file2.txt" in t for t in (out.get("missing_targets") or []))





# ---------------------------------------------------------------------------

# Case 4 — validation failure triggers rollback / restores original

# ---------------------------------------------------------------------------





def test_case4_validation_failure_triggers_rollback(

    tmp_path: Path, db: DatabaseManager

) -> None:

    library = tmp_path / "library"

    _install, mods = _setup_game(db, tmp_path)

    mod_dir = _make_mod(library)

    _register_mod(db, path=str(mod_dir))



    prior = mods / "TestMod" / "file1.txt"

    prior.parent.mkdir(parents=True)

    prior.write_text("ORIGINAL", encoding="utf-8")

    (mods / "TestMod" / "file2.txt").write_text("ORIGINAL2", encoding="utf-8")



    with patch_apply_then_unlink_targets():
        out = ModDeployer(library_root=library, db=db).deploy_mod("92001")



    assert out["success"] is False

    assert out.get("reason") == "missing_targets"

    assert prior.read_text(encoding="utf-8") == "ORIGINAL"

    assert (mods / "TestMod" / "file2.txt").read_text(encoding="utf-8") == "ORIGINAL2"

    assert load_manifest(mod_dir) is None

    info = db.get_mod_deploy_info("92001")

    assert info is not None

    assert info.deploy_status == DEPLOY_STATUS_FAILED

    assert str(info.deploy_error or "").strip()





# ---------------------------------------------------------------------------

# Case 5 — legacy folder_copy path unchanged

# ---------------------------------------------------------------------------





def test_case5_legacy_folder_copy_unaffected(

    tmp_path: Path, db: DatabaseManager

) -> None:

    library = tmp_path / "library"

    _install, mods = _setup_game(db, tmp_path)

    mod_dir = _make_mod(library, mod_id="92005", folder="LegacyMod")

    _register_mod(db, mod_id="92005", path=str(mod_dir))



    out = ModDeployer(library_root=library, db=db).deploy_mod("92005")

    assert out["success"] is True

    assert out["deploy_type"] == "folder_copy"

    assert (mods / "LegacyMod" / "file1.txt").read_text(encoding="utf-8") == "one"

    assert (mods / "LegacyMod" / "file2.txt").read_text(encoding="utf-8") == "two"

    assert load_manifest(mod_dir) is not None





# ---------------------------------------------------------------------------

# prepare: custom_path overlaps top-level dirs → do not flatten bin/Data

# ---------------------------------------------------------------------------





def test_custom_path_overlap_preserves_bin_layout(tmp_path: Path) -> None:

    extract = tmp_path / "extract"

    (extract / "bin").mkdir(parents=True)

    (extract / "Data").mkdir(parents=True)

    (extract / "bin" / "bink2w64.dll").write_bytes(b"dll")

    (extract / "Data" / "x.pak").write_bytes(b"pak")



    game_root = tmp_path / "Baldurs Gate 3"

    (game_root / "bin").mkdir(parents=True)

    (game_root / "Data").mkdir(parents=True)



    def _fake_find_mod_root(path: Path) -> Path:

        # Legacy behaviour would lift bin/ to content root.

        return path / "bin"



    root = _choose_archive_extract_root(

        extract,

        preserve_extract_layout=False,

        custom_deploy_path=str(game_root),

        find_mod_root_fn=_fake_find_mod_root,

    )

    assert root == extract

    assert (root / "bin" / "bink2w64.dll").is_file()





def test_without_custom_overlap_still_uses_find_mod_root(tmp_path: Path) -> None:

    extract = tmp_path / "extract"

    (extract / "bin").mkdir(parents=True)

    (extract / "bin" / "bink2w64.dll").write_bytes(b"dll")



    custom = tmp_path / "OtherGame"

    custom.mkdir()

    (custom / "Mods").mkdir()



    lifted = extract / "bin"



    def _fake_find_mod_root(path: Path) -> Path:

        return path / "bin"



    root = _choose_archive_extract_root(

        extract,

        preserve_extract_layout=False,

        custom_deploy_path=str(custom),

        find_mod_root_fn=_fake_find_mod_root,

    )

    assert root == lifted

