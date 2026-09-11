"""``mods.updated_at`` authority — system maintenance must not pollute sort time."""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager, TAG_TYPE_CATEGORY
from core.game_info import GameInfo
from core.models import ModMetadata
from services.content_status_eval import persist_evaluated_content_status
from services.mod_library_cache import reset_library_cache
from services.status_authority import (
    CONTENT_CONTENT_MISSING,
    CONTENT_HEALTHY,
    IDENTITY_STATUS_CONFLICT,
    IDENTITY_STATUS_OK,
)
from services.status_recovery import (
    migrate_identity_pollution_from_content,
    reevaluate_content_statuses,
)
from services.updated_at_authority import (
    FORBIDDEN_UPDATED_AT_REASONS,
    LEGAL_UPDATED_AT_REASONS,
    UpdatedAtAuthorityError,
    validate_updated_at_reason,
)
from tests.helpers.identity import create_steam_test_mod

ROOT = Path(__file__).resolve().parents[1]

# Modules that must never SET mods.updated_at (direct SQL or via helpers).
FORBIDDEN_UPDATED_AT_MODULES: tuple[str, ...] = (
    "services/content_status_eval.py",
    "services/status_recovery.py",
    "services/identity_repair.py",
    "services/identity_repair_service.py",
    "services/library_reconcile.py",
    "services/backup_manager.py",
    "services/offline/nexus_html_parser.py",
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    reset_library_cache()
    manager = DatabaseManager(tmp_path / "updated_at_auth.db")
    manager.upsert_game(GameInfo(app_id=42, name="AuthGame", folder_name="AuthGame"))
    yield manager
    manager.close()
    DatabaseManager.reset_instance()
    reset_library_cache()


def _iso(offset_seconds: int = 0) -> str:
    base = datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=offset_seconds)).replace(microsecond=0).isoformat()


def _seed(db: DatabaseManager, mid: int = 91001, *, updated_at: str | None = None) -> str:
    stamp = updated_at or _iso(1)
    create_steam_test_mod(
        db, external_id=str(mid), title="AuthMod", app_id=42, game_name="AuthGame"
    )
    db._conn.execute(
        "UPDATE mods SET updated_at = ?, folder_present = 1, "
        "last_known_path = ?, content_status = ?, identity_status = ? "
        "WHERE mod_id = ?",
        (stamp, f"/tmp/{mid}", CONTENT_HEALTHY, IDENTITY_STATUS_OK, mid),
    )
    db._conn.commit()
    return stamp


def _read_updated_at(db: DatabaseManager, mid: int) -> str:
    row = db._conn.execute(
        "SELECT updated_at FROM mods WHERE mod_id = ?",
        (mid,),
    ).fetchone()
    assert row is not None
    return str(row["updated_at"] or "")


# ---------------------------------------------------------------------------
# Reason validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", sorted(LEGAL_UPDATED_AT_REASONS))
def test_legal_reasons_accepted(reason: str) -> None:
    assert validate_updated_at_reason(reason) == reason


@pytest.mark.parametrize("reason", sorted(FORBIDDEN_UPDATED_AT_REASONS))
def test_forbidden_reasons_rejected(reason: str) -> None:
    with pytest.raises(UpdatedAtAuthorityError):
        validate_updated_at_reason(reason)


def test_empty_and_unknown_reasons_rejected() -> None:
    with pytest.raises(UpdatedAtAuthorityError):
        validate_updated_at_reason("")
    with pytest.raises(UpdatedAtAuthorityError):
        validate_updated_at_reason("mystery")


def test_touch_mod_updated_at_requires_legal_reason(db: DatabaseManager) -> None:
    mid = 91010
    before = _seed(db, mid)
    with pytest.raises(UpdatedAtAuthorityError):
        db.touch_mod_updated_at(mid, reason="recovery")
    assert _read_updated_at(db, mid) == before
    with pytest.raises(UpdatedAtAuthorityError):
        db.touch_mod_updated_at(mid, reason="")
    db.touch_mod_updated_at(mid, reason="user_edit")
    assert _read_updated_at(db, mid) != before


# ---------------------------------------------------------------------------
# System paths must not change updated_at
# ---------------------------------------------------------------------------


