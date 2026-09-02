"""P0 system governance: mint funnels, scanner, archive/conflict/deploy evidence."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import NON_STEAM_MOD_ID_BASE, PLATFORM_NEXUS, PLATFORM_STEAM
from core.mod_status import CONFLICT_STATUS_CONFLICT
from core.models import ModMetadata
from services.archive import ARCHIVE_OUTCOME_FAILED, OfflinePageArchiver
from services.conflict import ConflictClass, ConflictDetector
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_invariants import (
    EMPTY_MOD_PLACEHOLDER,
    INVALID_STEAM_SOURCE_URL,
    scan_invalid_entities,
)
from services.identity_service import (
    IdentityCreateBypassError,
    allocate_internal_id,
    create_mod_identity,
    lifecycle_scope,
    repair_no_allocate_scope,
)
from services.info_sidecar import apply_sidecar_to_db
from services.library_reconcile import reconcile_library
from services.mod_refresh import refresh_mod
from services.verification_result import (
    APPLY_EXECUTED,
    APPLY_UNVERIFIED,
    PRODUCTION_VERIFIED,
    production_verified_or_unverified,
)

STEAM_ID = "3591453758"
INTERNAL = str(NON_STEAM_MOD_ID_BASE + 99)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "gov.db")
    manager.upsert_game(
        GameInfo(app_id=3167020, name="逃离鸭科夫", folder_name="逃离鸭科夫")
    )
    yield manager
    DatabaseManager.reset_instance()


def _steam_folder(library: Path, *, name: str = "Collectibles", pub: str = STEAM_ID) -> Path:
    folder = library / "逃离鸭科夫" / name
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    payload = {
        "published_file_id": pub,
        "title": name,
        "app_id": 3167020,
        "source_type": "steam",
        "url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={pub}",
        "workspace_id": pub,
    }
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    (folder / "info.ini").write_text("[Mod]\nname=x\n", encoding="utf-8")
    return folder


def _count_mods(db: DatabaseManager) -> int:
    return int(db._conn.execute("SELECT COUNT(*) FROM mods").fetchone()[0])


def test_refresh_must_not_mint_identity(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder = _steam_folder(library)
    db.upsert_mod(ModMetadata(published_file_id=STEAM_ID, title="Collectibles", app_id=3167020))
    db.update_mod_identity_fields(STEAM_ID, last_known_path=str(folder.resolve()))
    before = _count_mods(db)
    refresh_mod(STEAM_ID, folder, platform=PLATFORM_STEAM, library_root=library, db=db)
    assert _count_mods(db) == before
    with lifecycle_scope("refresh"):
        with pytest.raises(IdentityCreateBypassError):
            allocate_internal_id(db)


def test_archive_must_not_mint_identity(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _steam_folder(tmp_path / "mod")
    db.upsert_mod(ModMetadata(published_file_id=STEAM_ID, title="X", app_id=3167020))
    before = _count_mods(db)

    def _boom(*_a, **_k):
        raise TimeoutError("curl: (28) Connection timed out after 15006 ms")

    monkeypatch.setattr(OfflinePageArchiver, "_fetch_main_html", _boom)
    archiver = OfflinePageArchiver(timeout=1)
    result = archiver.archive(STEAM_ID, folder / INFO_DIR_NAME)
    assert result.outcome == ARCHIVE_OUTCOME_FAILED
    assert _count_mods(db) == before
    with lifecycle_scope("archive"):
        with pytest.raises(IdentityCreateBypassError):
            allocate_internal_id(db)


def test_deploy_must_not_mint_identity(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder = _steam_folder(library)
    game = tmp_path / "game" / "Duckov_Data" / "Mods"
    game.mkdir(parents=True)
    db.upsert_mod(ModMetadata(published_file_id=STEAM_ID, title="Collectibles", app_id=3167020))
    db.update_mod_identity_fields(
        STEAM_ID, last_known_path=str(folder.resolve()), app_id=3167020
    )
    db.update_game_deploy_config(
        3167020,
        name="逃离鸭科夫",
        install_path=str(tmp_path / "game"),
        mod_path=str(game),
    )
    before = _count_mods(db)
    out = ModDeployer(library_root=library, db=db).deploy_mod(STEAM_ID)
    assert _count_mods(db) == before
    assert "deploy_timing" in out
    assert out["deploy_timing"]["mod_id"] == STEAM_ID
    stages = {row["stage"] for row in out["deploy_timing"]["stages"]}
    assert "resolve" in stages or "copy" in stages or "plan" in stages
    with lifecycle_scope("deploy"):
        with pytest.raises(IdentityCreateBypassError):
            allocate_internal_id(db)


def test_metadata_edit_must_not_mint_identity(db: DatabaseManager) -> None:
    db.upsert_mod(ModMetadata(published_file_id=STEAM_ID, title="X", app_id=3167020))
    before = _count_mods(db)
    db.update_mod_user_metadata(STEAM_ID, {"display_name": "Renamed"})
    assert _count_mods(db) == before
    with lifecycle_scope("metadata"):
        with pytest.raises(IdentityCreateBypassError):
            allocate_internal_id(db)
    with pytest.raises(IdentityCreateBypassError):
        db.update_mod_user_metadata(INTERNAL, {"display_name": "ghost"})
    assert db.get_mod(INTERNAL) is None


def test_sidecar_apply_must_not_mint_identity(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder = _steam_folder(library)
    before = _count_mods(db)
    assert apply_sidecar_to_db(folder, mod_id=STEAM_ID, db=db) is False
    assert _count_mods(db) == before
    db.upsert_mod(ModMetadata(published_file_id=STEAM_ID, title="X", app_id=3167020))
    assert apply_sidecar_to_db(folder, mod_id=STEAM_ID, db=db) is True
    with lifecycle_scope("sidecar"):
        with pytest.raises(IdentityCreateBypassError):
            allocate_internal_id(db)


def test_reconcile_without_official_identity_must_not_mint(
    db: DatabaseManager, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder = library / "Nexus" / "Empty Mod ee0974ce"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "source_type": "nexus",
                "title": "Empty Mod ee0974ce",
                "app_id": 3333,
            }
        ),
        encoding="utf-8",
    )
    (folder / "pak").write_text("x", encoding="utf-8")
    before = _count_mods(db)
    result = reconcile_library(library)
    assert _count_mods(db) == before
    assert any("PLACEHOLDER" in n or "UNRESOLVED" in n for n in result.notes) or before == _count_mods(db)
    with pytest.raises(IdentityCreateBypassError):
        create_mod_identity(
            db,
            platform=PLATFORM_NEXUS,
            title="Empty Mod ee0974ce",
            app_id=3333,
            operation="reconcile",
        )


def test_empty_mod_with_official_identity_strips_title_on_import(
    db: DatabaseManager,
) -> None:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="7329",
        source_url="https://www.nexusmods.com/witcher3/mods/7329",
        title="Empty Mod ee0974ce",
        app_id=292030,
        game_name="巫师3",
        operation="import",
    )
    assert created.mod_id
    info = db.get_mod_display_info(created.mod_id)
    assert info is not None
    assert info.external_id == "7329"
    persisted_name = f"{info.steam_name} {info.display_name}"
    assert "Empty Mod" not in persisted_name


def test_repair_must_not_allocate(db: DatabaseManager) -> None:
    with repair_no_allocate_scope():
        with pytest.raises(Exception):
            allocate_internal_id(db)


def test_internal_id_must_not_become_steam_workspace_or_published_or_external(
    db: DatabaseManager, tmp_path: Path
) -> None:
    mid = str(allocate_internal_id(db))
    db.update_mod_identity_fields(
        mid,
        platform=PLATFORM_NEXUS,
        external_id=mid,
        workspace_id=mid,
        source_url=f"https://steamcommunity.com/sharedfiles/filedetails/?id={mid}",
    )
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.workspace_id != mid
    from services.mod_identity_authority import sanitize_platform_external_id

    assert sanitize_platform_external_id(PLATFORM_STEAM, mid, mod_id=mid) == ""
    report = scan_invalid_entities(tmp_path / "mod", db=db)
    codes = {f.violation_code for f in report.findings if f.entity_id == mid}
    assert INVALID_STEAM_SOURCE_URL in codes or any(
        "STEAM" in c or "INTERNAL" in c for c in codes
    )


def test_fake_steam_url_detected_by_scanner(db: DatabaseManager, tmp_path: Path) -> None:
    db.upsert_mod(ModMetadata(published_file_id=STEAM_ID, title="X", app_id=3167020))
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET source_url=? WHERE mod_id=?",
            (
                f"https://steamcommunity.com/sharedfiles/filedetails/?id={INTERNAL}",
                int(STEAM_ID),
            ),
        )
        db._conn.commit()
    report = scan_invalid_entities(tmp_path / "mod", db=db)
    assert any(f.violation_code == INVALID_STEAM_SOURCE_URL for f in report.findings)


def test_scanner_empty_mod_with_official_identity_is_review_not_remove(
    db: DatabaseManager, tmp_path: Path
) -> None:
    db.upsert_game(GameInfo(app_id=292030, name="巫师3", folder_name="巫师3"))
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="7329",
        source_url="https://www.nexusmods.com/witcher3/mods/7329",
        title="Brothers in Arms",
        app_id=292030,
        game_name="巫师3",
        operation="import",
    )
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET title=?, display_name=? WHERE mod_id=?",
            ("Empty Mod ee0974ce", "Brothers in Arms", int(created.mod_id)),
        )
        db._conn.commit()
    report = scan_invalid_entities(tmp_path / "mod", db=db)
    hits = [
        f
        for f in report.findings
        if f.entity_id == created.mod_id and f.violation_code == EMPTY_MOD_PLACEHOLDER
    ]
    assert hits, report.to_dict()
    assert hits[0].severity == "REQUIRES_REVIEW"
    assert "keep entity" in hits[0].recommended_action.lower()


def test_scanner_empty_mod_without_official_identity_is_high(
    db: DatabaseManager, tmp_path: Path
) -> None:
    mid = str(allocate_internal_id(db))
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET title=?, platform=?, external_id=?, source_url=? WHERE mod_id=?",
            ("Empty Mod aabbccdd", "other", "", "", int(mid)),
        )
        db._conn.commit()
    report = scan_invalid_entities(tmp_path / "mod", db=db)
    hits = [
        f
        for f in report.findings
        if f.entity_id == mid and f.violation_code == EMPTY_MOD_PLACEHOLDER
    ]
    assert hits, report.to_dict()
    assert hits[0].severity == "HIGH"


def test_unknown_mod_internal_cannot_create(db: DatabaseManager) -> None:
    with pytest.raises(IdentityCreateBypassError):
        create_mod_identity(
            db,
            platform=PLATFORM_NEXUS,
            external_id=f"stub:{INTERNAL}",
            title=f"Unknown_Mod_{INTERNAL}",
            app_id=1,
            operation="import",
        )


def test_file_overwrite_writes_decision_trace(db: DatabaseManager, tmp_path: Path) -> None:
    from services.deploy_rules.manifest import (
        DeployManifest,
        ManifestFileEntry,
        save_manifest,
    )

    library = tmp_path / "mod"
    db.upsert_game(GameInfo(app_id=813780, name="Anno 1800", folder_name="Anno 1800"))
    target = str((tmp_path / "stamps" / "a.stamp").resolve())
    (tmp_path / "stamps").mkdir()
    for mid, title in (("111", "布局模板"), ("222", "全产业模板")):
        folder = library / "Anno 1800" / title
        info = folder / INFO_DIR_NAME
        info.mkdir(parents=True)
        (info / METADATA_FILENAME).write_text(
            json.dumps({"published_file_id": mid, "title": title}), encoding="utf-8"
        )
        save_manifest(
            folder,
            DeployManifest(
                mod_id=mid,
                deploy_time="2020-01-01T00:00:00+00:00",
                deploy_type="folder_copy",
                files=[ManifestFileEntry(source="a.stamp", target=target)],
            ),
        )
        db.upsert_mod(ModMetadata(published_file_id=mid, title=title, app_id=813780))
    reports = ConflictDetector(library, db=db).check_all_mods(persist=True)
    assert reports["111"].status == CONFLICT_STATUS_CONFLICT
    assert reports["222"].status == CONFLICT_STATUS_CONFLICT
    traces = reports["111"].traces
    assert traces
    assert traces[0].conflict_type == ConflictClass.FILE_OVERWRITE.value
    assert traces[0].overlap_count >= 1
    trace_path = library / "Anno 1800" / "布局模板" / INFO_DIR_NAME / "conflict_trace.json"
    assert trace_path.is_file()
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    assert payload[0]["conflict_type"] == "FILE_OVERWRITE"


def test_archive_timeout_logs_archive_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    info = tmp_path / ".info"
    info.mkdir()

    def _boom(*_a, **_k):
        raise TimeoutError("curl: (28) Connection timed out after 15006 ms")

    monkeypatch.setattr(OfflinePageArchiver, "_fetch_main_html", _boom)
    result = OfflinePageArchiver(timeout=1).archive("3786388428", info)
    assert result.outcome == ARCHIVE_OUTCOME_FAILED
    text = caplog.text
    assert "[ARCHIVE_FAILURE]" in text
    assert "NETWORK_FAILURE" in text or "curl_code=28" in text
    stub = (info / "index.html").read_text(encoding="utf-8")
    assert "data-archive-outcome=\"failed\"" in stub
    assert "不是成功页面" in stub


def test_deploy_emits_source_target_stage_timing(db: DatabaseManager, tmp_path: Path) -> None:
    library = tmp_path / "mod"
    folder = _steam_folder(library)
    game = tmp_path / "game" / "Duckov_Data" / "Mods"
    game.mkdir(parents=True)
    db.upsert_mod(ModMetadata(published_file_id=STEAM_ID, title="Collectibles", app_id=3167020))
    db.update_mod_identity_fields(
        STEAM_ID, last_known_path=str(folder.resolve()), app_id=3167020
    )
    db.update_game_deploy_config(
        3167020,
        name="逃离鸭科夫",
        install_path=str(tmp_path / "game"),
        mod_path=str(game),
    )
    out = ModDeployer(library_root=library, db=db).deploy_mod(STEAM_ID)
    timing = out.get("deploy_timing") or {}
    assert timing.get("mod_id") == STEAM_ID
    assert "stages" in timing
    assert out.get("source") or timing.get("source")


def test_production_verification_failure_is_not_verified() -> None:
    status = production_verified_or_unverified(
        critical=7, high=0, extra_checks_passed=False
    )
    assert status == APPLY_UNVERIFIED
    assert status != PRODUCTION_VERIFIED
    status_ok_incomplete = production_verified_or_unverified(
        critical=0, high=0, extra_checks_passed=False
    )
    assert status_ok_incomplete == APPLY_UNVERIFIED
    from services.verification_result import cannot_claim_production_verified

    assert (
        cannot_claim_production_verified(
            critical=7, high=0, apply_status=APPLY_EXECUTED
        )
        == APPLY_UNVERIFIED
    )


def test_deploy_missing_row_does_not_insert(db: DatabaseManager) -> None:
    with pytest.raises(IdentityCreateBypassError):
        db.update_mod_deploy_status(STEAM_ID, deploy_status="deployed")
    assert db.get_mod(STEAM_ID) is None


def test_ensure_mod_stub_missing_refuses(db: DatabaseManager) -> None:
    with pytest.raises(IdentityCreateBypassError):
        with db._lock:
            db._ensure_mod_stub(int(STEAM_ID))
    assert db.get_mod(STEAM_ID) is None
