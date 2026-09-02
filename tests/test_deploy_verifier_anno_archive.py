"""Deploy verification and Anno archive manifest regression tests."""

from __future__ import annotations

import zipfile
from pathlib import Path

from types import SimpleNamespace

import pytest

from core.db_manager import (
    DEPLOY_STATUS_DEPLOYED,
    DEPLOY_STATUS_NOT_DEPLOYED,
    DatabaseManager,
)
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import FILE_TYPE_MAIN, ModFileEntry, ModFilesBundle
from services.deploy import ModDeployer, validate_deploy_result
from services.deploy_errors import DeployValidationError
from services.deploy_rules.anno import ANNO_1800_APP_ID, _manifest_entries_for_paths
from services.deploy_rules.generic import FolderCopyStrategy
from services.deploy_rules.manifest import DeployManifest, ManifestFileEntry
from services.deploy_verifier import verify_deploy_result
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_status import CONTENT_HEALTHY


def _write_meta(mod_dir: Path, *, mid: str, title: str, game: str = "Anno 1800") -> None:
    info = mod_dir / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        "{\n"
        f'  "published_file_id": "{mid}",\n'
        f'  "title": "{title}",\n'
        f'  "game_name": "{game}",\n'
        f'  "app_id": {ANNO_1800_APP_ID}\n'
        "}\n",
        encoding="utf-8",
    )


def _make_zip(path: Path, mapping: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in mapping.items():
            zf.writestr(name, data)
    return path


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "deploy_verify.db")
    manager.upsert_game(
        GameInfo(
            app_id=ANNO_1800_APP_ID,
            name="Anno 1800",
            folder_name="Anno 1800",
        )
    )
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_verify_skips_archive_source_size_check(tmp_path: Path) -> None:
    archive = tmp_path / "mod.zip"
    archive.write_bytes(b"x" * 100)
    target = tmp_path / "out" / "readme.md"
    target.parent.mkdir(parents=True)
    target.write_text("hello", encoding="utf-8")

    manifest = DeployManifest(
        mod_id="1",
        deploy_time="2020-01-01T00:00:00+00:00",
        deploy_type="anno_1800",
        files=[
            ManifestFileEntry(
                source=str(archive), target=str(target), type="archive"
            ),
        ],
    )

    result = SimpleNamespace(manifest=manifest)

    assert verify_deploy_result(result) == 1


def test_verify_rejects_missing_target(tmp_path: Path) -> None:
    manifest = DeployManifest(
        mod_id="1",
        deploy_time="2020-01-01T00:00:00+00:00",
        deploy_type="folder_copy",
        files=[
            ManifestFileEntry(
                source=str(tmp_path / "missing-src.txt"),
                target=str(tmp_path / "missing-dst.txt"),
            ),
        ],
    )

    result = SimpleNamespace(manifest=manifest)

    with pytest.raises(DeployValidationError) as exc:
        verify_deploy_result(result)
    assert "缺少" in str(exc.value)


def test_verify_accepts_empty_target_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")

    manifest = DeployManifest(
        mod_id="1",
        deploy_time="2020-01-01T00:00:00+00:00",
        deploy_type="folder_copy",
        files=[ManifestFileEntry(source=str(empty), target=str(empty))],
    )

    result = SimpleNamespace(manifest=manifest)

    assert verify_deploy_result(result) == 1


def test_folder_copy_single_file(tmp_path: Path) -> None:
    mod_name = "ModA"
    source = tmp_path / "src" / mod_name
    target_root = tmp_path / "dst"
    (source / "a").mkdir(parents=True)
    (source / "a" / "one.txt").write_text("one", encoding="utf-8")

    ctx = _folder_ctx(source, target_root)
    result = FolderCopyStrategy().deploy(ctx)
    assert result.success
    assert (target_root / mod_name / "a" / "one.txt").is_file()
    validate_deploy_result(result)


def test_folder_copy_nested_dirs(tmp_path: Path) -> None:
    mod_name = "Nested"
    source = tmp_path / "src" / mod_name
    target_root = tmp_path / "dst"
    deep = source / "x" / "y" / "z"
    deep.mkdir(parents=True)
    (deep / "leaf.bin").write_bytes(b"payload")

    ctx = _folder_ctx(source, target_root)
    result = FolderCopyStrategy().deploy(ctx)
    assert result.success
    assert (target_root / mod_name / "x" / "y" / "z" / "leaf.bin").read_bytes() == b"payload"


