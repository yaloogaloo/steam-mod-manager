"""Identity Consumer P0 — path consumers unify on internal_id."""

from __future__ import annotations

import inspect
import json
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_NEXUS
from services.deploy_paths import resolve_deploy_managed_path
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.identity_service import create_mod_identity
from services.library_reconcile import reconcile_library
from services.mod_identity import ensure_mod_identity
from services.mod_library_cache import build_library_snapshot
from services.path_lifecycle import resolve_managed_folder

STARDEW = 413150
BG3 = 1086940


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "p0.db")
    manager.upsert_game(GameInfo(app_id=STARDEW, name="Stardew Valley"))
    manager.upsert_game(GameInfo(app_id=BG3, name="Baldurs Gate 3"))
    yield manager
    DatabaseManager.reset_instance()


def _meta(folder: Path, payload: dict) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _seed_nexus(
    db: DatabaseManager,
    *,
    external_id: str,
    app_id: int,
    game: str,
    title: str,
    url: str,
    folder: Path,
    iid: str,
) -> str:
    created = create_mod_identity(
        db,
        platform=PLATFORM_NEXUS,
        external_id=external_id,
        source_url=url,
        title=title,
        app_id=app_id,
        game_name=game,
        operation="import",
    )
    mid = str(created.mod_id)
    db.update_mod_identity_fields(
        mid,
        internal_id=iid,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        workspace_id=external_id,
    )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pak.bin").write_bytes(b"x")
    _meta(
        folder,
        {
            "internal_id": iid,
            "workspace_id": external_id,
            "platform": PLATFORM_NEXUS,
            "app_id": app_id,
            "title": title,
            "url": url,
        },
    )
    return mid


def test_reconcile_rebinds_to_surviving_info_path(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    old = library / "Stardew Valley" / "OldPlace"
    new = library / "Stardew Valley" / "NewPlace"
    iid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    mid = _seed_nexus(
        db,
        external_id="9001",
        app_id=STARDEW,
        game="Stardew Valley",
        title="Carry",
        url="https://www.nexusmods.com/stardewvalley/mods/9001",
        folder=old,
        iid=iid,
    )
    shutil.copytree(old, new)
    shutil.rmtree(old)
    db.update_mod_identity_fields(
        mid, last_known_path=str(old.resolve()), folder_present=False
    )

    reconcile_library(library_root=library)
    row = db.get_mod_backup_row(mid) or {}
    assert Path(str(row.get("last_known_path") or "")).resolve() == new.resolve()
    assert int(row.get("folder_present") or 0) == 1


def test_deploy_same_workspace_different_games_no_cross_bind(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    bg3 = library / "Baldurs Gate 3" / "Community Library"
    sd = library / "Stardew Valley" / "Carry Chest"
    bg3_mid = _seed_nexus(
        db,
        external_id="1333",
        app_id=BG3,
        game="Baldurs Gate 3",
        title="Community Library",
        url="https://www.nexusmods.com/baldursgate3/mods/1333",
        folder=bg3,
        iid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )
    sd_mid = _seed_nexus(
        db,
        external_id="1333",
        app_id=STARDEW,
        game="Stardew Valley",
        title="Carry Chest",
        url="https://www.nexusmods.com/stardewvalley/mods/1333",
        folder=sd,
        iid="cccccccc-cccc-cccc-cccc-cccccccccccc",
    )
    db.update_mod_identity_fields(bg3_mid, last_known_path="")
    db.update_mod_identity_fields(sd_mid, last_known_path="")

    found_bg3 = resolve_deploy_managed_path(bg3_mid, db=db, library_root=library)
    found_sd = resolve_deploy_managed_path(sd_mid, db=db, library_root=library)
    assert found_bg3 is not None and found_bg3.resolve() == bg3.resolve()
    assert found_sd is not None and found_sd.resolve() == sd.resolve()
    assert found_bg3.resolve() != found_sd.resolve()


def test_mismatched_info_internal_id_is_ignored(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    folder = library / "Baldurs Gate 3" / "Community Library"
    mid = _seed_nexus(
        db,
        external_id="1333",
        app_id=BG3,
        game="Baldurs Gate 3",
        title="Community Library",
        url="https://www.nexusmods.com/baldursgate3/mods/1333",
        folder=folder,
        iid="dddddddd-dddd-dddd-dddd-dddddddddddd",
    )
    foreign = library / "Baldurs Gate 3" / "Forged"
    foreign.mkdir(parents=True)
    (foreign / "y.bin").write_bytes(b"2")
    _meta(
        foreign,
        {
            "internal_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "workspace_id": "1333",
            "title": "Community Library",
        },
    )
    bound, _payload, _ = ensure_mod_identity(foreign, db=db)
    assert bound == ""
    resolved = resolve_managed_folder(mid, library_root=library, db=db)
    assert resolved.path is not None
    assert resolved.path.resolve() == folder.resolve()


def test_deploy_recovers_when_last_known_path_deleted(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    folder = library / "Stardew Valley" / "Carry Chest"
    iid = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    mid = _seed_nexus(
        db,
        external_id="777",
        app_id=STARDEW,
        game="Stardew Valley",
        title="Carry Chest",
        url="https://www.nexusmods.com/stardewvalley/mods/777",
        folder=folder,
        iid=iid,
    )
    db.update_mod_identity_fields(
        mid, last_known_path=str((library / "Stardew Valley" / "Gone").resolve())
    )
    found = resolve_deploy_managed_path(mid, db=db, library_root=library)
    assert found is not None
    assert found.resolve() == folder.resolve()
    row = db.get_mod_backup_row(mid) or {}
    assert Path(str(row.get("last_known_path") or "")).resolve() == folder.resolve()


def test_info_without_internal_id_is_not_a_mod(
    tmp_path: Path, db: DatabaseManager
) -> None:
    library = tmp_path / "mod"
    folder = library / "Stardew Valley" / "Nameless"
    folder.mkdir(parents=True)
    (folder / "z.bin").write_bytes(b"z")
    _meta(folder, {"workspace_id": "1333", "title": "Nameless"})
    bound, payload, _ = ensure_mod_identity(folder, db=db)
    assert bound == ""
    assert str(payload.get("identity_status") or "") == "unresolved"
    snap = build_library_snapshot(library)
    # Disk-only folder without internal_id must not become a Library Mod.
    assert all(str(c.id).isdigit() for c in snap.cards)
    assert not any("Nameless" in str(getattr(c, "title", "") or "") for c in snap.cards)
    assert not any(
        "Nameless" in str(getattr(c, "managed_path", "") or "") for c in snap.cards
    )


def test_runtime_modules_forbid_registration_lookups() -> None:
    forbidden_calls = (
        "find_mod_for_registration(",
        "find_mod_by_external(",
        "find_mod_by_workspace_id(",
        "find_mod_id_by_workspace_id(",
    )
    modules = [
        "services.deploy",
        "services.deploy_paths",
        "services.path_lifecycle",
        "services.mod_metadata_resolver",
        "services.mod_library_cache",
        "ui.mod_detail_panel",
    ]
    for name in modules:
        mod = __import__(name, fromlist=["*"])
        src = inspect.getsource(mod)
        for call in forbidden_calls:
            assert call not in src, f"{name} must not call {call}"
    resolve_src = inspect.getsource(
        __import__(
            "services.deploy_paths", fromlist=["resolve_deploy_managed_path"]
        ).resolve_deploy_managed_path
    )
    assert "resolve_managed_folder" in resolve_src
    assert "find_by_published_id" not in resolve_src
    assert "_scan_library_sidecar" not in resolve_src
