"""Path lifecycle — rename/move consistency across refresh, worker, and reconcile."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.models import ModMetadata
from core.mod_platform import PLATFORM_MODIO, PLATFORM_NEXUS, PLATFORM_STEAM
from core.steam_api import SteamWorkshopClient
from services.file_ops import INFO_DIR_NAME
from services.library_reconcile import reconcile_library
from services.mod_refresh import refresh_mod
from services.modio_api import ModioModDetails, map_mod_object
from services.modio_metadata_refresh import refresh_modio_mod_metadata
from services.path_lifecycle import (
    PathLifecycleStage,
    commit_path_change,
    detect_path_drift,
    record_filesystem_rename,
    resolve_managed_folder,
)
from ui.metadata_refresh_thread import ModRefreshWorker
from tests.helpers.identity import create_steam_test_mod, write_info_sidecar


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "path_lifecycle.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def _isolate_path_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Path Lifecycle validates against library root — bind tests to tmp lib."""
    lib = tmp_path / "mod"
    lib.mkdir(parents=True, exist_ok=True)

    def _lib() -> Path:
        return lib

    monkeypatch.setattr("core.paths.default_mod_library", _lib)
    monkeypatch.setattr("services.mod_path_validation.default_mod_library", _lib)
    monkeypatch.setattr("services.path_lifecycle.default_mod_library", _lib, raising=False)
    return lib


def _entity_frozen_id(db: DatabaseManager, mod_id: int | str) -> str:
    """Read durable ``mods.internal_id`` for a PK (must already be minted)."""
    row = db.get_mod_backup_row(str(mod_id)) or {}
    frozen = str(row.get("internal_id") or "").strip()
    assert frozen, f"Entity {mod_id} missing mods.internal_id"
    assert frozen != str(mod_id), "Frozen internal_id must not collapse to mod_id"
    return frozen


def _bind_managed_entity(
    db: DatabaseManager,
    folder: Path,
    *,
    mod_id: int | str,
    title: str,
    platform: str,
    external_id: str,
    workspace_id: str = "",
    app_id: int = 0,
    game_name: str = "",
    extra: dict | None = None,
) -> str:
    """Write Frozen ``.info/entity_key`` + bind ``last_known_path`` (fixture only).

    entity_key value equals Entity ``mods.internal_id`` — filesystem binding only,
    not a third Mod ID.
    """
    mid = str(mod_id)
    frozen = _entity_frozen_id(db, mid)
    ws = str(workspace_id or external_id or "").strip()
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title=title,
        external_id=str(external_id or ""),
        workspace_id=ws,
        app_id=int(app_id or 0),
        game_name=game_name,
        platform=platform,
        extra=extra,
    )
    db.update_mod_identity_fields(
        mid,
        last_known_path=str(folder.resolve()),
        folder_present=True,
        title=title,
        game_name=game_name or None,
    )
    return frozen


def _modio_folder(lib: Path, name: str, *, url: str) -> Path:
    folder = lib / "Game" / name
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {"title": name, "url": url, "source_type": "modio"},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(folder / "payload.zip", "w") as zf:
        zf.writestr("mod.txt", "x")
    return folder


def _steam_folder(lib: Path, workshop: str, *, name: str = "") -> Path:
    folder = lib / "Game" / (name or f"Unknown_Mod_{workshop}")
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": workshop,
                "title": name or f"Unknown_Mod_{workshop}",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(folder / "payload.zip", "w") as zf:
        zf.writestr("mod.xml", "<Mod/>")
    return folder


def _nexus_folder(lib: Path, *, name: str, external_id: str = "") -> Path:
    folder = lib / "Game" / name
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": str(external_id or ""),
                "title": name,
                "source_type": "nexus",
                "platform": "nexus",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(folder / "payload.zip", "w") as zf:
        zf.writestr("mod.txt", "x")
    return folder


def test_modio_refresh_rename_then_stale_path_still_succeeds(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 1: mod.io refresh rename; second call with old path heals via lifecycle."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/anno-1800/m/harborlife"
    folder = _modio_folder(lib, "OldName", url=url)
    reg = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="harborlife",
        source_url=url,
        title="OldName",
        app_id=916440,
        game_name="Anno 1800",
    )
    mid = str(reg.mod_id)
    row = db.get_mod_backup_row(mid) or {}
    _bind_managed_entity(
        db,
        folder,
        mod_id=mid,
        title="OldName",
        platform=PLATFORM_MODIO,
        external_id="harborlife",
        workspace_id=str(row.get("workspace_id") or "harborlife"),
        app_id=916440,
        game_name="Anno 1800",
        extra={"url": url, "source_type": "modio"},
    )

    details = map_mod_object(
        {
            "id": 424242,
            "game_id": 1111,
            "name": "Harbor Life",
            "name_id": "harborlife",
            "summary": "s",
            "description": "d",
            "profile_url": url,
            "logo": {"original": "https://example.com/logo.png"},
        }
    )

    class FakeClient:
        def resolve_mod(self, **kwargs):
            return details

        def download_file(self, url, dest):
            Path(dest).write_bytes(b"\x89PNG\r\n")
            return Path(dest)

        def close(self):
            return None

    monkeypatch.setattr(
        "services.importers.image_picker.validate_cover_image",
        lambda path: Path(path),
    )

    first = refresh_modio_mod_metadata(
        mid, folder, library_root=lib, client=FakeClient(), db=db  # type: ignore[arg-type]
    )
    assert first.success and first.renamed
    new_path = first.managed_path
    assert new_path is not None
    assert not folder.exists()

    second = refresh_mod(
        mid,
        folder,
        platform=PLATFORM_MODIO,
        library_root=lib,
        source_url=url,
        db=db,
    )
    assert second.success
    healed = resolve_managed_folder(mid, hint_path=folder, db=db)
    assert healed.path == new_path
    assert healed.resolved_from == "last_known_path"
    after = db.get_mod_backup_row(mid) or {}
    assert str(after.get("internal_id") or "") == _entity_frozen_id(db, mid)


