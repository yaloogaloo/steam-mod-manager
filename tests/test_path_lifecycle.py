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


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "path_lifecycle.db")
    yield manager
    DatabaseManager.reset_instance()


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


def _steam_folder(lib: Path, mid: str, *, name: str = "") -> Path:
    folder = lib / "Game" / (name or f"Unknown_Mod_{mid}")
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": name or f"Unknown_Mod_{mid}",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(folder / "payload.zip", "w") as zf:
        zf.writestr("mod.xml", "<Mod/>")
    return folder


def _nexus_folder(lib: Path, mid: str, *, name: str) -> Path:
    folder = lib / "Game" / name
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
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
    mid = reg.mod_id
    db.update_mod_identity_fields(mid, last_known_path=str(folder.resolve()))

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


def test_nexus_manual_rename_then_refresh_with_stale_path(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Case 2: Nexus manual rename + refresh heals path and marks synced."""
    lib = tmp_path / "mod"
    db.upsert_game(GameInfo(app_id=1623730, name="Palworld", folder_name="Game"))
    old = lib / "Game" / "OldNexus"
    info = old / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps({"title": "OldNexus", "source_type": "nexus"}, ensure_ascii=False),
        encoding="utf-8",
    )
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
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": "OldNexus",
                "source_type": "nexus",
                "platform": "nexus",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db.update_mod_identity_fields(mid, last_known_path=str(old.resolve()))

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


def test_steam_refresh_rename_then_stale_path_succeeds(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 3: Steam refresh rename; stale path on next refresh still works."""
    mid = "3413524002"
    lib = tmp_path / "mod"
    folder = _steam_folder(lib, mid)
    db.upsert_mod(ModMetadata(published_file_id=mid, title=f"Unknown_Mod_{mid}"))
    db.update_mod_identity_fields(mid, last_known_path=str(folder.resolve()))

    fresh = ModMetadata(
        published_file_id=mid,
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


def test_worker_heals_stale_path_after_rename(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case 4: Worker constructed with old path recovers via resolve at run()."""
    lib = tmp_path / "mod"
    mid = "3413524003"
    old = lib / "Game" / "before"
    old.mkdir(parents=True)
    (old / INFO_DIR_NAME).mkdir()
    (old / INFO_DIR_NAME / "metadata.json").write_text(
        json.dumps({"published_file_id": mid, "title": "before"}),
        encoding="utf-8",
    )
    db.upsert_mod(ModMetadata(published_file_id=mid, title="before"))
    db.update_mod_identity_fields(mid, last_known_path=str(old.resolve()))

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
    mid = reg.mod_id
    db.update_mod_identity_fields(mid, last_known_path=str(folder.resolve()))

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
    sidecar = json.loads(
        (result.managed_path / INFO_DIR_NAME / "metadata.json").read_text(encoding="utf-8")
    )
    assert sidecar.get("modio_mod_id") == 424242


def test_manual_move_reconcile_via_library_scan(
    db: DatabaseManager, tmp_path: Path
) -> None:
    """Case 6: User manually moves folder; library reconcile updates last_known_path."""
    lib = tmp_path / "mod"
    db.upsert_game(GameInfo(app_id=1623730, name="Palworld", folder_name="Game"))
    old = _nexus_folder(lib, "pending", name="ManualOld")
    reg = db.register_external_mod(
        platform=PLATFORM_NEXUS,
        external_id="4004",
        source_url="https://nexusmods.com/y",
        title="ManualOld",
        app_id=1623730,
        game_name="Palworld",
    )
    mid = str(reg.mod_id)
    (old / INFO_DIR_NAME / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": "ManualOld",
                "source_type": "nexus",
                "platform": "nexus",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    db.update_mod_identity_fields(
        mid,
        last_known_path=str(old.resolve()),
        folder_present=True,
        platform=PLATFORM_NEXUS,
    )

    new = old.parent / "ManualNew"
    old.rename(new)

    result = reconcile_library(lib)
    assert result.renamed >= 1
    row = db.get_mod_backup_row(mid) or {}
    assert Path(str(row.get("last_known_path") or "")).resolve() == new.resolve()


def test_commit_path_change_reports_stage_on_db_failure(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "Game" / "ModA"
    folder.mkdir(parents=True)
    (folder / INFO_DIR_NAME).mkdir()
    (folder / INFO_DIR_NAME / "metadata.json").write_text("{}", encoding="utf-8")
    mid = "9000000000004005"
    db.upsert_mod(ModMetadata(published_file_id=mid, title="ModA"))

    def _fail(**kwargs):
        raise OSError("db locked")

    monkeypatch.setattr(db, "update_mod_identity_fields", _fail)
    out = commit_path_change(mid, old_path=None, new_path=folder, db=db)
    assert not out.success
    assert out.stage == PathLifecycleStage.DB_WRITE
    assert out.new_path == folder.resolve()
    assert out.error
