"""Deploy lifecycle closure — materialization, cleanup, nested archive, ownership.

Does not re-audit Identity, copy performance, UI, or schema.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import ModFileEntry, ModFilesBundle
from services.backup_manager import BackupManager
from services.deploy import ModDeployer, prepare_deploy_content
from services.deploy_apply import ApplyResult
from services.deploy_rules.generic import deploy_wrapper_folder, iter_deploy_payload_files
from services.deploy_rules.manifest import (
    DeployManifest,
    ManifestFileEntry,
    load_manifest,
    save_manifest,
)
from services.deploy_status import classify_folder_copy_target
from services.file_ops import INFO_DIR_NAME
from services.importers.archive import import_cache_root
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


APP_ID = 100


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "lifecycle_closure.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name="SomeGame", folder_name="SomeGame"))
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _write_zip(path: Path, mapping: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in mapping.items():
            zf.writestr(name, data)
    return path


def _staging_leftovers() -> list[Path]:
    root = import_cache_root()
    if not root.is_dir():
        return []
    return [p for p in root.iterdir() if p.name.startswith("deploy_")]


def _seed_mod(
    tmp_path: Path,
    db: DatabaseManager,
    *,
    title: str,
    workspace_id: str,
    files: dict[str, str] | None = None,
    info_secret: str = "secret-meta",
) -> tuple[Path, Path, object, str]:
    library = tmp_path / "library"
    install_mods = tmp_path / "GameMods"
    install_mods.mkdir(parents=True, exist_ok=True)
    managed = library / "SomeGame" / title
    managed.mkdir(parents=True)
    (managed / INFO_DIR_NAME).mkdir()
    (managed / INFO_DIR_NAME / "secret.json").write_text(info_secret, encoding="utf-8")
    for rel, text in (files or {"payload.txt": "hello"}).items():
        path = managed / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    created = create_steam_test_mod(
        db, external_id=workspace_id, title=title, app_id=APP_ID
    )
    pk = prove_managed_folder(
        db,
        managed,
        handle=created.mod_id,
        title=title,
        app_id=APP_ID,
        game_name="SomeGame",
    )
    db.update_game_deploy_config(APP_ID, name="SomeGame", mod_path=str(install_mods))
    return library, install_mods, created, pk


def _timing_diag(managed: Path) -> dict:
    path = managed / INFO_DIR_NAME / "deploy_timing.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data.get("diagnostics") or {})


def test_ordinary_directory_has_no_full_materialized_copy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library, install_mods, created, pk = _seed_mod(
        tmp_path, db, title="PlainMod", workspace_id="88001"
    )
    managed = library / "SomeGame" / "PlainMod"
    content, _allowed, cleanup, overlays = prepare_deploy_content(pk, managed, db=db)
    assert content.resolve() == managed.resolve()
    assert cleanup is None
    assert overlays == ()
    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is True, result
    diag = _timing_diag(managed)
    assert int(diag.get("outer_bytes_to_temp") or 0) == 0
    assert _staging_leftovers() == []
    dest = install_mods / "PlainMod"
    assert (dest / "payload.txt").read_text(encoding="utf-8") == "hello"
    assert not (dest / INFO_DIR_NAME).exists()


def test_selected_nested_archive_extracts_overlay_only(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library, install_mods, created, pk = _seed_mod(
        tmp_path,
        db,
        title="NestedSel",
        workspace_id="88002",
        files={"outer.txt": "keep-outer"},
    )
    managed = library / "SomeGame" / "NestedSel"
    _write_zip(managed / "inner.zip", {"from_zip.txt": b"zip-payload"})
    db.set_mod_files(
        pk,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    filename="inner.zip",
                    path="inner.zip",
                    selected_for_deploy=True,
                    enabled=True,
                )
            ]
        ),
    )
    content, _allowed, cleanup, overlays = prepare_deploy_content(pk, managed, db=db)
    try:
        assert content.resolve() == managed.resolve()
        assert overlays
        stage_names = {
            p.name
            for root in overlays
            for p in root.rglob("*")
            if p.is_file()
        }
        assert "outer.txt" not in stage_names
        assert "from_zip.txt" in stage_names
    finally:
        from services.importers.archive import cleanup_import_cache

        cleanup_import_cache(cleanup)

    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is True, result
    dest = Path(result["target"])
    assert (dest / "outer.txt").read_text(encoding="utf-8") == "keep-outer"
    assert (dest / "from_zip.txt").read_bytes() == b"zip-payload"
    assert not (dest / "inner.zip").exists()
    assert int(_timing_diag(managed).get("outer_bytes_to_temp") or 0) == 0
    assert _staging_leftovers() == []


def test_unselected_archive_is_not_extracted(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library, _install_mods, created, pk = _seed_mod(
        tmp_path,
        db,
        title="TwoZips",
        workspace_id="88003",
        files={"keep.txt": "outer"},
    )
    managed = library / "SomeGame" / "TwoZips"
    _write_zip(managed / "keep.zip", {"selected.txt": b"yes"})
    _write_zip(managed / "keep_v2.zip", {"unselected.txt": b"no"})
    db.set_mod_files(
        pk,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    filename="keep.zip",
                    path="keep.zip",
                    selected_for_deploy=True,
                    enabled=True,
                ),
                ModFileEntry(
                    filename="keep_v2.zip",
                    path="keep_v2.zip",
                    selected_for_deploy=False,
                    enabled=False,
                ),
            ]
        ),
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is True, result
    dest = Path(result["target"])
    assert (dest / "selected.txt").read_bytes() == b"yes"
    assert not (dest / "unselected.txt").exists()
    assert not (dest / "keep.zip").exists()
    assert not (dest / "keep_v2.zip").exists()
    assert (managed / "keep_v2.zip").is_file()


def test_collision_precedence_is_deterministic_not_walk_order(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library, _install_mods, created, pk = _seed_mod(
        tmp_path,
        db,
        title="Collide",
        workspace_id="88004",
        files={"shared.txt": "from-outer"},
    )
    managed = library / "SomeGame" / "Collide"
    _write_zip(
        managed / "inner.zip",
        {"shared.txt": b"from-archive", "only_inner.txt": b"inner"},
    )
    db.set_mod_files(
        pk,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    filename="inner.zip",
                    path="inner.zip",
                    selected_for_deploy=True,
                    enabled=True,
                )
            ]
        ),
    )
    deployer = ModDeployer(library_root=library, db=db)
    ctx, early, cleanup = deployer._resolve_context(  # noqa: SLF001
        created.internal_id, require_target_exists=True, prepare_archives=True
    )
    try:
        assert early is None and ctx is not None
        planned = iter_deploy_payload_files(ctx)
        by_rel = {rel.as_posix(): src for src, rel in planned}
        assert by_rel["shared.txt"].read_text(encoding="utf-8") == "from-outer"
        assert by_rel["shared.txt"].resolve() == (managed / "shared.txt").resolve()
    finally:
        from services.importers.archive import cleanup_import_cache

        cleanup_import_cache(cleanup)

    result = deployer.deploy_mod(created.internal_id)
    assert result["success"] is True, result
    dest = Path(result["target"])
    assert (dest / "shared.txt").read_text(encoding="utf-8") == "from-outer"
    assert (dest / "only_inner.txt").read_bytes() == b"inner"


def test_info_never_enters_game_payload(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library, install_mods, created, _pk = _seed_mod(
        tmp_path, db, title="InfoSkip", workspace_id="88005"
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is True, result
    dest = install_mods / "InfoSkip"
    names = {p.name for p in dest.rglob("*")}
    assert "payload.txt" in names
    assert "secret.json" not in names
    assert INFO_DIR_NAME not in {p.name for p in dest.iterdir()}


def test_successful_deploy_cleanup_drops_staging(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library, _install_mods, created, pk = _seed_mod(
        tmp_path,
        db,
        title="CleanOk",
        workspace_id="88006",
        files={"a.txt": "a"},
    )
    managed = library / "SomeGame" / "CleanOk"
    _write_zip(managed / "pack.zip", {"b.txt": b"b"})
    db.set_mod_files(
        pk,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    filename="pack.zip",
                    path="pack.zip",
                    selected_for_deploy=True,
                    enabled=True,
                )
            ]
        ),
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is True, result
    assert _staging_leftovers() == []
    txn = managed / INFO_DIR_NAME / "deploy_transaction.json"
    assert not txn.exists()


def test_extraction_failure_cleanup(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library, install_mods, created, pk = _seed_mod(
        tmp_path, db, title="BadZip", workspace_id="88007"
    )
    managed = library / "SomeGame" / "BadZip"
    (managed / "broken.zip").write_bytes(b"this is not a zip")
    db.set_mod_files(
        pk,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    filename="broken.zip",
                    path="broken.zip",
                    selected_for_deploy=True,
                    enabled=True,
                )
            ]
        ),
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is False
    assert _staging_leftovers() == []
    dest = install_mods / "BadZip"
    assert not dest.exists()
    assert not (managed / INFO_DIR_NAME / "deploy_transaction.json").exists()


def test_copy_failure_cleanup(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library, install_mods, created, _pk = _seed_mod(
        tmp_path,
        db,
        title="CopyFail",
        workspace_id="88008",
        files={"one.txt": "1", "two.txt": "2"},
    )
    managed = library / "SomeGame" / "CopyFail"

    def _fail_after_one(plan, **_kwargs):  # noqa: ANN001
        entry = plan.files[0]
        dest = Path(entry.target_absolute)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("partial", encoding="utf-8")
        return ApplyResult(
            success=False,
            error="simulated copy failure",
            applied=1,
            failed_details=[str(dest)],
        )

    with patch("services.deploy_apply.apply_file_plan", _fail_after_one):
        result = ModDeployer(library_root=library, db=db).deploy_mod(
            created.internal_id
        )
    assert result["success"] is False
    assert "copy" in str(result.get("error") or "").lower() or "simulated" in str(
        result.get("error") or ""
    )
    dest = install_mods / "CopyFail"
    assert not dest.exists()
    assert _staging_leftovers() == []
    assert not (managed / INFO_DIR_NAME / "deploy_transaction.json").exists()


def test_target_absent_manifest_present_no_phantom_backup(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library, install_mods, created, pk = _seed_mod(
        tmp_path, db, title="GhostTgt", workspace_id="88009"
    )
    managed = library / "SomeGame" / "GhostTgt"
    deployer = ModDeployer(library_root=library, db=db)
    first = deployer.deploy_mod(created.internal_id)
    assert first["success"] is True, first
    dest = install_mods / "GhostTgt"
    import shutil

    shutil.rmtree(dest)
    assert load_manifest(managed) is not None
    before = BackupManager(managed, internal_id=created.internal_id, mod_pk=pk)
    before_files = list(before.backups_root().glob("*")) if before.backups_root().exists() else []
    second = deployer.deploy_mod(created.internal_id)
    assert second["success"] is True, second
    after = list(before.backups_root().glob("*")) if before.backups_root().exists() else []
    assert dest.is_dir()
    # Missing dest must not mint backups of a phantom payload.
    assert len(after) <= len(before_files)
    diag = _timing_diag(managed)
    assert int(diag.get("backup_files") or 0) == 0


def test_target_present_manifest_absent_does_not_guess_owned(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library, install_mods, created, _pk = _seed_mod(
        tmp_path, db, title="NoMan", workspace_id="88010", files={"a.txt": "NEW"}
    )
    dest = install_mods / "NoMan"
    dest.mkdir()
    (dest / "a.txt").write_text("ORIGINAL", encoding="utf-8")
    kind = classify_folder_copy_target(
        internal_id=created.internal_id,
        managed=library / "SomeGame" / "NoMan",
        mod_path=install_mods,
        library_root=library,
        mod_pk=created.mod_id,
        workspace_id=created.workspace_id,
    )
    assert kind == "foreign"
    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is True, result
    assert (dest / "a.txt").read_text(encoding="utf-8") == "NEW"


def test_owned_target_vs_foreign_target(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library, install_mods, ours, _pk = _seed_mod(
        tmp_path, db, title="OursMod", workspace_id="88011"
    )
    deployer = ModDeployer(library_root=library, db=db)
    out = deployer.deploy_mod(ours.internal_id)
    assert out["success"] is True, out
    managed = library / "SomeGame" / "OursMod"
    assert (
        classify_folder_copy_target(
            internal_id=ours.internal_id,
            managed=managed,
            mod_path=install_mods,
            library_root=library,
            mod_pk=ours.mod_id,
            workspace_id=ours.workspace_id,
        )
        == "ours"
    )

    other_lib = library / "SomeGame" / "OtherLib"
    other_lib.mkdir(parents=True, exist_ok=True)
    (other_lib / INFO_DIR_NAME).mkdir(exist_ok=True)
    foreign_payload = install_mods / "ForeignTree"
    foreign_payload.mkdir()
    (foreign_payload / "x.txt").write_text("not-ours", encoding="utf-8")
    save_manifest(
        other_lib,
        DeployManifest(
            schema_version=2,
            internal_id="00000000-0000-0000-0000-000000000099",
            mod_id="999999",
            deploy_time="2026-01-01T00:00:00+00:00",
            deploy_type="folder_copy",
            files=[
                ManifestFileEntry(
                    source=str(foreign_payload / "x.txt"),
                    target=str(foreign_payload / "x.txt"),
                    relative="x.txt",
                )
            ],
        ),
    )
    assert (
        classify_folder_copy_target(
            internal_id=ours.internal_id,
            managed=managed,
            mod_path=install_mods,
            library_root=library,
            mod_pk=ours.mod_id,
            workspace_id=ours.workspace_id,
        )
        == "ours"
    )
    assert deploy_wrapper_folder(managed.name, ours.workspace_id) == "OursMod"
    assert (install_mods / "OursMod" / "payload.txt").is_file()
    assert (foreign_payload / "x.txt").read_text(encoding="utf-8") == "not-ours"


def test_numeric_folder_name_is_not_pk_or_ownership_guess(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library, install_mods, created, pk = _seed_mod(
        tmp_path,
        db,
        title="AsciiNameMod",
        workspace_id="3308841999",
        files={"n.txt": "ok"},
    )
    pk_folder = install_mods / str(pk)
    pk_folder.mkdir()
    (pk_folder / "trap.txt").write_text("do-not-claim", encoding="utf-8")
    ws_folder = install_mods / created.workspace_id
    ws_folder.mkdir()
    (ws_folder / "trap2.txt").write_text("not-workspace", encoding="utf-8")

    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is True, result
    dest = Path(result["target"])
    assert dest.name == "AsciiNameMod"
    assert dest.name != str(pk)
    assert dest.name != created.workspace_id
    assert dest.name != f"mod_{pk}"
    assert (pk_folder / "trap.txt").read_text(encoding="utf-8") == "do-not-claim"
    assert (ws_folder / "trap2.txt").read_text(encoding="utf-8") == "not-workspace"
    assert (dest / "n.txt").read_text(encoding="utf-8") == "ok"
    kind = classify_folder_copy_target(
        internal_id=created.internal_id,
        managed=library / "SomeGame" / "AsciiNameMod",
        mod_path=install_mods,
        library_root=library,
        mod_pk=pk,
        workspace_id=created.workspace_id,
    )
    assert kind == "ours"


def test_han_basename_owned_target_is_workspace_wrapper_not_pk(
    tmp_path: Path, db: DatabaseManager
) -> None:
    title = "死而复生 Resurrection event"
    library, install_mods, created, pk = _seed_mod(
        tmp_path, db, title=title, workspace_id="2511735990"
    )
    result = ModDeployer(library_root=library, db=db).deploy_mod(created.internal_id)
    assert result["success"] is True, result
    dest = Path(result["target"])
    assert dest.name == "mod_2511735990"
    assert dest.name != str(pk)
    assert dest.name != title
    kind = classify_folder_copy_target(
        internal_id=created.internal_id,
        managed=library / "SomeGame" / title,
        mod_path=install_mods,
        library_root=library,
        mod_pk=pk,
        workspace_id=created.workspace_id,
    )
    assert kind == "ours"
    assert dest == (install_mods / "mod_2511735990").resolve()
