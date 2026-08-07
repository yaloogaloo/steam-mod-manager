"""Production hardening: audit, conflicts, deploy_error, undeploy safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_FAILED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.models import ModMetadata
from services.deploy import ModDeployer
from services.deploy_audit import (
    STATUS_BROKEN,
    STATUS_CONSISTENT,
    STATUS_MISSING,
    audit_deploy_state,
    scan_deployed_mods,
)
from services.deploy_conflict import detect_deploy_conflicts
from services.deploy_rules import MANIFEST_FILENAME, load_manifest
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_audit.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _meta(mod: Path, mid: str, app_id: int = 7) -> None:
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "{mod.name}",\n'
        f'  "app_id": {app_id},\n'
        '  "game_name": "G"\n'
        "}\n",
        encoding="utf-8",
    )


def test_manifest_complete_after_generic_deploy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    mod = library / "G" / "M1"
    mod.mkdir(parents=True)
    (mod / "a.txt").write_text("a", encoding="utf-8")
    (mod / "b.txt").write_text("b", encoding="utf-8")
    _meta(mod, "97001")
    db.update_game_deploy_config(7, name="G", mod_path=str(mods_root))
    db.upsert_mod(ModMetadata(published_file_id="97001", title="M1", app_id=7))

    result = ModDeployer(library_root=library, db=db).deploy_mod("97001")
    assert result["success"]
    man = load_manifest(mod)
    assert man is not None
    assert len(man.files) == 2
    assert (mod / INFO_DIR_NAME / MANIFEST_FILENAME).is_file()


def test_undeploy_does_not_delete_other_mod_files(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()

    a = library / "G" / "A"
    a.mkdir(parents=True)
    (a / "a.txt").write_text("a", encoding="utf-8")
    _meta(a, "97010")
    b = library / "G" / "B"
    b.mkdir(parents=True)
    (b / "b.txt").write_text("b", encoding="utf-8")
    _meta(b, "97011")

    db.update_game_deploy_config(7, name="G", mod_path=str(mods_root))
    db.upsert_mod(ModMetadata(published_file_id="97010", title="A", app_id=7))
    db.upsert_mod(ModMetadata(published_file_id="97011", title="B", app_id=7))

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod("97010")["success"]
    assert dep.deploy_mod("97011")["success"]
    foreign = mods_root / "keep.txt"
    foreign.write_text("keep", encoding="utf-8")

    assert dep.undeploy_mod("97010")["success"]
    assert not (mods_root / "A").exists()
    assert (mods_root / "B" / "b.txt").is_file()
    assert foreign.read_text(encoding="utf-8") == "keep"


def test_audit_missing_and_broken(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    mod = library / "G" / "M"
    mod.mkdir(parents=True)
    (mod / "f.txt").write_text("f", encoding="utf-8")
    _meta(mod, "97020")
    db.update_game_deploy_config(7, name="G", mod_path=str(mods_root))
    db.upsert_mod(ModMetadata(published_file_id="97020", title="M", app_id=7))

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod("97020")["success"]
    ok = audit_deploy_state("97020", library_root=library, db=db)
    assert ok.status == STATUS_CONSISTENT

    # Break: delete a target file
    target = mods_root / "M" / "f.txt"
    target.unlink()
    broken = audit_deploy_state("97020", library_root=library, db=db)
    assert broken.status == STATUS_BROKEN
    assert "缺失" in broken.reason or "missing" in broken.reason.lower()

    # Missing source while still marked deployed
    import shutil

    shutil.rmtree(mod)
    db.update_mod_deploy_status(
        "97020",
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path=str(mods_root / "M"),
    )
    missing = audit_deploy_state("97020", library_root=library, db=db)
    assert missing.status == STATUS_MISSING

    scanned = scan_deployed_mods(library, db=db)
    assert any(r.mod_id == "97020" for r in scanned)


def test_deploy_error_persisted(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    library.mkdir()
    mod = library / "G" / "M"
    mod.mkdir(parents=True)
    (mod / "f.txt").write_text("f", encoding="utf-8")
    _meta(mod, "97030")
    db.update_game_deploy_config(
        7, name="G", mod_path=str(tmp_path / "NoSuchModsDir")
    )
    db.upsert_mod(ModMetadata(published_file_id="97030", title="M", app_id=7))

    result = ModDeployer(library_root=library, db=db).deploy_mod("97030")
    assert result["success"] is False
    assert "does not exist" in result["error"].lower() or "不存在" in result["error"]

    info = db.get_mod_deploy_info("97030")
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_FAILED
    assert info.deploy_error
    assert "does not exist" in info.deploy_error.lower() or "不存在" in info.deploy_error


def test_conflict_detection(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    install = tmp_path / "PalInstall"
    install.mkdir()
    # Two mods that both want the same ~mods pak name
    a = library / "Palworld" / "ModA"
    a.mkdir(parents=True)
    (a / "Shared.pak").write_bytes(b"A")
    _meta(a, "98001", app_id=1623730)
    b = library / "Palworld" / "ModB"
    b.mkdir(parents=True)
    (b / "Shared.pak").write_bytes(b"B")
    _meta(b, "98002", app_id=1623730)

    db.update_game_deploy_config(
        1623730,
        name="Palworld",
        install_path=str(install),
        deploy_type="palworld_pak",
    )
    db.upsert_mod(ModMetadata(published_file_id="98001", title="ModA", app_id=1623730))
    db.upsert_mod(ModMetadata(published_file_id="98002", title="ModB", app_id=1623730))

    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod("98001")["success"]

    planned = (
        install / "Pal" / "Content" / "Paks" / "~mods" / "Shared.pak"
    )
    conflict = detect_deploy_conflicts(library, "98002", [planned])
    assert conflict.conflict is True
    assert conflict.files[0].existing_mod == "98001"

    # Deploy still proceeds (warn only) and reports conflicts
    second = dep.deploy_mod("98002")
    assert second["success"] is True
    assert second.get("conflicts", {}).get("conflict") is True


def test_redeploy_cleanup_covered_in_audit_suite(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """Shrinking file set via redeploy (also in test_redeploy_cleanup)."""
    library = tmp_path / "mod"
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    mod = library / "G" / "S"
    mod.mkdir(parents=True)
    (mod / "A.txt").write_text("A", encoding="utf-8")
    (mod / "B.txt").write_text("B", encoding="utf-8")
    _meta(mod, "97040")
    db.update_game_deploy_config(7, name="G", mod_path=str(mods_root))
    db.upsert_mod(ModMetadata(published_file_id="97040", title="S", app_id=7))
    dep = ModDeployer(library_root=library, db=db)
    assert dep.deploy_mod("97040")["success"]
    (mod / "B.txt").unlink()
    assert dep.redeploy_mod("97040")["success"]
    assert (mods_root / "S" / "A.txt").is_file()
    assert not (mods_root / "S" / "B.txt").exists()
    assert db.get_mod_deploy_info("97040").deploy_status == DEPLOY_STATUS_DEPLOYED
    assert db.get_mod_deploy_info("97040").deploy_error == ""
