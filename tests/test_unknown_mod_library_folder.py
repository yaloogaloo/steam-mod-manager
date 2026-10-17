"""Library folder names must never persist ``Unknown Mod …`` placeholders."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import (
    ModMetadata,
    is_placeholder_library_folder_name,
    is_unknown_mod_title,
    library_mod_folder_fallback,
)
from services.file_ops import ModFileManager
from services.importers.materialize import materialize_imported_mod


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager(tmp_path / "unknown_folder.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def test_library_mod_folder_fallback_is_clean() -> None:
    assert library_mod_folder_fallback("872296228") == "Mod_872296228"
    assert is_placeholder_library_folder_name(
        "Mod_872296228", published_file_id="872296228"
    )
    assert is_unknown_mod_title("Unknown Mod 872296228", published_file_id="872296228")
    assert not is_unknown_mod_title("Mod_872296228", published_file_id="872296228")


def test_mod_folder_name_never_emits_unknown_mod() -> None:
    mgr = ModFileManager(Path("unused"))
    meta = ModMetadata(published_file_id="872296228", title="")
    name = mgr.mod_folder_name(meta)
    assert name == "Mod_872296228"
    assert "Unknown" not in name
    assert "unknown" not in name.lower()

    meta2 = ModMetadata(
        published_file_id="872296228", title="Unknown_Mod_872296228"
    )
    assert mgr.mod_folder_name(meta2) == "Mod_872296228"

    meta3 = ModMetadata(
        published_file_id="872296228", title="Unknown Mod 872296228"
    )
    assert mgr.mod_folder_name(meta3) == "Mod_872296228"

    meta4 = ModMetadata(published_file_id="872296228", title="Real Workshop Title")
    assert mgr.mod_folder_name(meta4) == "Real Workshop Title"


def test_materialize_missing_title_does_not_create_unknown_mod_folder(
    tmp_path: Path, db: DatabaseManager
) -> None:
    """
    Import / materialize with workspace_id and empty title must not create
    ``Unknown Mod <id>`` under the managed library.
    """
    library = tmp_path / "library"
    library.mkdir()
    workspace_id = "872296228"
    src = tmp_path / "src_payload"
    src.mkdir()
    (src / "payload.txt").write_text("x", encoding="utf-8")

    dest = materialize_imported_mod(
        library_root=library,
        mod_id=workspace_id,
        title="",
        game_name="文明Ⅵ",
        source_folder=src,
        allow_invalid_game_name=True,
    )

    assert dest.is_dir()
    assert dest.name == "Mod_872296228"
    assert "Unknown" not in dest.name
    # No sibling Unknown Mod folders created.
    game_dir = dest.parent
    unknown_leaves = [
        p.name
        for p in game_dir.iterdir()
        if p.is_dir() and "Unknown" in p.name
    ]
    assert unknown_leaves == []


def test_materialize_unknown_title_string_still_uses_mod_fallback(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    workspace_id = "872296228"
    dest = materialize_imported_mod(
        library_root=library,
        mod_id=workspace_id,
        title=f"Unknown Mod {workspace_id}",
        game_name="Civilization VI",
        source_folder=None,
        allow_invalid_game_name=True,
    )
    assert dest.name == "Mod_872296228"
    assert not is_unknown_mod_title(dest.name, published_file_id=workspace_id)
