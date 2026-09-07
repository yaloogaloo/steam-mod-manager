"""Final Mod status boundary contract — permanent architecture guardrails.

Guarantees the reduced Mod status lifecycle cannot be broken by mis-wired
callers::

  content_status  ← content_status_eval only
  conflict_status ← user_annotation only
  identity_status ← identity / reconcile / recovery (never Mod Conflict)
  deploy_status   ← deploy service only

Library UI reads projection only — never reconcile / evaluator / FS status.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_status import CONFLICT_STATUS_CONFLICT, CONFLICT_STATUS_NONE
from core.models import ModMetadata
from services.content_status_eval import persist_evaluated_content_status
from services.library_status import CONTENT_CONTENT_MISSING, CONTENT_HEALTHY
from services.status_authority import (
    CONTENT_STATUS_WRITERS,
    CONFLICT_STATUS_WRITERS,
    DELETED_CONTENT_STATUS_TOKENS,
    DEPLOY_STATUS_WRITERS,
    IDENTITY_STATUS_CONFLICT,
    IDENTITY_STATUS_WRITERS,
    STATUS_MODEL_CLEANUP_V2_FLAG,
    SUPPORTED_CONTENT_STATUSES,
)
from services.status_recovery import run_status_model_cleanup_v2
from services.user_annotation import set_conflict_annotation
from ui.library_query import FILTER_CONFLICT, ModFilterIndex, matches_status_filter

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"
UI = ROOT / "ui"

_BANNED_CONFLICT_CALLS = frozenset(
    {
        "update_mod_conflict_annotation",
        "set_conflict_annotation",
        "clear_conflict_annotation",
        "apply_conflict_annotation",
    }
)
_BANNED_CONTENT_CALLS = frozenset(
    {
        "update_mod_content_status",
        "persist_evaluated_content_status",
    }
)

# Non-authority production modules that must not write conflict / content.
_FORBIDDEN_CONFLICT_MODULES = (
    SERVICES / "conflict.py",
    SERVICES / "deploy.py",
    SERVICES / "library_reconcile.py",
    SERVICES / "identity_repair.py",
    SERVICES / "identity_service.py",
    SERVICES / "content_status_eval.py",
    SERVICES / "mod_refresh.py",
    SERVICES / "sync.py",
    SERVICES / "path_lifecycle.py",
    SERVICES / "status_recovery.py",
    SERVICES / "mod_library_cache.py",
    UI / "library_view.py",
    UI / "mod_card.py",
)

_FORBIDDEN_CONTENT_MODULES = (
    SERVICES / "conflict.py",
    SERVICES / "deploy.py",
    SERVICES / "identity_repair.py",
    SERVICES / "identity_service.py",
    SERVICES / "user_annotation.py",
    UI / "library_view.py",
    UI / "mod_card.py",
    UI / "mod_detail_panel.py",
)

_LIBRARY_UI_MODULES = (
    UI / "library_view.py",
    UI / "mod_card.py",
    UI / "library_query.py",
)

_LIBRARY_FORBIDDEN_CALLEES = frozenset(
    {
        "reconcile_library",
        "start_reconcile_library_async",
        "persist_evaluated_content_status",
        "evaluate_content_status",
        "run_status_recovery",
        "run_status_model_cleanup_v2",
        "update_mod_content_status",
        "update_mod_conflict_annotation",
        "update_mod_deploy_status",
    }
)


def _call_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
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


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "boundary.db")
    yield manager
    DatabaseManager.reset_instance()


def _index(**kwargs) -> ModFilterIndex:
    base = dict(
        internal_id="1",
        display_name="X",
        steam_name="",
        notes="",
        game_name="G",
        favorite=False,
        deployed=False,
        has_offline=False,
        mtime=0.0,
        sort_name="X",
        content_status=CONTENT_HEALTHY,
        identity_status="ok",
        conflict=False,
        conflict_status="none",
        enabled=True,
    )
    base.update(kwargs)
    return ModFilterIndex(**base)


def test_unauthorized_modules_cannot_call_conflict_writers() -> None:
    for path in _FORBIDDEN_CONFLICT_MODULES:
        assert path.is_file(), path
        names = _call_names(path)
        banned = names & _BANNED_CONFLICT_CALLS
        assert not banned, f"{path.relative_to(ROOT)} calls {banned}"


def test_unauthorized_modules_cannot_call_content_writers() -> None:
    for path in _FORBIDDEN_CONTENT_MODULES:
        assert path.is_file(), path
        names = _call_names(path)
        banned = names & _BANNED_CONTENT_CALLS
        assert not banned, f"{path.relative_to(ROOT)} calls {banned}"


def test_writer_allowlists_match_architecture() -> None:
    assert "services/content_status_eval.py" in CONTENT_STATUS_WRITERS
    assert "services/user_annotation.py" in CONFLICT_STATUS_WRITERS
    assert "services/deploy.py" in DEPLOY_STATUS_WRITERS
    assert "services/library_reconcile.py" in IDENTITY_STATUS_WRITERS
    assert "services/status_recovery.py" in IDENTITY_STATUS_WRITERS


def test_content_writer_rejects_deleted_tokens(db: DatabaseManager) -> None:
    db.update_game_deploy_config(1, name="G")
    db.upsert_mod(ModMetadata(published_file_id="401", title="T", app_id=1))
    for token in sorted(DELETED_CONTENT_STATUS_TOKENS & {"folder_missing", "backup_invalid", "metadata_missing", "file_missing"}):
        with pytest.raises(ValueError, match="illegal content_status"):
            db.update_mod_content_status("401", content_status=token)


def test_identity_status_does_not_produce_conflict_filter() -> None:
    idx = _index(
        identity_status=IDENTITY_STATUS_CONFLICT,
        conflict_status="none",
        content_status=CONTENT_HEALTHY,
    )
    assert matches_status_filter(idx, FILTER_CONFLICT) is False
    assert matches_status_filter(idx, "identity_conflict") is False


def test_content_missing_does_not_produce_conflict() -> None:
    idx = _index(
        content_status=CONTENT_CONTENT_MISSING,
        conflict_status="none",
        identity_status="ok",
    )
    assert matches_status_filter(idx, FILTER_CONFLICT) is False


def test_deploy_and_recovery_preserve_user_conflict(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "Game" / "M"
    folder.mkdir(parents=True)
    (folder / "a.bin").write_bytes(b"x")
    db.update_game_deploy_config(1, name="Game")
    db.upsert_mod(ModMetadata(published_file_id="402", title="M", app_id=1))
    db.update_mod_identity_fields(
        "402", folder_present=True, last_known_path=str(folder)
    )
    set_conflict_annotation("402", note="user", db=db)
    db.update_mod_deploy_status("402", deploy_status="deployed")
    before_conflict = db.get_mod_status(402).conflict_status
    before_deploy = db.get_mod_deploy_info("402")
    assert before_conflict == CONFLICT_STATUS_CONFLICT
    assert before_deploy is not None
    assert before_deploy.deploy_status == "deployed"

    with db._lock:
        db._conn.execute(
            "DELETE FROM schema_flags WHERE flag = ?",
            (STATUS_MODEL_CLEANUP_V2_FLAG,),
        )
        db._conn.commit()

    run_status_model_cleanup_v2(db, library, force=True)

    assert db.get_mod_status(402).conflict_status == CONFLICT_STATUS_CONFLICT
    assert db.get_mod_status(402).conflict_note == "user"
    after = db.get_mod_deploy_info("402")
    assert after is not None
    assert after.deploy_status == "deployed"
    assert str((db.get_mod_backup_row("402") or {}).get("content_status") or "") in (
        SUPPORTED_CONTENT_STATUSES
    )


def test_library_ui_does_not_trigger_status_computation() -> None:
    for path in _LIBRARY_UI_MODULES:
        assert path.is_file(), path
        names = _call_names(path)
        hit = names & _LIBRARY_FORBIDDEN_CALLEES
        assert not hit, f"{path.relative_to(ROOT)} triggers status compute: {hit}"
        src = path.read_text(encoding="utf-8")
        assert "content_status_eval" not in src or path.name == "library_query.py"


def test_mod_card_status_is_projection_only() -> None:
    from ui.mod_card import ModCardWidget

    abandoned = inspect.getsource(ModCardWidget._is_abandoned)
    assert "get_mod_tags" not in abandoned
    assert "get_db" not in abandoned
    missing = inspect.getsource(ModCardWidget._render_missing_content_badge)
    assert "get_db" not in missing
    assert "get_mod_status" not in missing
    assert "identity_status" not in missing
    overlay = inspect.getsource(ModCardWidget._overlay_user_flags)
    assert "get_db" not in overlay
    assert "get_mod_status" not in overlay


def test_only_supported_content_statuses_persist(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "mod" / "G" / "A"
    folder.mkdir(parents=True)
    (folder / "p.bin").write_bytes(b"1")
    db.update_game_deploy_config(1, name="G")
    db.upsert_mod(ModMetadata(published_file_id="403", title="A", app_id=1))
    persist_evaluated_content_status(
        "403", folder, db=db, folder_present=True, sync_sticky_marker=True
    )
    cs = str((db.get_mod_backup_row("403") or {}).get("content_status") or "")
    assert cs in SUPPORTED_CONTENT_STATUSES
    assert cs not in DELETED_CONTENT_STATUS_TOKENS


def test_update_mod_status_cannot_write_conflict() -> None:
    sig = inspect.signature(DatabaseManager.update_mod_status)
    assert "conflict_status" not in sig.parameters
    assert "conflict_note" not in sig.parameters
