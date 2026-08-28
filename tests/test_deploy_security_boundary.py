"""Production security boundaries for deploy / undeploy / backup restore."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_TYPE_FOLDER_COPY, DatabaseManager
from core.models import ModMetadata
from services.backup_manager import BACKUPS_DIRNAME, BackupIntegrityError, BackupManager
from services.deploy import ModDeployer
from services.deploy_rules import load_manifest, save_manifest
from services.deploy_rules.base import DeployContext
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestBackupInfo,
    ManifestFileEntry,
    prune_protection,
    remove_empty_parents,
)
from services.deploy_security import (
    ManifestSecurityError,
    collect_allowed_target_roots,
    collect_protected_roots,
    validate_manifest_mod_id,
    validate_manifest_targets,
    validate_planned_sources,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_security.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _meta(mod: Path, *, mid: str, title: str, app_id: int = 4242) -> None:
    info = mod / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": title,
                "app_id": app_id,
                "game_name": "SomeGame",
            }
        ),
        encoding="utf-8",
    )


def _setup(db: DatabaseManager, tmp_path: Path, *, app_id: int = 4242) -> Path:
    mods_root = tmp_path / "GameMods"
    mods_root.mkdir()
    db.update_game_deploy_config(
        app_id,
        name="SomeGame",
        mod_path=str(mods_root),
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        install_path=str(tmp_path / "GameInstall"),
    )
    (tmp_path / "GameInstall").mkdir(exist_ok=True)
    return mods_root


def _add_mod(
    library: Path,
    db: DatabaseManager,
    *,
    mid: str,
    title: str,
    files: dict[str, str] | None = None,
    app_id: int = 4242,
) -> Path:
    mod = library / "SomeGame" / title
    mod.mkdir(parents=True)
    for rel, text in (files or {"a.txt": "MOD"}).items():
        path = mod / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _meta(mod, mid=mid, title=title, app_id=app_id)
    db.upsert_mod(ModMetadata(published_file_id=mid, title=title, app_id=app_id))
    return mod


def _ctx(
    *,
    mid: str,
    mod: Path,
    db: DatabaseManager,
    app_id: int = 4242,
) -> DeployContext:
    cfg = db.get_game_deploy_config(app_id)
    assert cfg is not None
    return DeployContext(
        mod_id=mid,
        source=mod,
        app_id=app_id,
        config=cfg,
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        managed_path=mod,
    )


# ---------------------------------------------------------------------------
# Case1 — malicious manifest target traversal
# ---------------------------------------------------------------------------


def test_case1_malicious_manifest_target_traversal(tmp_path: Path, db: DatabaseManager) -> None:
    mods_root = _setup(db, tmp_path)
    library = tmp_path / "library"
    mod = _add_mod(library, db, mid="91001", title="SecModA")
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("KEEP", encoding="utf-8")

    evil_target = (mods_root / ".." / "outside_secret.txt").resolve()
    assert evil_target == outside.resolve()

    man = DeployManifest(
        mod_id="91001",
        deploy_time="t",
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
        files=[
            ManifestFileEntry(
                source=str(mod / "a.txt"),
                target=str(evil_target),
            )
        ],
    )
    save_manifest(mod, man)

    ctx = _ctx(mid="91001", mod=mod, db=db)
    with pytest.raises(ManifestSecurityError, match="outside allowed"):
        validate_manifest_targets(
            man, allowed_roots=collect_allowed_target_roots(ctx)
        )

    deployer = ModDeployer(library_root=library, db=db)
    out = deployer.undeploy_mod("91001")
    assert out["success"] is False
    assert outside.read_text(encoding="utf-8") == "KEEP"
    assert "安全校验" in str(out.get("error") or "") or "mismatch" in str(
        out.get("error") or ""
    ).lower() or "清单" in str(out.get("error") or "")


# ---------------------------------------------------------------------------
# Case2 — illegal backup path restore
# ---------------------------------------------------------------------------


def test_case2_illegal_backup_path(tmp_path: Path) -> None:
    managed = tmp_path / "mod"
    managed.mkdir()
    (managed / INFO_DIR_NAME / BACKUPS_DIRNAME).mkdir(parents=True)
    external = tmp_path / "temp_a.dll"
    external.write_text("DLL", encoding="utf-8")

    mgr = BackupManager(managed)
    with pytest.raises(BackupIntegrityError, match="escapes|traversal"):
        mgr.resolve_backup_file(
            ManifestBackupInfo(path=str(external.resolve()), hash="x")
        )

    with pytest.raises(BackupIntegrityError, match="traversal|escapes"):
        mgr.resolve_backup_file(
            ManifestBackupInfo(path=".info/backups/../../temp_a.dll", hash="x")
        )


# ---------------------------------------------------------------------------
# Case3 — Mod A must not use Mod B manifest
# ---------------------------------------------------------------------------


def test_case3_mod_a_cannot_read_mod_b_manifest(tmp_path: Path, db: DatabaseManager) -> None:
    _setup(db, tmp_path)
    library = tmp_path / "library"
    mod_a = _add_mod(library, db, mid="91003", title="ModA")
    mod_b = _add_mod(library, db, mid="91004", title="ModB", files={"b.txt": "B"})

    save_manifest(
        mod_a,
        DeployManifest(
            mod_id="91004",  # pollution: claims B
            deploy_time="t",
            deploy_type=DEPLOY_TYPE_FOLDER_COPY,
            files=[
                ManifestFileEntry(
                    source=str(mod_b / "b.txt"),
                    target=str((tmp_path / "GameMods" / "x.txt").resolve()),
                )
            ],
        ),
    )

    assert load_manifest(mod_a, expected_mod_id="91003") is None
    loaded = load_manifest(mod_a)
    assert loaded is not None
    with pytest.raises(ManifestSecurityError, match="mismatch"):
        validate_manifest_mod_id(loaded, "91003")

    deployer = ModDeployer(library_root=library, db=db)
    out = deployer.undeploy_mod("91003")
    assert out["success"] is False


# ---------------------------------------------------------------------------
# Case4 — remove_empty_parents never deletes protected roots
# ---------------------------------------------------------------------------


def test_case4_remove_empty_parent_protection(tmp_path: Path, db: DatabaseManager) -> None:
    mods_root = _setup(db, tmp_path)
    install = tmp_path / "GameInstall"
    nested = mods_root / "ModFolder" / "sub" / "deep"
    nested.mkdir(parents=True)
    leaf = nested / "gone.txt"
    leaf.write_text("x", encoding="utf-8")
    leaf.unlink()

    library = tmp_path / "library"
    mod = _add_mod(library, db, mid="91005", title="PruneMod")
    ctx = _ctx(mid="91005", mod=mod, db=db)
    protected = collect_protected_roots(ctx)

    with prune_protection(protected):
        remove_empty_parents(nested, stop_at=mods_root, protected=protected)

    assert mods_root.is_dir()
    assert install.is_dir()
    assert mod.is_dir()

    # Direct attempt to prune at game / install roots must no-op
    remove_empty_parents(mods_root, stop_at=mods_root, protected=[mods_root, install])
    assert mods_root.is_dir()
    remove_empty_parents(install / "empty", stop_at=install, protected=[install])
    assert install.is_dir()


# ---------------------------------------------------------------------------
# Case5 — source outside workspace rejected
# ---------------------------------------------------------------------------


def test_case5_source_outside_workspace(tmp_path: Path, db: DatabaseManager) -> None:
    _setup(db, tmp_path)
    library = tmp_path / "library"
    mod = _add_mod(library, db, mid="91006", title="SrcMod")
    external = tmp_path / "evil_payload.dll"
    external.write_text("BAD", encoding="utf-8")

    entries = [
        ManifestFileEntry(
            source=str(external.resolve()),
            target=str((tmp_path / "GameMods" / "SrcMod" / "evil_payload.dll").resolve()),
        )
    ]
    with pytest.raises(ManifestSecurityError, match="outside mod workspace"):
        validate_planned_sources(
            entries,
            workspace_roots=[mod.resolve(), (mod).resolve()],
        )

    deployer = ModDeployer(library_root=library, db=db)
    from unittest.mock import patch
    from services.deploy_rules.base import StrategyResult

    planned = StrategyResult(
        success=True,
        target=str(tmp_path / "GameMods" / "SrcMod"),
        files=entries,
        deploy_type=DEPLOY_TYPE_FOLDER_COPY,
    )
    with patch(
        "services.deploy_rules.generic.FolderCopyStrategy.plan",
        return_value=planned,
    ):
        out = deployer.deploy_mod("91006")
    assert out["success"] is False
    assert "安全校验" in str(out.get("error") or "")


# ---------------------------------------------------------------------------
# Case6 — shared / referenced backup prune protection
# ---------------------------------------------------------------------------


def test_case6_shared_backup_reference_protection(tmp_path: Path, db: DatabaseManager) -> None:
    mods_root = _setup(db, tmp_path)
    library = tmp_path / "library"
    mod_a = _add_mod(library, db, mid="91007", title="ShareA", files={"a.txt": "A"})
    mod_b = _add_mod(library, db, mid="91008", title="ShareB", files={"a.txt": "B"})

    # Seed a shared game file so both create backups on deploy
    shared = mods_root / "ShareA" / "a.txt"
    shared.parent.mkdir(parents=True)
    shared.write_text("GAME", encoding="utf-8")
    shared_b = mods_root / "ShareB" / "a.txt"
    shared_b.parent.mkdir(parents=True)
    shared_b.write_text("GAME", encoding="utf-8")

    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod("91007")["success"] is True
    assert deployer.deploy_mod("91008")["success"] is True

    man_a = load_manifest(mod_a)
    man_b = load_manifest(mod_b)
    assert man_a and man_a.files and man_a.files[0].backup
    assert man_b and man_b.files and man_b.files[0].backup
    bak_a = mod_a / man_a.files[0].backup.path
    bak_b = mod_b / man_b.files[0].backup.path
    assert bak_a.is_file()
    assert bak_b.is_file()

    # Same-mod: two entries share one backup; prune with empty keep must retain it
    shared_rel = man_a.files[0].backup.path
    man_a.files.append(
        ManifestFileEntry(
            source=str(mod_a / "a.txt"),
            target=str((mods_root / "ShareA" / "extra.txt").resolve()),
            backup=ManifestBackupInfo(
                path=shared_rel,
                hash=man_a.files[0].backup.hash,
                created_at=man_a.files[0].backup.created_at,
            ),
        )
    )
    save_manifest(mod_a, man_a)
    BackupManager(mod_a).prune_unreferenced_backups(set())
    assert bak_a.is_file()

    # Cross-mod: undeploy A must not touch B's backup
    assert deployer.undeploy_mod("91007")["success"] is True
    assert bak_b.is_file()
    assert load_manifest(mod_b) is not None


# ---------------------------------------------------------------------------
# Case7 — illegal manifest refuses undeploy (no deletes)
# ---------------------------------------------------------------------------


def test_case7_illegal_manifest_refuses_undeploy(tmp_path: Path, db: DatabaseManager) -> None:
    mods_root = _setup(db, tmp_path)
    library = tmp_path / "library"
    mod = _add_mod(library, db, mid="91009", title="GuardMod")
    deployed = mods_root / "GuardMod" / "a.txt"
    deployed.parent.mkdir(parents=True)
    deployed.write_text("DEPLOYED", encoding="utf-8")

    victim = tmp_path / "should_remain.txt"
    victim.write_text("SAFE", encoding="utf-8")

    save_manifest(
        mod,
        DeployManifest(
            mod_id="91009",
            deploy_time="t",
            deploy_type=DEPLOY_TYPE_FOLDER_COPY,
            files=[
                ManifestFileEntry(
                    source=str(mod / "a.txt"),
                    target=str(victim.resolve()),
                ),
                ManifestFileEntry(
                    source=str(mod / "a.txt"),
                    target=str(deployed.resolve()),
                ),
            ],
        ),
    )

    deployer = ModDeployer(library_root=library, db=db)
    out = deployer.undeploy_mod("91009")
    assert out["success"] is False
    assert victim.read_text(encoding="utf-8") == "SAFE"
    assert deployed.read_text(encoding="utf-8") == "DEPLOYED"
