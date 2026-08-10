"""Phase E1 — source-aware ModFileEntry model (backward-compatible JSON)."""

from __future__ import annotations

import json

from core.mod_platform import (
    FILE_ROLE_GITHUB_RELEASE_ASSET,
    FILE_ROLE_NEXUS_MAIN,
    FILE_ROLE_NEXUS_OPTIONAL,
    FILE_ROLE_STEAM_CONTENT,
    FILE_TYPE_MAIN,
    FILE_TYPE_OPTIONAL,
    SOURCE_TYPE_GITHUB,
    SOURCE_TYPE_LEGACY,
    SOURCE_TYPE_NEXUS,
    SOURCE_TYPE_STEAM,
    ModFileEntry,
    ModFilesBundle,
    map_file_type_to_role,
    normalize_source_type,
)


def test_old_json_decode_defaults() -> None:
    raw = {
        "id": "legacy-1",
        "name": "Main File",
        "filename": "CharacterA.zip",
        "path": "CharacterA.zip",
        "type": "main",
        "enabled": True,
    }
    entry = ModFileEntry.from_dict(raw)
    assert entry.id == "legacy-1"
    assert entry.name == "Main File"
    assert entry.filename == "CharacterA.zip"
    assert entry.path == "CharacterA.zip"
    assert entry.type == FILE_TYPE_MAIN
    assert entry.enabled is True
    assert entry.selected_for_deploy is True  # mirrored from enabled
    assert entry.source_type == SOURCE_TYPE_LEGACY
    assert entry.file_role == ""  # legacy source → no invented platform role
    assert entry.display_name == "Main File"
    assert entry.metadata == {}


def test_old_json_bundle_roundtrip_keeps_enabled() -> None:
    blob = json.dumps(
        {
            "files": [
                {
                    "id": "a",
                    "name": "Main",
                    "filename": "a.pak",
                    "path": "a.pak",
                    "type": "main",
                    "enabled": True,
                },
                {
                    "id": "b",
                    "name": "Hat",
                    "filename": "hat.pak",
                    "path": "hat.pak",
                    "type": "optional",
                    "enabled": False,
                },
            ]
        }
    )
    bundle = ModFilesBundle.from_json(blob)
    assert len(bundle.files) == 2
    assert [f.id for f in bundle.enabled_files()] == ["a"]
    assert bundle.files[1].enabled is False
    assert all(f.source_type == SOURCE_TYPE_LEGACY for f in bundle.files)


def test_new_json_encode_decode_roundtrip() -> None:
    entry = ModFileEntry(
        id="nx-1",
        name="Character Pack",
        filename="CharacterA.zip",
        path="files/CharacterA.zip",
        type=FILE_TYPE_MAIN,
        enabled=True,
        source_type=SOURCE_TYPE_NEXUS,
        file_role=FILE_ROLE_NEXUS_MAIN,
        display_name="Main File",
        metadata={"nexus_file_id": 99, "category": "main"},
    )
    data = entry.to_dict()
    assert data["source_type"] == SOURCE_TYPE_NEXUS
    assert data["file_role"] == FILE_ROLE_NEXUS_MAIN
    assert data["display_name"] == "Main File"
    assert data["enabled"] is True
    assert data["selected_for_deploy"] is True
    assert data["metadata"]["nexus_file_id"] == 99

    again = ModFileEntry.from_dict(data)
    assert again.id == entry.id
    assert again.source_type == entry.source_type
    assert again.file_role == entry.file_role
    assert again.display_name == entry.display_name
    assert again.metadata == entry.metadata
    assert again.enabled is True
    assert again.selected_for_deploy is True
    assert again.type == FILE_TYPE_MAIN


def test_source_type_caller_provided_wins() -> None:
    entry = ModFileEntry.from_dict(
        {
            "name": "Workshop",
            "path": "content.pak",
            "type": "main",
            "source_type": "steam",
        }
    )
    assert entry.source_type == SOURCE_TYPE_STEAM
    assert entry.file_role == FILE_ROLE_STEAM_CONTENT


def test_source_type_default_legacy() -> None:
    assert normalize_source_type(None) == SOURCE_TYPE_LEGACY
    assert normalize_source_type("") == SOURCE_TYPE_LEGACY
    assert normalize_source_type("unknown") == SOURCE_TYPE_LEGACY
    entry = ModFileEntry(name="x", path="x.pak")
    assert entry.source_type == SOURCE_TYPE_LEGACY


