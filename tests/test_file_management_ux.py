"""Phase E3 — File Management UX helpers, batch selection, Steam vs Nexus grouping."""

from __future__ import annotations

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
    FILE_TYPE_MAIN,
    FILE_TYPE_OPTIONAL,
    FILE_TYPE_PATCH,
    PLATFORM_GITHUB,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    SOURCE_TYPE_GITHUB,
    SOURCE_TYPE_NEXUS,
    SOURCE_TYPE_STEAM,
    ModFileEntry,
    ModFilesBundle,
)
from services.deploy import resolve_deploy_sources
from services.mod_files import ModFileManager
from ui.mod_files_ux import (
    GROUP_FILES,
    GROUP_GITHUB_OTHER,
    GROUP_GITHUB_RELEASE,
    GROUP_NEXUS_MAIN,
    GROUP_NEXUS_MISC,
    GROUP_NEXUS_OPTIONAL,
    GROUP_STEAM_FILES,
    file_group_key,
    file_group_title,
    file_primary_label,
    file_secondary_label,
    file_tooltip,
    files_summary_lines,
    group_file_entries,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "file_ux.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _nexus_pack() -> list[ModFileEntry]:
    return [
        ModFileEntry(
            id="main",
            name="Body",
            display_name="Body Main",
            filename="body.zip",
            path="body.zip",
            type=FILE_TYPE_MAIN,
            source_type=SOURCE_TYPE_NEXUS,
            file_role=FILE_ROLE_NEXUS_MAIN,
            enabled=True,
        ),
        ModFileEntry(
            id="opt",
            name="Hat",
            filename="hat.zip",
            path="hat.zip",
            type=FILE_TYPE_OPTIONAL,
            source_type=SOURCE_TYPE_NEXUS,
            file_role=FILE_ROLE_NEXUS_OPTIONAL,
            enabled=True,
            selected_for_deploy=True,
        ),
        ModFileEntry(
            id="misc",
            name="Readme pack",
            filename="misc.zip",
            path="misc.zip",
            type=FILE_TYPE_OPTIONAL,
            source_type=SOURCE_TYPE_NEXUS,
            file_role=FILE_ROLE_NEXUS_MISC,
            enabled=False,
        ),
        ModFileEntry(
            id="old",
            name="Legacy",
            filename="old.zip",
            path="old.zip",
            type=FILE_TYPE_OPTIONAL,
            source_type=SOURCE_TYPE_NEXUS,
            file_role=FILE_ROLE_NEXUS_OLD,
            enabled=False,
        ),
        ModFileEntry(
            id="patch",
            name="Hotfix",
            filename="patch.zip",
            path="patch.zip",
            type=FILE_TYPE_PATCH,
            source_type=SOURCE_TYPE_NEXUS,
            file_role=FILE_ROLE_NEXUS_OPTIONAL,
            enabled=True,
            selected_for_deploy=True,
        ),
    ]


def test_display_helpers_hide_ids_from_primary() -> None:
    entry = ModFileEntry(
        id="uuid-should-not-appear",
        name="Stored Name",
        display_name="Pretty Title",
        filename="real_file.pak",
        path="nested/real_file.pak",
        source_type=SOURCE_TYPE_NEXUS,
        file_role=FILE_ROLE_NEXUS_MAIN,
        metadata={"version": "1.2.3", "file_id": 99},
    )
    assert file_primary_label(entry) == "Pretty Title"
    assert file_secondary_label(entry) == "Nexus · Main File"
    tip = file_tooltip(entry)
    assert "real_file.pak" in tip
    assert "nested/real_file.pak" in tip
    assert "nexus_main" in tip
    assert "1.2.3" in tip
    assert "uuid-should-not-appear" in tip
    assert "uuid-should-not-appear" not in file_primary_label(entry)
    assert "nested/" not in file_primary_label(entry)
    assert "nested/" not in file_secondary_label(entry)

    no_display = ModFileEntry(
        id="x",
        name="",
        display_name="",
        filename="fallback.bin",
        path="fallback.bin",
        source_type=SOURCE_TYPE_GITHUB,
        file_role=FILE_ROLE_GITHUB_RELEASE_ASSET,
    )
    assert file_primary_label(no_display) == "fallback.bin"
    assert "GitHub · Release Asset" == file_secondary_label(no_display)


def test_nexus_grouping_order() -> None:
    files = _nexus_pack()
    keys = [file_group_key(f, PLATFORM_NEXUS) for f in files]
    assert keys[0] == GROUP_NEXUS_MAIN
    assert keys[1] == GROUP_NEXUS_OPTIONAL
    assert GROUP_NEXUS_OPTIONAL in keys
    groups = group_file_entries(files, PLATFORM_NEXUS)
    titles = [t for t, _ in groups]
    assert titles == ["Main Files", "Optional Files", "Misc", "Old"]
    assert file_group_title(GROUP_NEXUS_MISC) == "Misc"


def test_github_release_and_other_grouping() -> None:
    files = [
        ModFileEntry(
            id="a",
            filename="mod.zip",
            path="mod.zip",
            source_type=SOURCE_TYPE_GITHUB,
            file_role=FILE_ROLE_GITHUB_RELEASE_ASSET,
            type=FILE_TYPE_MAIN,
        ),
        ModFileEntry(
            id="b",
            filename="mod-dev.zip",
            path="mod-dev.zip",
            source_type=SOURCE_TYPE_GITHUB,
            file_role=FILE_ROLE_GITHUB_DEVELOPER_BUILD,
            type=FILE_TYPE_OPTIONAL,
        ),
        ModFileEntry(
            id="c",
            filename="src.zip",
            path="src.zip",
            source_type=SOURCE_TYPE_GITHUB,
            file_role=FILE_ROLE_GITHUB_SOURCE_ARCHIVE,
            type=FILE_TYPE_OPTIONAL,
        ),
    ]
    assert file_group_key(files[0], PLATFORM_GITHUB) == GROUP_GITHUB_RELEASE
    assert file_group_key(files[1], PLATFORM_GITHUB) == GROUP_GITHUB_OTHER
    assert file_group_key(files[2], PLATFORM_GITHUB) == GROUP_GITHUB_OTHER
    titles = [t for t, _ in group_file_entries(files, PLATFORM_GITHUB)]
    assert titles == ["Release Assets", "Other"]


def test_steam_does_not_use_nexus_grouping() -> None:
    files = [
        ModFileEntry(
            id="1",
            filename="a.pak",
            path="a.pak",
            source_type=SOURCE_TYPE_STEAM,
            file_role=FILE_ROLE_STEAM_CONTENT,
            type=FILE_TYPE_MAIN,
        ),
        ModFileEntry(
            id="2",
            filename="b.pak",
            path="b.pak",
            source_type=SOURCE_TYPE_STEAM,
            file_role=FILE_ROLE_STEAM_CONTENT,
            type=FILE_TYPE_OPTIONAL,
        ),
    ]
    assert all(file_group_key(f, PLATFORM_STEAM) == GROUP_STEAM_FILES for f in files)
    titles = [t for t, _ in group_file_entries(files, PLATFORM_STEAM)]
    assert titles == ["Workshop Files"]
    assert "Main Files" not in titles
    assert "Optional Files" not in titles


def test_missing_role_falls_back_to_files() -> None:
    entry = ModFileEntry(
        id="z",
        filename="loose.bin",
        path="loose.bin",
        source_type="legacy",
        file_role="",
        type=FILE_TYPE_OPTIONAL,
    )
    # Force empty role after construction (legacy soft migration may invent none).
    entry.file_role = ""
    assert file_group_key(entry, "unknown") == GROUP_FILES
    assert file_group_title(GROUP_FILES) == "Files"


def test_summary_ready_and_empty() -> None:
    summary, status = files_summary_lines(2, 5)
    assert summary == "Selected 2/5"
    assert status == "Ready"
    summary, status = files_summary_lines(0, 5)
    assert "No files selected" in summary
    assert status == "Deployment Empty"
    summary, status = files_summary_lines(1, 1, legacy_workshop=True)
    assert "Workshop Content" in summary
    assert "Full folder deploy" in summary
    assert status == "Ready"


def test_enabled_save_via_manager(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="9401", title="Nexus"))
    mgr = ModFileManager(db)
    mgr.replace_all("9401", _nexus_pack())
    updated = mgr.set_file_selection("9401", "opt", False)
    assert updated is not None
    assert updated.enabled is False
    assert updated.selected_for_deploy is False
    again = {f.id: f for f in mgr.get_files("9401")}
    assert again["opt"].enabled is False
    assert again["opt"].selected_for_deploy is False