def test_content_status_update_does_not_touch_updated_at(db: DatabaseManager) -> None:
    mid = 91101
    before = _seed(db, mid)
    db.update_mod_content_status(
        mid,
        content_status=CONTENT_CONTENT_MISSING,
        folder_present=False,
    )
    assert _read_updated_at(db, mid) == before


def test_content_status_touch_true_raises(db: DatabaseManager) -> None:
    mid = 91102
    before = _seed(db, mid)
    with pytest.raises(UpdatedAtAuthorityError):
        db.update_mod_content_status(
            mid,
            content_status=CONTENT_HEALTHY,
            touch_updated_at=True,
        )
    assert _read_updated_at(db, mid) == before


def test_persist_evaluated_content_status_does_not_touch(
    db: DatabaseManager, tmp_path: Path
) -> None:
    mid = 91103
    before = _seed(db, mid)
    folder = tmp_path / "mod_folder"
    folder.mkdir()
    (folder / "a.txt").write_text("x", encoding="utf-8")
    persist_evaluated_content_status(
        mid,
        folder,
        db=db,
        folder_present=True,
    )
    assert _read_updated_at(db, mid) == before


def test_identity_status_update_does_not_touch_updated_at(db: DatabaseManager) -> None:
    mid = 91201
    before = _seed(db, mid)
    db.update_mod_identity_fields(
        mid,
        identity_status=IDENTITY_STATUS_CONFLICT,
        last_known_path="/tmp/moved",
        folder_present=True,
    )
    assert _read_updated_at(db, mid) == before
    row = db.get_mod(mid)
    assert row is not None


def test_status_recovery_peel_does_not_touch_updated_at(db: DatabaseManager) -> None:
    mid = 91301
    before = _seed(db, mid)
    # Seed identity pollution on content/library axes that recovery will peel.
    db._conn.execute(
        "UPDATE mods SET library_status = ?, content_status = ? WHERE mod_id = ?",
        ("identity_conflict", "identity_conflict", mid),
    )
    db._conn.commit()
    migrate_identity_pollution_from_content(db)
    assert _read_updated_at(db, mid) == before
    reevaluate_content_statuses(db, None)
    assert _read_updated_at(db, mid) == before


def test_update_mod_status_scan_does_not_touch(db: DatabaseManager) -> None:
    mid = 91302
    before = _seed(db, mid)
    db.update_mod_status(mid, invalid=True, invalid_reason="scan", touch_check_time=True)
    assert _read_updated_at(db, mid) == before


def test_offline_backup_deploy_do_not_touch(db: DatabaseManager) -> None:
    mid = 91401
    before = _seed(db, mid)
    db.update_mod_offline_status(mid, status="ready", provider="nexus")
    assert _read_updated_at(db, mid) == before
    db.update_mod_backup_snapshot(
        mid,
        last_known_path="/tmp/x",
        folder_present=True,
        backup_metadata_json="{}",
    )
    assert _read_updated_at(db, mid) == before
    db.update_mod_deploy_status(mid, deploy_status="deployed", deploy_path="/game/x")
    assert _read_updated_at(db, mid) == before
    db.set_official_metadata_synced(mid, True)
    assert _read_updated_at(db, mid) == before
    db.update_mod_cover_path(mid, ".info/cover.png")
    assert _read_updated_at(db, mid) == before
    db.set_mod_folder_present(mid, present=False)
    assert _read_updated_at(db, mid) == before
    db.update_mod_platform_info(mid, title="RefreshedTitle")
    assert _read_updated_at(db, mid) == before


def test_upsert_mod_conflict_does_not_bump(db: DatabaseManager) -> None:
    mid = 91402
    before = _seed(db, mid, updated_at=_iso(50))
    db.upsert_mod(
        ModMetadata(published_file_id=str(mid), title="NewSteamTitle", app_id=42)
    )
    assert _read_updated_at(db, mid) == before


# ---------------------------------------------------------------------------
# User paths must change updated_at
# ---------------------------------------------------------------------------


