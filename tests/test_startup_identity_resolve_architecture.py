"""Architecture: ordinary identity resolve must not full-scan backups.

ARCHITECTURE RULE
-----------------
``resolve_existing_mod_id`` / startup reconcile must use indexed DB lookups.
Walking every backup + ``load_backup`` made Loading Mods wait minutes while
``identity_ms`` dominated ``RECONCILE_TIMING``.
"""

from __future__ import annotations

import ast
import inspect
import time
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.library_reconcile import reconcile_library
from services.mod_identity import ensure_mod_identity, resolve_existing_mod_id


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "ident_perf.db")
    yield manager
    DatabaseManager.reset_instance()


def test_resolve_existing_mod_id_source_has_no_backup_full_scan() -> None:
    src = inspect.getsource(resolve_existing_mod_id)
    # Strip docstring — architecture notes may mention forbidden APIs.
    body = src.split('"""', 2)[-1] if '"""' in src else src
    assert "load_backup(" not in body
    assert "iter_mod_backup_rows(" not in body
    assert "_lookup_relocated_folder(" not in body


def test_resolve_ast_forbids_load_backup_call() -> None:
    path = ROOT / "services" / "mod_identity.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "resolve_existing_mod_id":
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                name = ""
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                assert name != "load_backup"
                assert name != "iter_mod_backup_rows"
                assert name != "_lookup_relocated_folder"


def test_unknown_mod_placeholder_resolve_is_cheap(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Unknown Mod with Workspace ID must use indexed lookup — never backup scan.

    When no entity exists yet, resolve returns empty (create is Sync/Identity
    Service's job). Parsing Workspace ID from the title must stay O(1).
    """
    library = tmp_path / "mod"
    # Seed many backup rows so a full scan would be expensive if reintroduced.
    for i in range(80):
        mid = str(900100 + i)
        db.update_game_deploy_config(1, name="G")
        db.upsert_mod(ModMetadata(published_file_id=mid, title=f"M{i}", app_id=1))
        db.update_mod_identity_fields(
            mid,
            folder_present=True,
            last_known_path=str(library / "G" / f"M{i}"),
        )

    folder = library / "Duckov" / "Unknown Mod 3413520661"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        '{"title":"Unknown Mod 3413520661","published_file_id":""}',
        encoding="utf-8",
    )
    payload = {
        "title": "Unknown Mod 3413520661",
        "_folder_name": folder.name,
        "_managed_path": str(folder),
    }
    t0 = time.perf_counter()
    for _ in range(50):
        # No entity yet → empty; must not invent Internal ID here.
        assert resolve_existing_mod_id(payload, db=db) == ""
        mid, _, _ = ensure_mod_identity(folder, dict(payload), db=db)
        assert mid == ""
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    # Indexed early-return should stay well under a second for 50×2 calls.
    assert elapsed_ms < 500.0, f"placeholder resolve too slow: {elapsed_ms:.1f}ms"


def test_reconcile_unknown_mod_does_not_invent_identity_from_folder_name(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Folder name ``Unknown Mod <digits>`` is storage only — not identity proof.

    Reconcile must not mint entities and must not invent workspace/external IDs
    from directory names. Incomplete .info without DB-bound internal_id is ignored.
    """
    library = tmp_path / "mod"
    folder = library / "Duckov" / "Unknown Mod 3413520661"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        '{"title":"Unknown Mod 3413520661"}',
        encoding="utf-8",
    )
    result = reconcile_library(library)
    assert not any("IDENTITY_UNRESOLVED_PLACEHOLDER" in n for n in result.notes)
    assert db.get_mod("3413520661") is None
    assert db.find_mod_by_workspace_id("3413520661") is None
    # Digits in the folder title must not become fabricated external/workspace IDs.
    assert not any(
        getattr(o, "workspace_id", "") == "3413520661"
        or getattr(o, "external_id", "") == "3413520661"
        for o in result.orphans
    )