def test_batch_select_all_main_only_clear_reset(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="9402", title="Nexus"))
    mgr = ModFileManager(db)
    mgr.replace_all("9402", _nexus_pack())

    mgr.set_all_selection("9402", True)
    assert all(f.selected_for_deploy and f.enabled for f in mgr.get_files("9402"))

    mgr.select_main_only("9402")
    files = {f.id: f for f in mgr.get_files("9402")}
    assert files["main"].selected_for_deploy is True
    assert files["opt"].selected_for_deploy is False
    assert files["misc"].selected_for_deploy is False
    assert files["old"].selected_for_deploy is False
    assert files["patch"].selected_for_deploy is False

    mgr.set_all_selection("9402", True)
    mgr.clear_optional_selection("9402")
    files = {f.id: f for f in mgr.get_files("9402")}
    assert files["main"].selected_for_deploy is True
    assert files["opt"].selected_for_deploy is False
    assert files["misc"].selected_for_deploy is False
    assert files["patch"].selected_for_deploy is False

    # Flip selection then reset to importer defaults (main on, optional off).
    mgr.set_all_selection("9402", False)
    mgr.reset_default_selection("9402")
    files = {f.id: f for f in mgr.get_files("9402")}
    assert files["main"].selected_for_deploy is True
    assert files["opt"].selected_for_deploy is False
    assert files["misc"].selected_for_deploy is False


