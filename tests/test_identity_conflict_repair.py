"""Historical path/identity conflict diagnose + confirm-gated repair."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.content_status_eval import persist_evaluated_content_status
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from tools.diagnose_mod_path_identity_conflict import (
    DECISION_CONFLICT,
    run_diagnose,
)
from tools.repair_mod_path_identity_conflict import apply_repair

STARDEW = 413150
FORGED_INTERNAL_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "conflict_repair.db")
    manager.upsert_game(
        GameInfo(app_id=STARDEW, name="Stardew", folder_name="Stardew")
    )
    yield manager
    DatabaseManager.reset_instance()


def _write_info(folder: Path, payload: dict) -> Path:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (folder / "content.bin").write_bytes(b"x")
    return folder


def _read_info(folder: Path) -> dict:
    return json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )


def _seed_conflict(
    db: DatabaseManager, library: Path
) -> tuple[str, str, Path, Path]:
    """
    DB entity A (missing path) + disk folder proving forged internal_id B.

    Returns ``(mod_id, db_internal_id, conflict_folder, db_path)``.
    """
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="1401",
        source_url="https://www.nexusmods.com/stardewvalley/mods/1401",
        title="Tractor Mod",
        app_id=STARDEW,
        game_name="Stardew",
        operation="import",
    )
    mid = str(created.mod_id)
    # Mirror production portable proof (create_mod_identity may leave column empty
    # in minimal fixtures; bind proof is still mods.internal_id UUID).
    db_iid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    gone = library / "Stardew" / "Tractor Mod_gone"
    db.update_mod_identity_fields(
        mid,
        internal_id=db_iid,
        last_known_path=str(gone.resolve()),
        folder_present=False,
    )
    persist_evaluated_content_status(
        mid, gone, db=db, folder_present=False, sync_sticky_marker=False
    )

    folder = _write_info(
        library / "Stardew" / "Tractor Mod",
        {
            "internal_id": FORGED_INTERNAL_ID,
            "workspace_id": "1401",
            "platform": PLATFORM_NEXUS,
            "app_id": STARDEW,
            "title": "Tractor Mod",
            "display_name": "拖拉机",
            "cover_path": "cover.jpg",
        },
    )
    (folder / INFO_DIR_NAME / "cover.jpg").write_bytes(b"cover")
    return mid, db_iid, folder, Path(db.db_path)


def test_diagnose_identity_conflict_is_readonly(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid, db_iid, folder, db_path = _seed_conflict(db, library)

    before_info = _read_info(folder)
    before_row = dict(db.get_mod_backup_row(mid) or {})

    report = run_diagnose(
        internal_id=db_iid,
        db_path=db_path,
        library_root=library,
    )

    assert report.decision == DECISION_CONFLICT
    assert report.db_entity["internal_id"] == db_iid
    assert report.db_entity["workspace_id"] == "1401"
    assert report.conflict_candidates
    conflict = report.conflict_candidates[0]
    assert conflict["info_internal_id"] == FORGED_INTERNAL_ID
    assert conflict["info_workspace_id"] == "1401"
    assert conflict["info_internal_id_in_db"] is False
    assert Path(conflict["path"]).resolve() == folder.resolve()

    # Diagnose must not mutate .info or DB.
    assert _read_info(folder) == before_info
    after_row = dict(db.get_mod_backup_row(mid) or {})
    assert after_row.get("last_known_path") == before_row.get("last_known_path")
    assert int(after_row.get("folder_present") or 0) == 0
    assert after_row.get("internal_id") == before_row.get("internal_id")


def test_repair_with_confirm_rebinds_info_and_path(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid, db_iid, folder, db_path = _seed_conflict(db, library)

    result = apply_repair(
        from_internal_id=db_iid,
        to_path=folder,
        confirm=True,
        db_path=db_path,
        library_root=library,
        db=db,
    )
    assert result["executed"] is True
    assert result["db_internal_id_unchanged"] is True

    info = _read_info(folder)
    assert info["internal_id"] == db_iid
    assert info["workspace_id"] == "1401"
    assert info.get("cover_path") == "cover.jpg"
    assert (folder / INFO_DIR_NAME / "cover.jpg").is_file()
    assert Path(result["backup_metadata"]).is_file()

    row = db.get_mod_backup_row(mid) or {}
    assert Path(str(row.get("last_known_path") or "")).resolve() == folder.resolve()
    assert int(row.get("folder_present") or 0) == 1
    # DB entity identity must not be rewritten (A stays A).
    assert str(row.get("internal_id") or "").strip() == db_iid
    assert result["info_internal_id_after"] == db_iid


def test_workspace_match_alone_does_not_auto_repair(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid, db_iid, folder, db_path = _seed_conflict(db, library)
    before = _read_info(folder)

    # Diagnose finds conflict — reporting only, not a bind.
    report = run_diagnose(
        workspace_id="1401",
        app_id=STARDEW,
        db_path=db_path,
        library_root=library,
    )
    assert report.decision == DECISION_CONFLICT
    assert _read_info(folder) == before
    row = db.get_mod_backup_row(mid) or {}
    assert Path(str(row.get("last_known_path") or "")).resolve() != folder.resolve()
    assert int(row.get("folder_present") or 0) == 0

    # No API path auto-executes repair from workspace_id alone.
    dry = apply_repair(
        from_internal_id=db_iid,
        to_path=folder,
        confirm=False,
        db_path=db_path,
        library_root=library,
        db=db,
    )
    assert dry["executed"] is False
    assert _read_info(folder)["internal_id"] == FORGED_INTERNAL_ID


def test_repair_without_confirm_makes_no_changes(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    mid, db_iid, folder, db_path = _seed_conflict(db, library)
    before_info = _read_info(folder)
    before_row = dict(db.get_mod_backup_row(mid) or {})

    result = apply_repair(
        from_internal_id=db_iid,
        to_path=folder,
        confirm=False,
        db_path=db_path,
        library_root=library,
        db=db,
    )
    assert result["executed"] is False
    assert "missing --confirm" in result["reason"]
    assert _read_info(folder) == before_info
    after_row = dict(db.get_mod_backup_row(mid) or {})
    assert after_row.get("last_known_path") == before_row.get("last_known_path")
    assert int(after_row.get("folder_present") or 0) == int(
        before_row.get("folder_present") or 0
    )
    backups = list((folder / INFO_DIR_NAME).glob("metadata.json.pre_identity_repair.*"))
    assert backups == []