def test_user_metadata_edit_touches_updated_at(db: DatabaseManager) -> None:
    mid = 91501
    before = _seed(db, mid)
    db.update_mod_user_metadata(
        mid,
        {"display_name": "Renamed", "custom_description": "", "user_notes": "", "favorite": 0},
    )
    after = _read_updated_at(db, mid)
    assert after != before
    assert after


def test_user_enable_and_conflict_annotation_touch(db: DatabaseManager) -> None:
    mid = 91502
    before = _seed(db, mid)
    db.disable_mod(mid)
    assert _read_updated_at(db, mid) != before
    # Re-seed an older stamp so the next user write is observably newer.
    older = _iso(2)
    db._conn.execute(
        "UPDATE mods SET updated_at = ? WHERE mod_id = ?",
        (older, mid),
    )
    db._conn.commit()
    db.update_mod_conflict_annotation(mid, conflict=True, note="user mark")
    assert _read_updated_at(db, mid) != older


def test_user_tag_touches_updated_at(db: DatabaseManager) -> None:
    mid = 91503
    before = _seed(db, mid)
    from core.db_manager import TAG_TYPE_CATEGORY

    db.add_mod_tag(mid, TAG_TYPE_CATEGORY, "rpg")
    assert _read_updated_at(db, mid) != before


# ---------------------------------------------------------------------------
# Static: forbidden modules must not assign mods.updated_at
# ---------------------------------------------------------------------------


_MODS_UPDATED_AT_ASSIGN = re.compile(
    r"UPDATE\s+mods\s+SET[\s\S]{0,800}?updated_at\s*=",
    re.IGNORECASE,
)


def test_forbidden_modules_do_not_assign_mods_updated_at() -> None:
    violations: list[str] = []
    for rel in FORBIDDEN_UPDATED_AT_MODULES:
        path = ROOT / rel
        if not path.is_file():
            violations.append(f"missing module: {rel}")
            continue
        text = path.read_text(encoding="utf-8")
        if _MODS_UPDATED_AT_ASSIGN.search(text):
            violations.append(rel)
        if "touch_mod_updated_at(" in text or "_append_mods_updated_at(" in text:
            violations.append(f"{rel}: calls touch helper")
        # Only flag updated_at-related reason kwargs, not audit log sources.
        if re.search(
            r"(?:touch_mod_updated_at|_append_mods_updated_at|_mods_updated_at_now"
            r"|updated_at_reason)\s*\([^)]*reason\s*=\s*[\"']"
            r"(?:recovery|reconcile|identity_repair|content_eval|backup|sync|migration)",
            text,
        ):
            violations.append(f"{rel}: forbidden updated_at reason")
    assert not violations, f"forbidden updated_at pollution: {violations}"


def test_db_manager_content_status_default_is_false() -> None:
    import inspect

    sig = inspect.signature(DatabaseManager.update_mod_content_status)
    assert sig.parameters["touch_updated_at"].default is False


def test_authority_module_exports_stable_sets() -> None:
    assert "user_edit" in LEGAL_UPDATED_AT_REASONS
    assert "recovery" in FORBIDDEN_UPDATED_AT_REASONS
    assert LEGAL_UPDATED_AT_REASONS.isdisjoint(FORBIDDEN_UPDATED_AT_REASONS)


def test_reconcile_module_has_no_updated_at_sql() -> None:
    path = ROOT / "services" / "library_reconcile.py"
    text = path.read_text(encoding="utf-8")
    assert "updated_at" not in text or not _MODS_UPDATED_AT_ASSIGN.search(text)


def test_no_implicit_updated_at_in_ast_for_status_apis() -> None:
    """Guards: update_mod_content_status / update_mod_status bodies omit updated_at."""
    path = ROOT / "core" / "db_manager.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    watched = {"update_mod_content_status", "update_mod_status", "update_mod_deploy_status"}
    found: dict[str, bool] = {n: False for n in watched}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "DatabaseManager":
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name not in watched:
                continue
            src = ast.get_source_segment(path.read_text(encoding="utf-8"), item) or ""
            # Allow docstring / comments mentioning the field; forbid SET assignments.
            found[item.name] = bool(
                re.search(r"updated_at\s*=\s*\?", src)
                or re.search(r'["\']updated_at\s*=', src)
            )
    assert found == {n: False for n in watched}, found