def test_legacy_empty_bundle_deploy_whole_mod(db: DatabaseManager, tmp_path: Path) -> None:
    db.upsert_mod(ModMetadata(published_file_id="9403", title="Steam"))
    source = tmp_path / "steam_mod"
    source.mkdir()
    (source / "content.pak").write_bytes(b"x")
    assert db.get_mod_files("9403").files == []
    assert resolve_deploy_sources("9403", source, db=db) is None


def test_github_main_only_keeps_type_main(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id="9404", title="GH"))
    mgr = ModFileManager(db)
    mgr.replace_all(
        "9404",
        [
            ModFileEntry(
                id="r1",
                filename="release.zip",
                path="release.zip",
                source_type=SOURCE_TYPE_GITHUB,
                file_role=FILE_ROLE_GITHUB_RELEASE_ASSET,
                type=FILE_TYPE_MAIN,
                enabled=True,
            ),
            ModFileEntry(
                id="r2",
                filename="release2.zip",
                path="release2.zip",
                source_type=SOURCE_TYPE_GITHUB,
                file_role=FILE_ROLE_GITHUB_RELEASE_ASSET,
                type=FILE_TYPE_OPTIONAL,
                enabled=True,
                selected_for_deploy=True,
            ),
            ModFileEntry(
                id="dev",
                filename="dev.zip",
                path="dev.zip",
                source_type=SOURCE_TYPE_GITHUB,
                file_role=FILE_ROLE_GITHUB_DEVELOPER_BUILD,
                type=FILE_TYPE_OPTIONAL,
                enabled=True,
                selected_for_deploy=True,
            ),
        ],
    )
    mgr.select_main_only("9404")
    files = {f.id: f for f in mgr.get_files("9404")}
    assert files["r1"].selected_for_deploy is True
    assert files["r2"].selected_for_deploy is False
    assert files["dev"].selected_for_deploy is False