def test_nexus_manual_rename_then_refresh_with_stale_path(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Case 2: Nexus manual rename + refresh heals path and marks synced."""
    lib = tmp_path / "mod"
    db.upsert_game(GameInfo(app_id=1623730, name="Palworld", folder_name="Game"))
    old = lib / "Game" / "OldNexus"
    old.mkdir(parents=True)
    (old / INFO_DIR_NAME).mkdir(parents=True)
    with zipfile.ZipFile(old / "payload.zip", "w") as zf:
        zf.writestr("mod.txt", "x")
    reg = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="4001",
        source_url="https://nexusmods.com/x",
        title="OldNexus",
        app_id=1623730,
        game_name="Palworld",
    )
    mid = str(reg.mod_id)
    row = db.get_mod_backup_row(mid) or {}
    _bind_managed_entity(
        db,
        old,
        mod_id=mid,
        title="OldNexus",
        platform=PLATFORM_NEXUS,
        external_id="4001",
        workspace_id=str(row.get("workspace_id") or "4001"),
        app_id=1623730,
        game_name="Palworld",
        extra={"source_type": "nexus"},
    )

    new = old.parent / "RenamedNexus"
    old.rename(new)
    drift = detect_path_drift(mid, new, db=db)
    assert drift is not None and drift.success
    assert drift.new_path == new.resolve()

    result = refresh_mod(mid, old, platform=PLATFORM_NEXUS, library_root=lib, db=db)
    assert result.success
    assert db.is_official_metadata_synced(mid)
    row = db.get_mod_backup_row(mid) or {}
    assert Path(str(row.get("last_known_path") or "")).resolve() == new.resolve()
    assert str(row.get("internal_id") or "")
    assert str(row.get("workspace_id") or "") == "4001"


def test_steam_refresh_rename_then_stale_path_succeeds(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 3: Steam refresh rename; stale path on next refresh still works."""
    workshop = "3413524002"
    lib = tmp_path / "mod"
    folder = _steam_folder(lib, workshop)
    created = create_steam_test_mod(
        db, external_id=workshop, title=f"Unknown_Mod_{workshop}"
    )
    mid = str(created.mod_id)
    _bind_managed_entity(
        db,
        folder,
        mod_id=mid,
        title=f"Unknown_Mod_{workshop}",
        platform=PLATFORM_STEAM,
        external_id=workshop,
        workspace_id=workshop,
    )

    fresh = ModMetadata(
        published_file_id=workshop,
        title="Official Steam Title",
        description="desc",
    )
    monkeypatch.setattr(SteamWorkshopClient, "refresh_details", lambda *a, **k: [fresh])
    monkeypatch.setattr(SteamWorkshopClient, "fetch_and_save_cover", lambda *a, **k: None)

    first = refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    assert first.official_success
    new_path = first.managed_path
    assert new_path is not None
    assert new_path.name == "Official Steam Title"

    db.set_official_metadata_synced(mid, False)
    second = refresh_mod(mid, folder, platform=PLATFORM_STEAM, library_root=lib, db=db)
    assert second.success
    healed = resolve_managed_folder(mid, hint_path=folder, db=db)
    assert healed.path == new_path
    assert str((db.get_mod_backup_row(mid) or {}).get("internal_id") or "") == (
        str(created.internal_id or "")
    )


def test_worker_heals_stale_path_after_rename(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 4: Worker constructed with old path recovers via resolve at run()."""
    lib = tmp_path / "mod"
    workshop = "3413524003"
    old = lib / "Game" / "before"
    old.mkdir(parents=True)
    (old / INFO_DIR_NAME).mkdir()
    created = create_steam_test_mod(db, external_id=workshop, title="before")
    mid = str(created.mod_id)
    _bind_managed_entity(
        db,
        old,
        mod_id=mid,
        title="before",
        platform=PLATFORM_STEAM,
        external_id=workshop,
        workspace_id=workshop,
    )

    new = lib / "Game" / "after"
    old.rename(new)
    record_filesystem_rename(mid, old, new, reason="refresh", db=db)

    calls: list[Path] = []

    def _fake_refresh(mod_id, managed_path, **kwargs):
        calls.append(Path(managed_path))
        from services.mod_refresh import ModRefreshResult, reconcile_local_state

        local = reconcile_local_state(mod_id, managed_path, db=db)
        return ModRefreshResult(
            mod_id=str(mod_id),
            success=True,
            local=local,
            official_attempted=False,
            official_synced=True,
            managed_path=Path(managed_path),
            message="ok",
        )

    monkeypatch.setattr("services.mod_refresh.refresh_mod", _fake_refresh)
    worker = ModRefreshWorker(old, mod_id=mid, library_root=lib, platform=PLATFORM_STEAM)
    worker.run()
    assert calls
    assert calls[0].resolve() == new.resolve()


def test_db_identity_failure_after_path_commit_leaves_no_orphan(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 5: DB platform update failure after path commit — disk/DB path aligned."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/anno-1800/m/harborlife"
    folder = _modio_folder(lib, "OldName", url=url)
    reg = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="harborlife",
        source_url=url,
        title="OldName",
        app_id=916440,
        game_name="Anno 1800",
    )
    mid = str(reg.mod_id)
    row = db.get_mod_backup_row(mid) or {}
    frozen_before = _bind_managed_entity(
        db,
        folder,
        mod_id=mid,
        title="OldName",
        platform=PLATFORM_MODIO,
        external_id="harborlife",
        workspace_id=str(row.get("workspace_id") or "harborlife"),
        app_id=916440,
        game_name="Anno 1800",
        extra={"url": url, "source_type": "modio"},
    )

    details = ModioModDetails(
        mod_id=424242,
        game_id=1111,
        name="Harbor Life",
        name_id="harborlife",
        summary="s",
        description="d",
        profile_url=url,
        logo_url="",
        author="",
        raw={},
    )

    class FakeClient:
        def resolve_mod(self, **kwargs):
            return details

        def close(self):
            return None

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated platform db failure")

    monkeypatch.setattr(
        "services.modio_metadata_refresh._update_modio_db_identity",
        _boom,
    )

    result = refresh_modio_mod_metadata(
        mid, folder, library_root=lib, client=FakeClient(), download_cover=False, db=db  # type: ignore[arg-type]
    )
    assert not result.success
    assert "db_write" in (result.error or "").lower()
    assert result.managed_path is not None
    assert result.managed_path.is_dir()
    row = db.get_mod_backup_row(mid) or {}
    assert Path(str(row.get("last_known_path") or "")).resolve() == result.managed_path.resolve()
    assert str(row.get("internal_id") or "") == frozen_before
    sidecar = json.loads(
        (result.managed_path / INFO_DIR_NAME / "metadata.json").read_text(encoding="utf-8")
    )
    assert sidecar.get("modio_mod_id") == 424242
    assert str(sidecar.get("internal_id") or "") == frozen_before
    assert "entity_key" not in sidecar
    assert str(row.get("workspace_id") or "")  # workspace_id unchanged on Entity


def test_manual_move_reconcile_via_library_scan(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Case 6: User manually moves folder; library reconcile updates last_known_path."""
    lib = tmp_path / "mod"
    db.upsert_game(GameInfo(app_id=1623730, name="Palworld", folder_name="Game"))
    old = _nexus_folder(lib, name="ManualOld", external_id="4004")
    reg = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="4004",
        source_url="https://nexusmods.com/y",
        title="ManualOld",
        app_id=1623730,
        game_name="Palworld",
    )
    mid = str(reg.mod_id)
    row = db.get_mod_backup_row(mid) or {}
    frozen = _bind_managed_entity(
        db,
        old,
        mod_id=mid,
        title="ManualOld",
        platform=PLATFORM_NEXUS,
        external_id="4004",
        workspace_id=str(row.get("workspace_id") or "4004"),
        app_id=1623730,
        game_name="Palworld",
        extra={"source_type": "nexus"},
    )

    new = old.parent / "ManualNew"
    old.rename(new)

    result = reconcile_library(lib)
    assert result.renamed >= 1 or mid in result.rebound_ids
    row = db.get_mod_backup_row(mid) or {}
    assert Path(str(row.get("last_known_path") or "")).resolve() == new.resolve()
    assert str(row.get("internal_id") or "") == frozen
    assert str(row.get("workspace_id") or "") == "4004"


def test_commit_path_change_reports_stage_on_db_failure(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "mod" / "Game" / "ModA"
    folder.mkdir(parents=True)
    (folder / INFO_DIR_NAME).mkdir()
    workshop = "3413524005"
    created = create_steam_test_mod(db, external_id=workshop, title="ModA")
    mid = str(created.mod_id)
    write_info_sidecar(
        folder,
        internal_id=str(created.internal_id or ""),
        title="ModA",
        external_id=workshop,
        workspace_id=workshop,
        platform=PLATFORM_STEAM,
    )

    def _fail(**kwargs):
        raise OSError("db locked")

    monkeypatch.setattr(db, "update_mod_identity_fields", _fail)
    out = commit_path_change(mid, old_path=None, new_path=folder, db=db)
    assert not out.success
    assert out.stage == PathLifecycleStage.DB_WRITE
    assert out.new_path == folder.resolve()
    assert out.error
