"""Phase E2 — selected_for_deploy sync, importers, deploy resolve compatibility."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from core.mod_platform import (
    FILE_ROLE_GITHUB_DEVELOPER_BUILD,
    FILE_ROLE_GITHUB_RELEASE_ASSET,
    FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
    FILE_ROLE_NEXUS_MAIN,
    FILE_ROLE_NEXUS_MISC,
    FILE_ROLE_NEXUS_OLD,
    FILE_ROLE_NEXUS_OPTIONAL,
    FILE_ROLE_STEAM_CONTENT,
    FILE_ROLE_UNKNOWN,
    FILE_TYPE_MAIN,
    FILE_TYPE_OPTIONAL,
    SOURCE_TYPE_GITHUB,
    SOURCE_TYPE_NEXUS,
    SOURCE_TYPE_STEAM,
    ModFileEntry,
    ModFilesBundle,
    is_entry_selected_for_deploy,
)
from services.deploy import resolve_deploy_sources
from services.importers.github import GithubImporter
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from services.importers.source_files import (
    apply_steam_file_semantics,
    build_github_mod_files,
    build_nexus_mod_files,
    build_steam_workshop_bundle,
)
from services.importers.steam import SteamImporter
from services.mod_files import ModFileManager


PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "source_sel.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_old_json_enabled_maps_to_selected() -> None:
    entry = ModFileEntry.from_dict(
        {
            "id": "legacy",
            "name": "Main",
            "filename": "a.pak",
            "path": "a.pak",
            "type": "main",
            "enabled": False,
        }
    )
    assert entry.enabled is False
    assert entry.selected_for_deploy is False
    data = entry.to_dict()
    assert data["enabled"] is False
    assert data["selected_for_deploy"] is False


def test_selected_for_deploy_prefers_over_enabled_on_load() -> None:
    entry = ModFileEntry.from_dict(
        {
            "path": "x.pak",
            "filename": "x.pak",
            "enabled": False,
            "selected_for_deploy": True,
        }
    )
    assert entry.selected_for_deploy is True
    assert entry.enabled is True


def test_manager_set_file_selection_syncs_both(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="9101", title="Pack"))
    mgr = ModFileManager(db)
    entry = mgr.add_file(
        "9101",
        {
            "name": "Main",
            "filename": "main.pak",
            "path": "main.pak",
            "type": "main",
            "enabled": True,
        },
    )
    updated = mgr.set_file_selection("9101", entry.id, False)
    assert updated is not None
    assert updated.selected_for_deploy is False
    assert updated.enabled is False
    again = mgr.get_files("9101")[0]
    assert again.selected_for_deploy is False
    assert again.enabled is False

    via_enabled = mgr.set_file_enabled("9101", entry.id, True)
    assert via_enabled is not None
    assert via_enabled.selected_for_deploy is True
    assert via_enabled.enabled is True


def test_steam_empty_bundle_whole_mod(db: DatabaseManager) -> None:
    result = SteamImporter(db=db).import_mod(
        workshop_id="3761838546", title="Steam Mod"
    )
    assert result.success
    bundle = db.get_mod_files("3761838546")
    assert bundle.files == []
    assert build_steam_workshop_bundle(whole_mod=True).files == []


def test_steam_file_entries_tagged_workshop_content() -> None:
    bundle = apply_steam_file_semantics(
        ModFilesBundle(
            files=[
                ModFileEntry(
                    name="Main File",
                    filename="content.pak",
                    path="content.pak",
                    type=FILE_TYPE_MAIN,
                    enabled=True,
                )
            ]
        )
    )
    assert len(bundle.files) == 1
    entry = bundle.files[0]
    assert entry.source_type == SOURCE_TYPE_STEAM
    assert entry.file_role == FILE_ROLE_STEAM_CONTENT
    assert entry.selected_for_deploy is True
    assert entry.enabled is True
    assert entry.name == "Workshop Content"


def test_nexus_roles_and_defaults(tmp_path: Path, db: DatabaseManager) -> None:
    folder = tmp_path / "CharacterA"
    folder.mkdir()
    (folder / "Main.pak").write_bytes(b"M")
    (folder / "HatAddon.pak").write_bytes(b"H")

    scanned = build_nexus_mod_files(folder)
    assert len(scanned.files) == 2
    # Folder scan no longer forces Main/Optional roles — user assigns later.
    assert all(f.file_role == FILE_ROLE_UNKNOWN for f in scanned.files)
    assert all(f.source_type == SOURCE_TYPE_NEXUS for f in scanned.files)
    assert all(f.display_name for f in scanned.files)

    # Explicit entries without file_role stay unknown (no type/extension guess).
    no_role = build_nexus_mod_files(
        file_entries=[
            {"filename": "x.zip", "path": "x.zip", "type": "main"},
            {"filename": "y.zip", "path": "y.zip", "type": "optional"},
        ]
    )
    assert all(f.file_role == FILE_ROLE_UNKNOWN for f in no_role.files)

    explicit = build_nexus_mod_files(
        file_entries=[
            {
                "filename": "a.zip",
                "path": "a.zip",
                "file_role": "main_file",
            },
            {
                "filename": "b.zip",
                "path": "b.zip",
                "file_role": "optional_file",
            },
            {
                "filename": "c.zip",
                "path": "c.zip",
                "file_role": "misc_file",
            },
            {
                "filename": "d.zip",
                "path": "d.zip",
                "file_role": "old_file",
            },
        ]
    )
    roles = {f.file_role for f in explicit.files}
    assert roles == {
        FILE_ROLE_NEXUS_MAIN,
        FILE_ROLE_NEXUS_OPTIONAL,
        FILE_ROLE_NEXUS_MISC,
        FILE_ROLE_NEXUS_OLD,
    }
    by_role = {f.file_role: f for f in explicit.files}
    assert by_role[FILE_ROLE_NEXUS_MAIN].selected_for_deploy is True
    assert by_role[FILE_ROLE_NEXUS_OPTIONAL].selected_for_deploy is False
    assert by_role[FILE_ROLE_NEXUS_MISC].selected_for_deploy is False
    assert by_role[FILE_ROLE_NEXUS_OLD].selected_for_deploy is False

    result = NexusImporter(db=db).import_mod(
        source_folder=folder,
        title="Char",
        nexus_url="https://www.nexusmods.com/game/mods/77",
        nexus_id="77",
        context=PALWORLD,
        file_entries=[
            {"filename": "main.zip", "path": "main.zip", "file_role": "nexus_main"},
            {
                "filename": "opt.zip",
                "path": "opt.zip",
                "file_role": "nexus_optional",
            },
        ],
    )
    assert result.success
    files = db.get_mod_files(result.mod_id).files
    assert len(files) == 2
    assert all(f.source_type == SOURCE_TYPE_NEXUS for f in files)
    assert sum(1 for f in files if f.selected_for_deploy) == 1


def test_github_roles_first_release_selected(tmp_path: Path, db: DatabaseManager) -> None:
    folder = tmp_path / "repo"
    folder.mkdir()
    (folder / "mod-release.zip").write_bytes(b"R")
    (folder / "mod-nightly-dev.zip").write_bytes(b"D")
    (folder / "mod-source.zip").write_bytes(b"S")

    bundle = build_github_mod_files(folder)
    assert len(bundle.files) == 3
    # Folder scan does not infer release/dev/source — role stays unknown.
    assert all(f.file_role == FILE_ROLE_UNKNOWN for f in bundle.files)
    assert all(f.source_type == SOURCE_TYPE_GITHUB for f in bundle.files)

    explicit = build_github_mod_files(
        file_entries=[
            {
                "filename": "a.zip",
                "path": "a.zip",
                "file_role": "github_release_asset",
            },
            {
                "filename": "b.zip",
                "path": "b.zip",
                "file_role": "developer_build",
            },
            {
                "filename": "c.zip",
                "path": "c.zip",
                "file_role": "github_source_archive",
            },
            {
                "filename": "d.zip",
                "path": "d.zip",
                "file_role": "release_asset",
            },
        ]
    )
    selected = [f for f in explicit.files if f.selected_for_deploy]
    assert len(selected) == 1
    assert selected[0].filename == "a.zip"
    assert selected[0].file_role == FILE_ROLE_GITHUB_RELEASE_ASSET

    result = GithubImporter(db=db).import_mod(
        github_url="https://github.com/user/project",
        source_folder=folder,
        title="Repo",
        context=PALWORLD,
    )
    assert result.success
    files = db.get_mod_files(result.mod_id).files
    assert all(f.source_type == SOURCE_TYPE_GITHUB for f in files)
    assert sum(1 for f in files if is_entry_selected_for_deploy(f)) == 1


def test_deploy_prefers_selected_for_deploy(db: DatabaseManager, tmp_path: Path) -> None:
    source = tmp_path / "mod"
    source.mkdir()
    (source / "main.pak").write_bytes(b"M")
    (source / "hat.pak").write_bytes(b"H")
    db.upsert_mod(ModMetadata(published_file_id="9201", title="Multi"))

    # selected_for_deploy False even if we somehow had enabled True historically —
    # from_dict syncs both; construct via JSON blob that only has selected.
    blob = {
        "files": [
            {
                "id": "a",
                "name": "Main",
                "filename": "main.pak",
                "path": "main.pak",
                "type": "main",
                "selected_for_deploy": True,
            },
            {
                "id": "b",
                "name": "Hat",
                "filename": "hat.pak",
                "path": "hat.pak",
                "type": "optional",
                "selected_for_deploy": False,
            },
        ]
    }
    db.set_mod_files("9201", ModFilesBundle.from_json(json.dumps(blob)))
    allowed = resolve_deploy_sources("9201", source, db=db)
    assert allowed is not None
    assert "main.pak" in allowed
    assert "hat.pak" not in allowed


def test_deploy_falls_back_to_enabled(db: DatabaseManager, tmp_path: Path) -> None:
    source = tmp_path / "mod"
    source.mkdir()
    (source / "main.pak").write_bytes(b"M")
    (source / "hat.pak").write_bytes(b"H")
    db.upsert_mod(ModMetadata(published_file_id="9202", title="Legacy"))
    # Old JSON: only enabled (no selected_for_deploy key in stored dict before load).
    # After from_dict both are set; resolve still treats selection correctly.
    db.set_mod_files(
        "9202",
        ModFilesBundle(
            files=[
                ModFileEntry(
                    id="a",
                    name="Main",
                    filename="main.pak",
                    path="main.pak",
                    type=FILE_TYPE_MAIN,
                    enabled=True,
                ),
                ModFileEntry(
                    id="b",
                    name="Hat",
                    filename="hat.pak",
                    path="hat.pak",
                    type=FILE_TYPE_OPTIONAL,
                    enabled=False,
                ),
            ]
        ),
    )
    # Simulate a partially-hydrated entry missing selected_for_deploy (None).
    bundle = db.get_mod_files("9202")
    bundle.files[0].selected_for_deploy = None  # type: ignore[assignment]
    bundle.files[0].enabled = True
    bundle.files[1].selected_for_deploy = None  # type: ignore[assignment]
    bundle.files[1].enabled = False
    db.set_mod_files("9202", bundle)
    # set_mod_files → to_dict → from_dict will re-sync; test helper path directly:
    from services.deploy import resolve_deploy_sources as resolve

    # Direct entry fallback without going through to_dict sync:
    orphan_on = ModFileEntry.__new__(ModFileEntry)
    orphan_on.path = "main.pak"
    orphan_on.filename = "main.pak"
    orphan_on.enabled = True
    orphan_on.selected_for_deploy = None
    orphan_off = ModFileEntry.__new__(ModFileEntry)
    orphan_off.path = "hat.pak"
    orphan_off.filename = "hat.pak"
    orphan_off.enabled = False
    orphan_off.selected_for_deploy = None
    assert is_entry_selected_for_deploy(orphan_on) is True
    assert is_entry_selected_for_deploy(orphan_off) is False

    allowed = resolve("9202", source, db=db)
    assert allowed is not None
    assert "main.pak" in allowed
    assert "hat.pak" not in allowed


def test_deploy_empty_bundle_still_none(db: DatabaseManager, tmp_path: Path) -> None:
    db.upsert_mod(ModMetadata(published_file_id="9203", title="Steam"))
    source = tmp_path / "mod"
    source.mkdir()
    assert resolve_deploy_sources("9203", source, db=db) is None


def test_reset_default_and_clear_optional(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="9301", title="NexusPack"))
    mgr = ModFileManager(db)
    mgr.replace_all(
        "9301",
        [
            ModFileEntry(
                id="m",
                name="Main",
                path="m.zip",
                filename="m.zip",
                source_type=SOURCE_TYPE_NEXUS,
                file_role=FILE_ROLE_NEXUS_MAIN,
                enabled=False,
                selected_for_deploy=False,
            ),
            ModFileEntry(
                id="o",
                name="Opt",
                path="o.zip",
                filename="o.zip",
                source_type=SOURCE_TYPE_NEXUS,
                file_role=FILE_ROLE_NEXUS_OPTIONAL,
                enabled=True,
                selected_for_deploy=True,
            ),
        ],
    )
    mgr.reset_default_selection("9301")
    files = {f.id: f for f in mgr.get_files("9301")}
    assert files["m"].selected_for_deploy is True
    assert files["o"].selected_for_deploy is False

    mgr.set_all_selection("9301", True)
    assert all(f.selected_for_deploy for f in mgr.get_files("9301"))
    mgr.clear_optional_selection("9301")
    files = {f.id: f for f in mgr.get_files("9301")}
    assert files["m"].selected_for_deploy is True
    assert files["o"].selected_for_deploy is False