def test_folder_copy_existing_target_overwrites(tmp_path: Path) -> None:
    mod_name = "Mod"
    source = tmp_path / "src" / mod_name
    target_root = tmp_path / "dst"
    target = target_root / mod_name
    target.mkdir(parents=True, exist_ok=True)
    (target / "old.txt").write_text("old", encoding="utf-8")
    source.mkdir(parents=True)
    (source / "new.txt").write_text("new", encoding="utf-8")

    result = FolderCopyStrategy().deploy(_folder_ctx(source, target_root))
    assert result.success
    assert (target / "new.txt").read_text(encoding="utf-8") == "new"


def test_folder_copy_source_missing_fails(tmp_path: Path) -> None:
    source = tmp_path / "missing"
    target = tmp_path / "dst" / "Mod"
    result = FolderCopyStrategy().deploy(_folder_ctx(source, target))
    assert result.success is False


def test_anno_archive_brackets_spaces_mod_name(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mod_id = "9000000000000343"
    folder = library / "Anno 1800" / "21 Legendary Items (Lion053)"
    folder.mkdir(parents=True)
    mod_name = "[Gameplay] 21 new Legendary Items (Lion053)"
    archive = folder / "gameplay.zip"
    _make_zip(
        archive,
        {
            f"{mod_name}/modinfo.json": b'{"ModName":"Test"}',
            f"{mod_name}/README.md": b"# readme",
            f"{mod_name}/data/ui/icon.png": b"\x89PNG",
            f"{mod_name}/zzz_wysiwyg/modinfo.json": b'{"ModName":"wys"}',
            f"{mod_name}/zzz_wysiwyg/data/config/export/main/asset/assets.xml": b"<A/>",
        },
    )
    _write_meta(folder, mid=mod_id, title="21 Legendary Items")
    install = tmp_path / "AnnoInstall"
    (install / "mods").mkdir(parents=True)
    db.update_game_deploy_config(
        ANNO_1800_APP_ID,
        name="Anno 1800",
        install_path=str(install),
        mod_path="",
        deploy_type="folder_copy",
    )
    db.upsert_mod(
        ModMetadata(
            published_file_id=mod_id,
            title="21 Legendary Items",
            app_id=ANNO_1800_APP_ID,
            game_name="Anno 1800",
            managed_path=str(folder),
        )
    )
    db.update_mod_identity_fields(
        mod_id,
        content_status=CONTENT_HEALTHY,
        folder_present=True,
        last_known_path=str(folder),
    )
    db.set_mod_files(
        mod_id,
        ModFilesBundle(
            files=[
                ModFileEntry(
                    id="f1",
                    name="Main",
                    filename="gameplay.zip",
                    path="gameplay.zip",
                    type=FILE_TYPE_MAIN,
                    enabled=True,
                    selected_for_deploy=True,
                )
            ]
        ),
    )

    dep = ModDeployer(library_root=library, db=db)
    out = dep.deploy_mod(mod_id)
    assert out.get("success") is True, out

    root = install / "mods" / mod_name
    assert (root / "modinfo.json").is_file()
    assert (root / "README.md").is_file()
    assert (root / "data" / "ui" / "icon.png").stat().st_size > 0
    assert (root / "zzz_wysiwyg" / "modinfo.json").is_file()
    assert (
        root / "zzz_wysiwyg" / "data" / "config" / "export" / "main" / "asset" / "assets.xml"
    ).is_file()

    info = db.get_mod_deploy_info(mod_id)
    assert info is not None
    assert info.deploy_status == DEPLOY_STATUS_DEPLOYED


def test_manifest_entries_for_paths_tag_archive_type(tmp_path: Path) -> None:
    mods_root = tmp_path / "mods"
    mods_root.mkdir()
    f1 = mods_root / "a.txt"
    f1.write_text("a", encoding="utf-8")
    archive = tmp_path / "mod.zip"
    archive.write_bytes(b"zip")
    entries = _manifest_entries_for_paths(mods_root, [f1], source=str(archive))
    assert len(entries) == 1
    assert entries[0].type == "archive"
    assert entries[0].source == str(archive)
    assert entries[0].target == str(f1.resolve())


def _folder_ctx(source: Path, target_parent: Path):
    from services.deploy_rules.base import DeployContext
    from core.db_manager import GameDeployConfig

    cfg = GameDeployConfig(
        app_id=1,
        name="Game",
        install_path="",
        mod_path=str(target_parent),
        deploy_type="folder_copy",
    )
    return DeployContext(
        mod_id="1",
        source=source,
        app_id=1,
        config=cfg,
        deploy_type="folder_copy",
        managed_path=source,
    )
