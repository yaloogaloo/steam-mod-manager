"""Regression: mod.io metadata refresh must stay idempotent after path lifecycle."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_MODIO
from services.modio_api import ModioAPIError, ModioModDetails, map_mod_object
from services.modio_metadata_refresh import refresh_modio_mod_metadata
from services.mod_refresh import refresh_mod
from services.path_lifecycle import PathLifecycleStage
from ui.metadata_refresh_thread import ModRefreshWorker


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "modio_regression.db")
    yield manager
    DatabaseManager.reset_instance()


def _sample_details(**overrides) -> ModioModDetails:
    data = {
        "id": 4228735,
        "game_id": 6715,
        "name": "Better Inventory UI",
        "name_id": "better-inventory-ui1",
        "summary": "summary",
        "description": "description",
        "profile_url": "https://mod.io/g/baldursgate3/m/better-inventory-ui1",
        "logo": {"original": "https://example.com/logo.png"},
    }
    data.update(overrides)
    return map_mod_object(data)


def _modio_folder(lib: Path, name: str, *, url: str, mid: str) -> Path:
    folder = lib / "Game" / name
    info = folder / ".info"
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": name,
                "url": url,
                "source_type": "modio",
                "workspace_id": "17878864628646656",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(folder / "payload.zip", "w") as zf:
        zf.writestr("mod.txt", "x")
    return folder


def _register_modio_mod(
    db: DatabaseManager,
    lib: Path,
    url: str,
    *,
    folder_name: str = "better-inventory-ui1",
) -> tuple[str, Path]:
    """Register mod.io mod and return (mod_id, folder path)."""
    folder = _modio_folder(lib, folder_name, url=url, mid="")
    info = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="better-inventory-ui1",
        source_url=url,
        title=folder_name,
        app_id=1086940,
        game_name="Baldur's Gate 3",
    )
    mid = info.mod_id
    sidecar = folder / ".info" / "metadata.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["published_file_id"] = mid
    sidecar.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    db.update_mod_identity_fields(mid, last_known_path=str(folder.resolve()))
    return mid, folder


class FakeModioClient:
    def __init__(self, details: ModioModDetails | None = None) -> None:
        self.details = details or _sample_details()

    def resolve_mod(self, **kwargs):
        return self.details

    def download_file(self, url, dest):
        Path(dest).write_bytes(b"\x89PNG\r\n")
        return Path(dest)

    def close(self):
        return None


def test_case1_slug_external_id_refresh_success(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Import state: external_id slug, no modio_mod_id → refresh succeeds."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/baldursgate3/m/better-inventory-ui1"
    mid, folder = _register_modio_mod(db, lib, url)

    monkeypatch.setattr(
        "services.importers.image_picker.validate_cover_image",
        lambda path: Path(path),
    )
    result = refresh_modio_mod_metadata(
        mid,
        folder,
        library_root=lib,
        client=FakeModioClient(),
        download_cover=False,
        db=db,
    )
    assert result.success is True
    assert result.title == "Better Inventory UI"


def test_case2_rename_updates_last_known_path(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rename during refresh updates last_known_path to new folder."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/baldursgate3/m/better-inventory-ui1"
    mid, old = _register_modio_mod(db, lib, url)

    monkeypatch.setattr(
        "services.importers.image_picker.validate_cover_image",
        lambda path: Path(path),
    )
    result = refresh_mod(
        mid,
        old,
        platform=PLATFORM_MODIO,
        library_root=lib,
        source_url=url,
        db=db,
    )
    assert result.official_success is True
    row = db.get_mod_backup_row(mid) or {}
    lkp = Path(str(row.get("last_known_path") or ""))
    assert lkp.name == "Better Inventory UI"
    assert lkp.is_dir()
    assert not old.exists()


def test_case3_double_refresh_both_succeed(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two consecutive refreshes succeed (second uses stale UI path)."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/baldursgate3/m/better-inventory-ui1"
    mid, old = _register_modio_mod(db, lib, url)

    monkeypatch.setattr(
        "services.importers.image_picker.validate_cover_image",
        lambda path: Path(path),
    )
    client = FakeModioClient()
    monkeypatch.setattr(
        "services.modio_metadata_refresh.ModioClient",
        lambda *a, **k: client,
    )

    first = refresh_mod(mid, old, platform=PLATFORM_MODIO, library_root=lib, source_url=url, db=db)
    assert first.official_success is True
    compat1 = first.to_metadata_refresh_result()
    assert compat1.success is True

    second = refresh_mod(mid, old, platform=PLATFORM_MODIO, library_root=lib, source_url=url, db=db)
    assert second.success is True
    compat2 = second.to_metadata_refresh_result()
    assert compat2.success is True


def test_case4_cover_failure_still_succeeds(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API success + cover download failure → refresh still succeeds."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/baldursgate3/m/better-inventory-ui1"
    mid, folder = _register_modio_mod(db, lib, url)

    class CoverFailClient(FakeModioClient):
        def download_file(self, url, dest):
            raise ModioAPIError("cover down", status_code=500)

    result = refresh_modio_mod_metadata(
        mid,
        folder,
        library_root=lib,
        client=CoverFailClient(),
        download_cover=True,
        db=db,
    )
    assert result.success is True
    assert "封面下载失败" in (result.message or "")


def test_case5_db_identity_failure_is_explicit(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DB identity update failure returns DB_WRITE stage with new path."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/baldursgate3/m/better-inventory-ui1"
    mid, folder = _register_modio_mod(db, lib, url)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated db identity failure")

    monkeypatch.setattr(
        "services.modio_metadata_refresh._update_modio_db_identity",
        _boom,
    )
    result = refresh_modio_mod_metadata(
        mid,
        folder,
        library_root=lib,
        client=FakeModioClient(),
        download_cover=False,
        db=db,
    )
    assert result.success is False
    assert PathLifecycleStage.DB_WRITE.value in (result.error or "").lower()
    assert result.managed_path is not None
    assert result.managed_path.name == "Better Inventory UI"
    assert result.managed_path.is_dir()


def test_backup_sync_failure_does_not_fail_refresh(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Path commit backup sync error must not abort metadata refresh."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/baldursgate3/m/better-inventory-ui1"
    mid, folder = _register_modio_mod(db, lib, url)

    def _backup_boom(*args, **kwargs):
        raise OSError("backup sync simulated failure")

    monkeypatch.setattr(
        "services.metadata_backup_sync.sync_after_metadata_change",
        _backup_boom,
    )

    result = refresh_modio_mod_metadata(
        mid,
        folder,
        library_root=lib,
        client=FakeModioClient(),
        download_cover=False,
        db=db,
    )
    assert result.success is True
    assert result.managed_path is not None
    assert result.managed_path.name == "Better Inventory UI"


def test_worker_heals_stale_path_after_rename(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker with stale path still completes refresh."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/baldursgate3/m/better-inventory-ui1"
    mid, old = _register_modio_mod(db, lib, url)

    finished: list = []

    class _Worker(ModRefreshWorker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.refresh_finished.connect(lambda r: finished.append(r))

    monkeypatch.setattr(
        "services.importers.image_picker.validate_cover_image",
        lambda path: Path(path),
    )
    monkeypatch.setattr(
        "services.modio_metadata_refresh.ModioClient",
        lambda *a, **k: FakeModioClient(),
    )

    worker = _Worker(
        old,
        mod_id=mid,
        library_root=lib,
        platform=PLATFORM_MODIO,
        source_url=url,
    )
    worker.run()
    assert finished
    assert finished[0].success is True
