"""
Real acceptance: Palworld user install flow (deploy → verify → undeploy).

Does not modify production code — validation only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_rules import MANIFEST_FILENAME, load_manifest
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME

APP_ID = 1623730
MOD_ID = "992001"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "palworld_acceptance.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_palworld_real_acceptance_deploy_and_undeploy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """
    Layout (managed library)::

        Palworld_Test_Mod/
        ├── test_normal.pak
        ├── readme.txt
        └── LogicMods/
              └── test_logic.pak

    install_path = tmp/Palworld
    """
    library = tmp_path / "mod"
    install = tmp_path / "Palworld"
    install.mkdir(parents=True)

    # --- Create acceptance Mod exactly as specified ---
    mod = library / "Palworld" / "Palworld_Test_Mod"
    mod.mkdir(parents=True)
    (mod / "test_normal.pak").write_bytes(b"NORMAL-PAK")
    (mod / "readme.txt").write_text("do not deploy", encoding="utf-8")
    logic = mod / "LogicMods"
    logic.mkdir()
    (logic / "test_logic.pak").write_bytes(b"LOGIC-PAK")

    info = mod / INFO_DIR_NAME
    info.mkdir()
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{MOD_ID}",\n'
        '  "title": "Palworld_Test_Mod",\n'
        f'  "app_id": {APP_ID},\n'
        '  "game_name": "Palworld"\n'
        "}\n",
        encoding="utf-8",
    )

    db.update_game_deploy_config(
        APP_ID,
        name="Palworld",
        install_path=str(install),
        # No mod_path: non-pak leftovers (readme) are not folder_copied
        deploy_type="palworld_pak",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id=MOD_ID,
            title="Palworld_Test_Mod",
            app_id=APP_ID,
        )
    )

    # Foreign file that must survive undeploy
    foreign_dir = install / "Pal" / "Content" / "Paks" / "~mods"
    foreign_dir.mkdir(parents=True)
    foreign = foreign_dir / "SomeoneElse.pak"
    foreign.write_bytes(b"FOREIGN-KEEP")

    deployer = ModDeployer(library_root=library, db=db)

    # --- deploy_mod() ---
    result = deployer.deploy_mod(MOD_ID)
    assert result["success"] is True, result

    normal_target = install / "Pal" / "Content" / "Paks" / "~mods" / "test_normal.pak"
    logic_target = (
        install / "Pal" / "Content" / "Paks" / "LogicMods" / "test_logic.pak"
    )

    # 1. Normal pak → ~mods
    assert normal_target.is_file()
    assert normal_target.read_bytes() == b"NORMAL-PAK"

    # 2. LogicMods → Paks/LogicMods/
    assert logic_target.is_file()
    assert logic_target.read_bytes() == b"LOGIC-PAK"

    # 3. readme.txt not copied
    paks = install / "Pal" / "Content" / "Paks"
    assert not (paks / "readme.txt").exists()
    assert not (paks / "~mods" / "readme.txt").exists()
    assert not (paks / "LogicMods" / "readme.txt").exists()
    assert list(paks.rglob("readme.txt")) == []

    # 4. manifest has both targets
    man = load_manifest(mod)
    assert man is not None
    assert (mod / INFO_DIR_NAME / MANIFEST_FILENAME).is_file()
    targets = {Path(f.target).resolve() for f in man.files}
    assert normal_target.resolve() in targets
    assert logic_target.resolve() in targets
    assert len(man.files) == 2

    info_row = db.get_mod_deploy_info(MOD_ID)
    assert info_row is not None
    assert info_row.deploy_status == DEPLOY_STATUS_DEPLOYED

    # --- undeploy_mod() ---
    und = deployer.undeploy_mod(MOD_ID)
    assert und["success"] is True, und

    # 5. Both paks removed
    assert not normal_target.exists()
    assert not logic_target.exists()
    assert load_manifest(mod) is None

    # 6. Other non-owned files retained
    assert foreign.is_file()
    assert foreign.read_bytes() == b"FOREIGN-KEEP"

    cleared = db.get_mod_deploy_info(MOD_ID)
    assert cleared is not None
    assert cleared.deploy_status == DEPLOY_STATUS_NOT_DEPLOYED