def test_map_file_type_to_role() -> None:
    assert map_file_type_to_role(FILE_TYPE_MAIN, SOURCE_TYPE_LEGACY) == ""
    assert map_file_type_to_role(FILE_TYPE_MAIN, SOURCE_TYPE_STEAM) == FILE_ROLE_STEAM_CONTENT
    assert map_file_type_to_role(FILE_TYPE_MAIN, SOURCE_TYPE_NEXUS) == FILE_ROLE_NEXUS_MAIN
    assert (
        map_file_type_to_role(FILE_TYPE_OPTIONAL, SOURCE_TYPE_NEXUS)
        == FILE_ROLE_NEXUS_OPTIONAL
    )
    assert (
        map_file_type_to_role(FILE_TYPE_MAIN, SOURCE_TYPE_GITHUB)
        == FILE_ROLE_GITHUB_RELEASE_ASSET
    )


def test_file_role_inferred_when_source_known() -> None:
    entry = ModFileEntry(
        name="Hat",
        path="hat.zip",
        type=FILE_TYPE_OPTIONAL,
        source_type=SOURCE_TYPE_NEXUS,
    )
    assert entry.file_role == FILE_ROLE_NEXUS_OPTIONAL


def test_explicit_file_role_preserved() -> None:
    entry = ModFileEntry.from_dict(
        {
            "name": "Old",
            "path": "old.zip",
            "type": "optional",
            "source_type": SOURCE_TYPE_NEXUS,
            "file_role": "nexus_old",
        }
    )
    assert entry.file_role == "nexus_old"
    assert entry.type == FILE_TYPE_OPTIONAL


def test_display_name_alias_from_name() -> None:
    entry = ModFileEntry.from_dict(
        {"name": "Shown", "filename": "f.pak", "path": "f.pak", "type": "main"}
    )
    assert entry.display_name == "Shown"
    # display_name-only payload still fills name
    entry2 = ModFileEntry.from_dict(
        {"display_name": "Only Display", "filename": "g.pak", "path": "g.pak"}
    )
    assert entry2.display_name == "Only Display"
    assert entry2.name == "Only Display"


def test_enabled_serializes_and_filters() -> None:
    bundle = ModFilesBundle(
        files=[
            ModFileEntry(
                id="on",
                name="On",
                path="on.pak",
                enabled=True,
                source_type=SOURCE_TYPE_GITHUB,
                file_role=FILE_ROLE_GITHUB_RELEASE_ASSET,
            ),
            ModFileEntry(
                id="off",
                name="Off",
                path="off.pak",
                enabled=False,
                source_type=SOURCE_TYPE_GITHUB,
            ),
        ]
    )
    dumped = bundle.to_dict()
    assert dumped["files"][0]["enabled"] is True
    assert dumped["files"][1]["enabled"] is False
    restored = ModFilesBundle.from_dict(dumped)
    assert [f.id for f in restored.enabled_files()] == ["on"]
    assert restored.files[1].enabled is False


def test_unknown_fields_ignored() -> None:
    entry = ModFileEntry.from_dict(
        {
            "id": "u1",
            "name": "X",
            "filename": "x.pak",
            "path": "x.pak",
            "type": "main",
            "enabled": False,
            "selected_for_deploy": True,  # E2: selected wins when present
            "extra_future": {"a": 1},
            "metadata": {"keep": True},
        }
    )
    assert entry.selected_for_deploy is True
    assert entry.enabled is True  # kept in sync with selected_for_deploy
    assert entry.metadata == {"keep": True}
    assert entry.source_type == SOURCE_TYPE_LEGACY
    data = entry.to_dict()
    assert data["selected_for_deploy"] is True
    assert data["enabled"] is True
    assert "extra_future" not in data


def test_type_field_unchanged_for_consumers() -> None:
    """Coarse type remains the contract for existing UI/deploy code paths."""
    entry = ModFileEntry.from_dict(
        {
            "name": "Patch",
            "path": "p.zip",
            "type": "patch",
            "enabled": True,
            "source_type": SOURCE_TYPE_NEXUS,
        }
    )
    assert entry.type == "patch"
    assert entry.to_dict()["type"] == "patch"
    assert entry.file_role == FILE_ROLE_NEXUS_OPTIONAL
