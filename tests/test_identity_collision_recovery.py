"""Identity Collision Recovery Phase 3 — audit / preview / apply."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.deploy_paths import resolve_deploy_managed_path
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.library_reconcile import reconcile_library
from tools.identity_collision_apply import apply_plan
from tools.identity_collision_audit import run_audit
from tools.identity_collision_common import (
    CODE_INTERNAL_ID_COLLISION,
    DECISION_APPROVE,
    read_info,
)
from tools.identity_collision_preview import build_recovery_plan

STARDEW = 413150
BG3 = 1086940
SHARED_WS = "1333"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "collision.db")
    manager.upsert_game(GameInfo(app_id=STARDEW, name="Stardew Valley"))
    manager.upsert_game(GameInfo(app_id=BG3, name="Baldurs Gate 3"))
    yield manager
    DatabaseManager.reset_instance()


def _write_info(folder: Path, payload: dict) -> Path:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    path = info / METADATA_FILENAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _seed_merged_collision(
    db: DatabaseManager, library: Path
) -> tuple[str, Path, Path]:
    """
    Simulate historical merge pollution:

    - One DB entity (BG3 Community Library, workspace 1333)
    - Two .info folders both stamped with that entity's internal_id
    """
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=SHARED_WS,
        source_url="https://www.nexusmods.com/baldursgate3/mods/1333",
        title="Community Library",
        app_id=BG3,
        game_name="Baldurs Gate 3",
        operation="import",
    )
    mid = str(created.mod_id)
    proof = mid
    db.update_mod_identity_fields(mid, internal_id=proof, workspace_id=SHARED_WS)

    bg3 = library / "Baldurs Gate 3" / "Community Library"
    bg3.mkdir(parents=True)
    (bg3 / "pak.bin").write_bytes(b"bg3")
    _write_info(
        bg3,
        {
            "internal_id": proof,
            "workspace_id": SHARED_WS,
            "external_id": SHARED_WS,
            "platform": PLATFORM_NEXUS,
            "app_id": BG3,
            "title": "Community Library",
            "url": "https://www.nexusmods.com/baldursgate3/mods/1333",
        },
    )
    db.update_mod_identity_fields(
        mid, last_known_path=str(bg3.resolve()), folder_present=True
    )

    sd = library / "Stardew Valley" / "Carry Chest"
    sd.mkdir(parents=True)
    (sd / "pak.bin").write_bytes(b"sd")
    _write_info(
        sd,
        {
            "internal_id": proof,
            "workspace_id": SHARED_WS,
            "external_id": SHARED_WS,
            "platform": PLATFORM_NEXUS,
            "app_id": STARDEW,
            "title": "Carry Chest",
            "url": "https://www.nexusmods.com/stardewvalley/mods/1333",
        },
    )
    return mid, bg3, sd


def test_two_info_sharing_internal_id_generate_collision(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mid, bg3, sd = _seed_merged_collision(db, library)
    report = run_audit(db_path=tmp_path / "collision.db", library=library)
    collisions = [
        f for f in report["findings"] if f["code"] == CODE_INTERNAL_ID_COLLISION
    ]
    assert collisions, report["counts"]
    hit = collisions[0]
    assert hit["old_internal_id"] in {mid, str(mid)}
    paths = {Path(p).resolve() for p in hit["affected_paths"]}
    assert bg3.resolve() in paths
    assert sd.resolve() in paths


def test_preview_does_not_mutate_database(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mid, _bg3, _sd = _seed_merged_collision(db, library)
    db_path = tmp_path / "collision.db"
    before = db.get_mod_backup_row(mid) or {}
    report = run_audit(db_path=db_path, library=library)
    plan = build_recovery_plan(report)
    after = db.get_mod_backup_row(mid) or {}
    assert before == after
    assert plan["production_mutation"] == "NONE"
    assert all(i.get("manual_decision") == "" for i in plan["items"])


def test_unapproved_cannot_apply(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mid, bg3, sd = _seed_merged_collision(db, library)
    db_path = tmp_path / "collision.db"
    report = run_audit(db_path=db_path, library=library)
    plan = build_recovery_plan(report)
    for item in plan["items"]:
        if item["finding_code"] != CODE_INTERNAL_ID_COLLISION:
            continue
        item["keep_path"] = str(bg3)
        item["split_paths"] = [str(sd)]
        item["manual_decision"] = ""
    dry = apply_plan(
        plan,
        db_path=db_path,
        library=library,
        apply=False,
        confirm=False,
        rollback_dir=tmp_path / "rb",
        db=db,
    )
    assert dry["applied"] is False

    for item in plan["items"]:
        if item["finding_code"] == CODE_INTERNAL_ID_COLLISION:
            item["manual_decision"] = "REJECT"
    result = apply_plan(
        plan,
        db_path=db_path,
        library=library,
        apply=True,
        confirm=True,
        rollback_dir=tmp_path / "rb",
        db=db,
    )
    assert result["applied"] is True
    assert all(
        not r.get("applied")
        for r in result["results"]
        if r.get("finding_code") == CODE_INTERNAL_ID_COLLISION
    )
    row = db.get_mod_backup_row(mid)
    assert row is not None
    payload, _ = read_info(sd)
    assert str((payload or {}).get("internal_id") or "") == mid


def test_approve_splits_and_rewrites_info(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    mid, bg3, sd = _seed_merged_collision(db, library)
    db_path = tmp_path / "collision.db"
    report = run_audit(db_path=db_path, library=library)
    plan = build_recovery_plan(report)
    for item in plan["items"]:
        if item["finding_code"] != CODE_INTERNAL_ID_COLLISION:
            continue
        item["manual_decision"] = DECISION_APPROVE
        item["keep_path"] = str(bg3.resolve())
        item["split_paths"] = [str(sd.resolve())]

    result = apply_plan(
        plan,
        db_path=db_path,
        library=library,
        apply=True,
        confirm=True,
        rollback_dir=tmp_path / "rb",
        db=db,
    )
    assert result["applied"] is True
    applied = [r for r in result["results"] if r.get("applied")]
    assert applied
    created = applied[0]["created"]
    assert created and created[0].get("mod_id")
    new_mid = str(created[0]["mod_id"])
    new_iid = str(created[0]["internal_id"])
    assert new_mid != mid

    keep_row = db.get_mod_backup_row(mid) or {}
    assert Path(str(keep_row.get("last_known_path") or "")).resolve() == bg3.resolve()
    keep_info, _ = read_info(bg3)
    assert str((keep_info or {}).get("internal_id") or "") in {
        mid,
        str(keep_row.get("internal_id") or mid),
    }

    split_row = db.get_mod_backup_row(new_mid) or {}
    assert Path(str(split_row.get("last_known_path") or "")).resolve() == sd.resolve()
    assert int(split_row.get("app_id") or 0) == STARDEW
    split_info, _ = read_info(sd)
    assert str((split_info or {}).get("internal_id") or "") == new_iid
    assert str(split_row.get("internal_id") or "") == new_iid


def test_reconcile_does_not_remerge_after_split(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mid, bg3, sd = _seed_merged_collision(db, library)
    db_path = tmp_path / "collision.db"
    report = run_audit(db_path=db_path, library=library)
    plan = build_recovery_plan(report)
    for item in plan["items"]:
        if item["finding_code"] != CODE_INTERNAL_ID_COLLISION:
            continue
        item["manual_decision"] = DECISION_APPROVE
        item["keep_path"] = str(bg3.resolve())
        item["split_paths"] = [str(sd.resolve())]
    result = apply_plan(
        plan,
        db_path=db_path,
        library=library,
        apply=True,
        confirm=True,
        rollback_dir=tmp_path / "rb",
        db=db,
    )
    new_mid = str(result["results"][0]["created"][0]["mod_id"])

    reconcile_library(library_root=library)
    keep = db.get_mod_backup_row(mid) or {}
    split = db.get_mod_backup_row(new_mid) or {}
    assert Path(str(keep.get("last_known_path") or "")).resolve() == bg3.resolve()
    assert Path(str(split.get("last_known_path") or "")).resolve() == sd.resolve()
    assert str(keep.get("internal_id") or mid) != str(split.get("internal_id") or "")


def test_deploy_resolves_by_internal_id_after_split(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    mid, bg3, sd = _seed_merged_collision(db, library)
    db_path = tmp_path / "collision.db"
    report = run_audit(db_path=db_path, library=library)
    plan = build_recovery_plan(report)
    for item in plan["items"]:
        if item["finding_code"] != CODE_INTERNAL_ID_COLLISION:
            continue
        item["manual_decision"] = DECISION_APPROVE
        item["keep_path"] = str(bg3.resolve())
        item["split_paths"] = [str(sd.resolve())]
    result = apply_plan(
        plan,
        db_path=db_path,
        library=library,
        apply=True,
        confirm=True,
        rollback_dir=tmp_path / "rb",
        db=db,
    )
    new_mid = str(result["results"][0]["created"][0]["mod_id"])

    found_bg3 = resolve_deploy_managed_path(mid, db=db, library_root=library)
    found_sd = resolve_deploy_managed_path(new_mid, db=db, library_root=library)
    assert found_bg3 is not None and found_bg3.resolve() == bg3.resolve()
    assert found_sd is not None and found_sd.resolve() == sd.resolve()
    assert found_bg3.resolve() != found_sd.resolve()


def test_apply_refuses_without_confirm(tmp_path: Path, db: DatabaseManager) -> None:
    library = tmp_path / "mod"
    _mid, bg3, sd = _seed_merged_collision(db, library)
    db_path = tmp_path / "collision.db"
    report = run_audit(db_path=db_path, library=library)
    plan = build_recovery_plan(report)
    for item in plan["items"]:
        if item["finding_code"] != CODE_INTERNAL_ID_COLLISION:
            continue
        item["manual_decision"] = DECISION_APPROVE
        item["keep_path"] = str(bg3)
        item["split_paths"] = [str(sd)]
    with pytest.raises(SystemExit):
        apply_plan(
            plan,
            db_path=db_path,
            library=library,
            apply=True,
            confirm=False,
            rollback_dir=tmp_path / "rb",
            db=db,
        )
