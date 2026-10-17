"""Metadata Ownership Isolation contract — internal_id is the only ownership key."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.metadata_backup import (
    backup_root,
    restore_info_sidecar_from_backup,
    sync_metadata_backup,
)
from services.metadata_owner_guard import (
    metadata_payload_is_foreign,
    resolve_owner_mod_id_from_info,
)
from services.mod_metadata_resolver import resolve_mod_metadata
from tools.metadata_ownership_audit import run_audit
from tools.metadata_ownership_common import CODE_METADATA_FOREIGN_OWNER, DECISION_APPROVE
from tools.metadata_ownership_repair import apply_plan, build_preview

STARDEW = 413150
BG3 = 1086940
SHARED_WS = "1333"
BG3_DESC = "Community Library is a BG3 dependency framework used by many mods."
SD_TITLE = "Carry Chest"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "meta_own.db")
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


def _seed_cross_game_1333(
    db: DatabaseManager, library: Path, data: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, Path, Path]:
    monkeypatch.setattr("core.paths.data_dir", lambda: data)
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: data)

    bg3 = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=SHARED_WS,
        source_url="https://www.nexusmods.com/baldursgate3/mods/1333",
        title="Community Library",
        app_id=BG3,
        game_name="Baldurs Gate 3",
        operation="import",
    )
    sd = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=SHARED_WS,
        source_url="https://www.nexusmods.com/stardewvalley/mods/1333",
        title=SD_TITLE,
        app_id=STARDEW,
        game_name="Stardew Valley",
        operation="import",
    )
    bg3_mid = str(bg3.mod_id)
    sd_mid = str(sd.mod_id)
    db.update_mod_identity_fields(bg3_mid, internal_id=bg3_mid, workspace_id=SHARED_WS)
    db.update_mod_identity_fields(sd_mid, internal_id=sd_mid, workspace_id=SHARED_WS)

    bg3_folder = library / "Baldurs Gate 3" / "Community Library"
    bg3_folder.mkdir(parents=True)
    (bg3_folder / "pak.bin").write_bytes(b"bg3")
    _write_info(
        bg3_folder,
        {
            "internal_id": bg3_mid,
            "workspace_id": SHARED_WS,
            "external_id": SHARED_WS,
            "platform": PLATFORM_NEXUS,
            "app_id": BG3,
            "title": "Community Library",
            "description": BG3_DESC,
            "url": "https://www.nexusmods.com/baldursgate3/mods/1333",
        },
    )
    db.update_mod_identity_fields(
        bg3_mid, last_known_path=str(bg3_folder.resolve()), folder_present=True
    )
    db.update_mod_platform_info(bg3_mid, description=BG3_DESC)

    sd_folder = library / "Stardew Valley" / SD_TITLE
    sd_folder.mkdir(parents=True)
    (sd_folder / "manifest.json").write_text("{}", encoding="utf-8")
    # Polluted: Stardew entity carries BG3 description text (historical metadata leak).
    _write_info(
        sd_folder,
        {
            "internal_id": sd_mid,
            "workspace_id": SHARED_WS,
            "external_id": SHARED_WS,
            "platform": PLATFORM_NEXUS,
            "app_id": STARDEW,
            "title": SD_TITLE,
            "description": BG3_DESC,
            "url": "https://www.nexusmods.com/stardewvalley/mods/1333",
        },
    )
    db.update_mod_identity_fields(
        sd_mid, last_known_path=str(sd_folder.resolve()), folder_present=True
    )
    db.update_mod_platform_info(sd_mid, description=BG3_DESC)

    sync_metadata_backup(bg3_folder)
    sync_metadata_backup(sd_folder)
    return bg3_mid, sd_mid, bg3_folder, sd_folder


def test_1_bg3_metadata_cannot_show_on_stardew(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    library = tmp_path / "mod"
    bg3_mid, sd_mid, bg3_folder, sd_folder = _seed_cross_game_1333(
        db, library, data, monkeypatch
    )

    # Inject foreign BG3 app_id into Stardew backup — resolver must refuse it.
    bak = backup_root(sd_mid) / "metadata.json"
    payload = json.loads(bak.read_text(encoding="utf-8"))
    payload["app_id"] = BG3
    payload["url"] = "https://www.nexusmods.com/baldursgate3/mods/1333"
    payload["description"] = BG3_DESC
    bak.write_text(json.dumps(payload), encoding="utf-8")
    # Also poison .info with foreign app evidence
    _write_info(
        sd_folder,
        {
            "internal_id": sd_mid,
            "app_id": BG3,
            "url": "https://www.nexusmods.com/baldursgate3/mods/1333",
            "description": BG3_DESC,
            "title": "Community Library",
        },
    )

    resolved = resolve_mod_metadata(sd_mid, sd_folder)
    assert resolved is not None
    assert resolved.app_id == STARDEW
    # Foreign .info/backup must not supply title/url; entity title remains Stardew.
    assert resolved.display_name == SD_TITLE or resolved.title == SD_TITLE
    assert "baldursgate3" not in (resolved.source_url or "").lower()
    assert "Community Library" not in (resolved.display_name or "")
    assert "Community Library" not in (resolved.title or "")

    bg3_resolved = resolve_mod_metadata(bg3_mid, bg3_folder)
    assert bg3_resolved is not None
    assert bg3_resolved.app_id == BG3
    assert BG3_DESC in (bg3_resolved.description or "")


def test_2_stardew_metadata_cannot_overwrite_bg3(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    library = tmp_path / "mod"
    bg3_mid, sd_mid, bg3_folder, sd_folder = _seed_cross_game_1333(
        db, library, data, monkeypatch
    )
    # Poison BG3 backup with Stardew URL/app
    bak = backup_root(bg3_mid) / "metadata.json"
    payload = json.loads(bak.read_text(encoding="utf-8"))
    payload["app_id"] = STARDEW
    payload["url"] = "https://www.nexusmods.com/stardewvalley/mods/1333"
    payload["description"] = "Stardew only text that must not win"
    bak.write_text(json.dumps(payload), encoding="utf-8")

    resolved = resolve_mod_metadata(bg3_mid, bg3_folder)
    assert resolved is not None
    assert resolved.app_id == BG3
    assert "Stardew only text" not in (resolved.description or "")
    # Owned .info still wins when not foreign
    assert BG3_DESC in (resolved.description or "")


def test_3_same_workspace_different_app_coexist(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    library = tmp_path / "mod"
    bg3_mid, sd_mid, bg3_folder, sd_folder = _seed_cross_game_1333(
        db, library, data, monkeypatch
    )
    assert bg3_mid != sd_mid
    r1 = resolve_mod_metadata(bg3_mid, bg3_folder)
    r2 = resolve_mod_metadata(sd_mid, sd_folder)
    assert r1 and r2
    assert r1.workspace_id == r2.workspace_id == SHARED_WS
    assert r1.app_id == BG3
    assert r2.app_id == STARDEW
    assert r1.published_file_id == bg3_mid
    assert r2.published_file_id == sd_mid


def test_4_cache_regen_does_not_pollute(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    library = tmp_path / "mod"
    bg3_mid, sd_mid, bg3_folder, sd_folder = _seed_cross_game_1333(
        db, library, data, monkeypatch
    )
    # Clear polluted SD description then regenerate backup
    plan = build_preview(
        db_path=tmp_path / "meta_own.db",
        mod_root=library,
        data_root=data,
    )
    # Ensure audit sees cross-app identical description
    codes = {
        f.get("code")
        for f in (run_audit(db_path=tmp_path / "meta_own.db", mod_root=library, data_root=data).get("findings") or [])
    }
    assert CODE_METADATA_FOREIGN_OWNER in codes

    for item in plan["items"]:
        if item.get("mod_id") == sd_mid:
            item["manual_decision"] = DECISION_APPROVE
    result = apply_plan(
        plan,
        db_path=tmp_path / "meta_own.db",
        mod_root=library,
        data_root=data,
        dry_run=False,
    )
    assert result["applied_count"] >= 1

    sync_metadata_backup(sd_folder)
    sync_metadata_backup(bg3_folder)
    sd_bak = json.loads((backup_root(sd_mid) / "metadata.json").read_text(encoding="utf-8"))
    bg3_bak = json.loads((backup_root(bg3_mid) / "metadata.json").read_text(encoding="utf-8"))
    assert not (sd_bak.get("description") or "").strip() or sd_bak.get("description") != BG3_DESC
    assert BG3_DESC in (bg3_bak.get("description") or "")


def test_5_backup_restore_refuses_cross_game(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("core.paths.data_dir", lambda: data)
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: data)

    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id="9991",
        source_url="https://www.nexusmods.com/stardewvalley/mods/9991",
        title="Local",
        app_id=STARDEW,
        game_name="Stardew Valley",
        operation="import",
    )
    mid = str(created.mod_id)
    db.update_mod_identity_fields(mid, internal_id=mid, workspace_id="9991", app_id=STARDEW)

    folder = tmp_path / "mod" / "Stardew Valley" / "Local"
    folder.mkdir(parents=True)
    db.update_mod_identity_fields(mid, last_known_path=str(folder), folder_present=True)

    bak = backup_root(mid)
    bak.mkdir(parents=True)
    (bak / "metadata.json").write_text(
        json.dumps(
            {
                "internal_id": mid,
                "workspace_id": "9991",
                "app_id": BG3,
                "url": "https://www.nexusmods.com/baldursgate3/mods/9991",
                "title": "Foreign",
                "description": "BG3 text",
            }
        ),
        encoding="utf-8",
    )
    assert restore_info_sidecar_from_backup(mid, folder, db=db) is False
    assert not (folder / INFO_DIR_NAME / METADATA_FILENAME).is_file()


def test_6_source_url_cannot_override_internal_id(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    library = tmp_path / "mod"
    bg3_mid, sd_mid, _bg3_folder, sd_folder = _seed_cross_game_1333(
        db, library, data, monkeypatch
    )
    info = {
        "internal_id": sd_mid,
        "url": "https://www.nexusmods.com/baldursgate3/mods/1333",
        "app_id": BG3,
        "published_file_id": bg3_mid,
        "workspace_id": SHARED_WS,
        "external_id": SHARED_WS,
    }
    assert resolve_owner_mod_id_from_info(info) == sd_mid
    assert metadata_payload_is_foreign(info, entity_app_id=STARDEW) is True
    # published_file_id alone must not become owner
    assert resolve_owner_mod_id_from_info({"published_file_id": bg3_mid}) == ""


def test_7_folder_rename_does_not_change_ownership(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    library = tmp_path / "mod"
    _bg3_mid, sd_mid, _bg3_folder, sd_folder = _seed_cross_game_1333(
        db, library, data, monkeypatch
    )
    renamed = sd_folder.parent / "1333"
    sd_folder.rename(renamed)
    db.update_mod_identity_fields(sd_mid, last_known_path=str(renamed.resolve()))
    resolved = resolve_mod_metadata(sd_mid, renamed)
    assert resolved is not None
    assert resolved.published_file_id == sd_mid
    assert resolved.app_id == STARDEW
    # Folder digit name must not be used as backup owner
    sync_metadata_backup(renamed)
    assert (backup_root(sd_mid) / "metadata.json").is_file()
    assert not (data / "mod_backup" / "1333" / "metadata.json").is_file()


def test_guard_source_invariants() -> None:
    sync_src = (Path(__file__).resolve().parents[1] / "services" / "metadata_backup_sync.py").read_text(
        encoding="utf-8"
    )
    assert "resolve_owner_mod_id_from_info" in sync_src
    assert 'data.get("published_file_id")' not in sync_src
    bak_src = (Path(__file__).resolve().parents[1] / "services" / "metadata_backup.py").read_text(
        encoding="utf-8"
    )
    assert "if root.name.isdigit():" not in bak_src or "mark_missing" in bak_src
    # Absent-folder path must not use root.name as mid
    assert "Never treat folder/workspace digits as ownership" in bak_src
