"""Regression: mod.io offline archive after refresh / path lifecycle."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import OFFLINE_STATUS_ARCHIVED, PLATFORM_MODIO
from services import archive as archive_mod
from services.archive import OfflinePageArchiver
from services.file_ops import INFO_DIR_NAME
from services.mod_refresh import refresh_mod
from services.modio_api import map_mod_object
from services.offline.manager import OfflineManager
from services.offline.modio import (
    ModioOfflineProvider,
    resolve_modio_offline_page_url,
)
from ui.offline_archive_thread import OfflineArchiveWorker

_RENDERED_MODIO_HTML = """<!DOCTYPE html>
<html><head>
<title>Better Inventory UI for Baldur's Gate 3 - mod.io</title>
<link rel="stylesheet" href="https://cdn.example/modio.css">
</head><body>
<div id="root">
  <h1>Better Inventory UI</h1>
  <section class="description"><h2>Description</h2><p>Inventory UI mod.</p></section>
  <button>Subscribe</button>
</div>
</body></html>
"""


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "modio_offline_regression.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def _isolate_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(archive_mod, "_get_archive_proxy", lambda: None)
    monkeypatch.setattr(archive_mod, "_get_steam_cookie", lambda: None)
    cache_root = tmp_path / "asset_cache"
    cache_root.mkdir(exist_ok=True)
    monkeypatch.setattr(archive_mod, "asset_cache_dir", lambda: cache_root)
    archive_mod.reset_asset_cache_stats()


def _sample_details(**overrides):
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


class FakeModioClient:
    def resolve_mod(self, **kwargs):
        return _sample_details()

    def download_file(self, url, dest):
        Path(dest).write_bytes(b"\x89PNG\r\n")
        return Path(dest)

    def close(self):
        return None


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
                "source_url": url,
                "source_type": "modio",
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


def _fake_asset_get(self: OfflinePageArchiver, url: str, kwargs: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"Content-Type": "text/css"}
    resp.raise_for_status = MagicMock()
    resp.iter_content = MagicMock(return_value=[b"body{color:#111}"])
    resp.close = MagicMock()
    resp.content = b"body{color:#111}"
    return resp


def _install_capture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    captured: list[str] = []

    def fake_capture(url: str) -> str:
        captured.append(url)
        return _RENDERED_MODIO_HTML

    monkeypatch.setattr(OfflinePageArchiver, "_perform_asset_get", _fake_asset_get)
    monkeypatch.setattr(
        OfflinePageArchiver,
        "_perform_get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no html fetch")),
    )
    monkeypatch.setattr(
        "services.modio_metadata_refresh.ModioClient",
        lambda *a, **k: FakeModioClient(),
    )
    monkeypatch.setattr(
        "services.importers.image_picker.validate_cover_image",
        lambda path: Path(path),
    )
    return captured


def test_case1_offline_after_refresh(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh mod.io metadata, then offline archive succeeds."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/baldursgate3/m/better-inventory-ui1"
    mid, folder = _register_modio_mod(db, lib, url)
    _install_capture(monkeypatch)

    refresh = refresh_mod(
        mid,
        folder,
        platform=PLATFORM_MODIO,
        library_root=lib,
        source_url=url,
        db=db,
    )
    assert refresh.official_success is True

    new_folder = Path(db.get_mod_backup_row(mid)["last_known_path"])
    urls: list[str] = []

    def capture(url: str) -> str:
        urls.append(url)
        return _RENDERED_MODIO_HTML

    result = ModioOfflineProvider(capture_func=capture).update_offline_page(
        mid,
        managed_path=new_folder,
        library_root=lib,
    )
    assert result.status == OFFLINE_STATUS_ARCHIVED
    assert urls == ["https://mod.io/g/baldursgate3/m/better-inventory-ui1"]
    index = new_folder / INFO_DIR_NAME / "offline" / "index.html"
    assert index.is_file()
    assert "Better Inventory UI" in index.read_text(encoding="utf-8")


def test_case2_offline_after_rename(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh rename, offline archive writes to renamed folder."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/baldursgate3/m/better-inventory-ui1"
    mid, old = _register_modio_mod(db, lib, url)
    _install_capture(monkeypatch)

    refresh_mod(mid, old, platform=PLATFORM_MODIO, library_root=lib, source_url=url, db=db)
    new_folder = Path(db.get_mod_backup_row(mid)["last_known_path"])
    assert new_folder.name == "Better Inventory UI"
    assert not old.exists()

    urls: list[str] = []

    def capture(url: str) -> str:
        urls.append(url)
        return _RENDERED_MODIO_HTML

    result = ModioOfflineProvider(capture_func=capture).update_offline_page(
        mid,
        managed_path=new_folder,
        library_root=lib,
    )
    assert result.status == OFFLINE_STATUS_ARCHIVED
    assert (new_folder / INFO_DIR_NAME / "offline" / "index.html").is_file()


def test_case3_numeric_external_id_uses_source_url(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When external_id becomes numeric, offline still uses web source_url."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/baldursgate3/m/better-inventory-ui1"
    mid, folder = _register_modio_mod(db, lib, url)
    _install_capture(monkeypatch)

    refresh_mod(mid, folder, platform=PLATFORM_MODIO, library_root=lib, source_url=url, db=db)
    live_folder = Path(db.get_mod_backup_row(mid)["last_known_path"])
    db.update_mod_platform_info(
        mid,
        platform=PLATFORM_MODIO,
        external_id="4228735",
        source_url=url,
    )
    # Simulate bad API URL in DB — sidecar must win.
    db.update_mod_platform_info(
        mid,
        platform=PLATFORM_MODIO,
        source_url="https://g-6715.modapi.io/v1/games/6715/mods/4228735",
        external_id="4228735",
    )
    sidecar = live_folder / ".info" / "metadata.json"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    data["source_url"] = url
    data["url"] = url
    data["modio_name_id"] = "better-inventory-ui1"
    sidecar.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    resolved = resolve_modio_offline_page_url(mid, managed_path=live_folder, db=db)
    assert resolved == url
    assert "4228735" not in resolved or resolved.endswith("/better-inventory-ui1")

    urls: list[str] = []

    def capture(u: str) -> str:
        urls.append(u)
        return _RENDERED_MODIO_HTML

    ModioOfflineProvider(capture_func=capture).update_offline_page(
        mid,
        managed_path=live_folder,
        library_root=lib,
    )
    assert urls == [url]


def test_case4_stale_ui_path_heals(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UI holds old path after rename; offline archive auto-heals."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/baldursgate3/m/better-inventory-ui1"
    mid, old = _register_modio_mod(db, lib, url)
    _install_capture(monkeypatch)

    refresh_mod(mid, old, platform=PLATFORM_MODIO, library_root=lib, source_url=url, db=db)
    new_folder = Path(db.get_mod_backup_row(mid)["last_known_path"])

    urls: list[str] = []

    def capture(u: str) -> str:
        urls.append(u)
        return _RENDERED_MODIO_HTML

    result = ModioOfflineProvider(capture_func=capture).update_offline_page(
        mid,
        managed_path=old,
        library_root=lib,
    )
    assert result.status == OFFLINE_STATUS_ARCHIVED
    assert (new_folder / INFO_DIR_NAME / "offline" / "index.html").is_file()
    assert not (old / INFO_DIR_NAME / "offline" / "index.html").exists()


def test_worker_heals_stale_path(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OfflineArchiveWorker with stale path completes archive."""
    lib = tmp_path / "mod"
    url = "https://mod.io/g/baldursgate3/m/better-inventory-ui1"
    mid, old = _register_modio_mod(db, lib, url)
    _install_capture(monkeypatch)

    refresh_mod(mid, old, platform=PLATFORM_MODIO, library_root=lib, source_url=url, db=db)
    new_folder = Path(db.get_mod_backup_row(mid)["last_known_path"])

    finished: list[str] = []
    errors: list[str] = []

    class _Worker(OfflineArchiveWorker):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.archive_finished.connect(lambda p: finished.append(p))
            self.archive_failed.connect(lambda e: errors.append(e))

    monkeypatch.setattr(
        "services.offline.manager.ModioOfflineProvider",
        lambda *a, **k: ModioOfflineProvider(
            capture_func=lambda u: _RENDERED_MODIO_HTML
        ),
    )

    worker = _Worker(
        old,
        platform=PLATFORM_MODIO,
        published_file_id=mid,
        library_root=lib,
    )
    worker.run()
    assert not errors, errors
    assert finished
    assert Path(finished[0]).resolve().is_file()
    assert (new_folder / INFO_DIR_NAME / "offline" / "index.html").is_file()
