"""Frozen architecture lock for IDENTITY_LIFECYCLE_CONTRACT.md.

Read-only guards. Does not modify Identity / Sync / Import / Reconcile /
Backup / Deploy / Status / Projection business logic — only asserts that
current sources still obey the frozen contract.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.library_reconcile import reconcile_library
from services.mod_identity import ensure_internal_id, ensure_mod_identity

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "docs" / "architecture" / "IDENTITY_LIFECYCLE_CONTRACT.md"

STARDEW = 413150


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "identity_contract_freeze.db")
    manager.upsert_game(GameInfo(app_id=STARDEW, name="Stardew", folder_name="Stardew"))
    yield manager
    DatabaseManager.reset_instance()


def _strip_comments_and_docstrings(source: str) -> str:
    stripped = re.sub(r'"""[\s\S]*?"""', "", source)
    stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
    stripped = re.sub(r"#.*?$", "", stripped, flags=re.M)
    return stripped


def _call_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_contract_document_exists_and_covers_required_sections() -> None:
    assert CONTRACT.is_file(), "IDENTITY_LIFECYCLE_CONTRACT.md must exist"
    text = CONTRACT.read_text(encoding="utf-8")
    required = (
        "internal_id",
        "workspace_id",
        "Steam Workshop Sync",
        "Import",
        ".info",
        "Reconcile",
        "Backup",
        "find_mod_by_workspace_id",
        "folder digits",
        "Orphan auto",
        "external_id",
    )
    for token in required:
        assert token in text, f"contract missing required token: {token!r}"


def test_workspace_lookup_api_is_disabled(db: DatabaseManager) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1333",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
        title="Carry Chest",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    assert created.workspace_id
    assert db.find_mod_by_workspace_id(created.workspace_id) is None
    assert db.find_mod_by_workspace_id(created.workspace_id, platform=PLATFORM_NEXUS, app_id=STARDEW) is None
    assert db.find_mod_id_by_workspace_id(created.workspace_id) is None


def test_forged_info_does_not_create_db_entity(db: DatabaseManager, tmp_path: Path) -> None:
    folder = tmp_path / "mod" / "Game" / "Forged"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        '{"internal_id":"ffffffff-ffff-ffff-ffff-ffffffffffff","workspace_id":"999"}',
        encoding="utf-8",
    )
    (folder / "x.bin").write_bytes(b"x")
    mid, _, _ = ensure_mod_identity(folder, {}, db=db)
    assert mid == ""
    forged = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    assert db.find_mod_by_internal_id(forged) is None
    data, minted = ensure_internal_id({"internal_id": forged, "title": "x"})
    assert minted is False
    assert data.get("internal_id") == forged  # unchanged; never invents a new one
    # Still no DB entity for forged proof.
    assert db.find_mod_by_internal_id(forged) is None


def test_reconcile_source_forbids_create_and_workspace_lookup() -> None:
    src = (ROOT / "services" / "library_reconcile.py").read_text(encoding="utf-8")
    body = _strip_comments_and_docstrings(src)
    calls = _call_names(src)
    assert "create_mod_identity" not in calls
    assert "find_mod_by_workspace_id(" not in body
    assert "find_mod_id_by_workspace_id(" not in body


def test_mod_identity_source_forbids_mint_and_path_identity() -> None:
    src = (ROOT / "services" / "mod_identity.py").read_text(encoding="utf-8")
    body = _strip_comments_and_docstrings(src)
    # Must not mint UUIDs for missing identity.
    assert "uuid4(" not in body and "uuid.uuid4(" not in body
    # Must not reverse-lookup via workspace.
    assert "find_mod_by_workspace_id(" not in body
    assert "find_mod_id_by_workspace_id(" not in body


def test_db_manager_workspace_api_is_hard_noop() -> None:
    src = (ROOT / "core" / "db_manager.py").read_text(encoding="utf-8")
    # Locate find_mod_by_workspace_id body and require immediate None return.
    match = re.search(
        r"def find_mod_by_workspace_id\([\s\S]*?\n    def ",
        src,
    )
    assert match, "find_mod_by_workspace_id must exist as disabled API"
    block = match.group(0)
    assert "return None" in block
    assert "SELECT" not in block.upper()


def test_backup_restore_helper_does_not_create_mods() -> None:
    src = (ROOT / "services" / "metadata_backup.py").read_text(encoding="utf-8")
    body = _strip_comments_and_docstrings(src)
    assert "create_mod_identity(" not in body
    assert "INSERT INTO mods" not in body


def test_orphan_import_is_bind_only_not_create() -> None:
    path = ROOT / "services" / "orphan_import.py"
    if not path.is_file():
        pytest.skip("orphan_import module not present")
    src = path.read_text(encoding="utf-8")
    body = _strip_comments_and_docstrings(src)
    assert "create_mod_identity(" not in body
    calls = _call_names(src)
    assert "create_mod_identity" not in calls


def test_reconcile_ignores_folder_digits_without_db_entity(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "Game" / "Unknown Mod 4242424242"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text('{"title":"Unknown Mod 4242424242"}', encoding="utf-8")
    result = reconcile_library(library)
    assert db.get_mod("4242424242") is None
    assert not any(
        getattr(o, "external_id", "") == "4242424242"
        or getattr(o, "workspace_id", "") == "4242424242"
        for o in result.orphans
    )
