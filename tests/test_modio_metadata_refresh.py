"""Mod.io metadata refresh (API mapping, rename, routing)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_MODIO, PLATFORM_STEAM
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME
from services.modio_api import (
    ModioClient,
    ModioModDetails,
    map_mod_object,
    parse_modio_url,
)
from services.modio_metadata_refresh import (
    refresh_modio_mod_metadata,
    rename_modio_folder_for_title,
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "modio_refresh.db")
    yield manager
    DatabaseManager.reset_instance()


def _sample_mod_object(**overrides) -> dict:
    data = {
        "id": 424242,
        "game_id": 1111,
        "name": "Harbor Life",
        "name_id": "harborlife",
        "summary": "Short summary",
        "description": "Full description from API",
        "profile_url": "https://mod.io/g/anno-1800/m/harborlife",
        "logo": {
            "original": "https://example.com/logo.png",
            "thumb_640x360": "https://example.com/logo_640.png",
        },
        "submitted_by": {"username": "harbor_author"},
    }
    data.update(overrides)
    return data


def test_parse_modio_url_harborlife() -> None:
    parts = parse_modio_url(
        "https://mod.io/g/anno-1800/m/harborlife#description"
    )
    assert parts is not None
    assert parts.game_slug == "anno-1800"
    assert parts.mod_name_id == "harborlife"
    assert parts.canonical_url == "https://mod.io/g/anno-1800/m/harborlife"
    assert "#" not in parts.canonical_url


def test_parse_modio_url_rejects_non_modio() -> None:
    assert parse_modio_url("https://steamcommunity.com/sharedfiles/filedetails/?id=1") is None
    assert parse_modio_url("") is None


def test_map_mod_object_fields() -> None:
    details = map_mod_object(_sample_mod_object())
    assert details.mod_id == 424242
    assert details.game_id == 1111
    assert details.name == "Harbor Life"
    assert details.name_id == "harborlife"
    assert details.description == "Full description from API"
    assert details.author == "harbor_author"
    assert details.logo_url == "https://example.com/logo.png"


def test_refresh_updates_metadata_json(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "mod"
    folder = lib / "Anno 1800" / "OldName"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "title": "OldName",
                "url": "https://mod.io/g/anno-1800/m/harborlife#description",
                "source_type": "modio",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (info / "offline").mkdir()
    (info / "offline" / "index.html").write_text("<html>offline</html>", encoding="utf-8")

    display = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="harborlife",
        source_url="https://mod.io/g/anno-1800/m/harborlife",
        title="OldName",
        app_id=916440,
        game_name="Anno 1800",
    )

    details = map_mod_object(_sample_mod_object(name="NewName"))

    class FakeClient:
        def resolve_mod(self, **kwargs):
            return details

        def download_file(self, url, dest):
            Path(dest).write_bytes(b"\x89PNG\r\n\x1a\n")
            return Path(dest)

        def close(self):
            return None

    monkeypatch.setattr(
        "services.importers.image_picker.validate_cover_image",
        lambda path: Path(path),
    )

    result = refresh_modio_mod_metadata(
        display.mod_id,
        folder,
        library_root=lib,
        client=FakeClient(),  # type: ignore[arg-type]
        download_cover=True,
    )
    assert result.success
    assert result.title == "NewName"
    assert result.renamed is True
    assert result.managed_path is not None
    assert result.managed_path.name == "NewName"
    assert not folder.exists()
    assert result.managed_path.is_dir()

    disk = json.loads(
        (result.managed_path / INFO_DIR_NAME / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert disk["title"] == "NewName"
    assert disk["display_name"] == "NewName"
    assert disk["modio_mod_id"] == 424242
    assert disk["author"] == "harbor_author"
    assert "Full description" in disk["description"]
    assert disk["url"] == "https://mod.io/g/anno-1800/m/harborlife"
    assert disk["source_url"] == "https://mod.io/g/anno-1800/m/harborlife"
    assert disk["preview_url"].startswith("https://example.com/")
    assert disk.get("cover_path")
    assert (result.managed_path / INFO_DIR_NAME / "cover.png").is_file() or (
        result.managed_path / INFO_DIR_NAME / "cover.jpg"
    ).is_file()
    # Rename must rewrite offline path away from the old folder name.
    offline = str(disk.get("offline_page_path") or disk.get("offline_page") or "")
    assert "OldName" not in offline
    assert (result.managed_path / INFO_DIR_NAME / "offline" / "index.html").is_file()

    info_db = db.get_mod_display_info(display.mod_id)
    assert info_db is not None
    assert info_db.steam_name == "NewName" or info_db.display_name == "NewName"
    assert "Full description" in (info_db.steam_description or "")
    assert (info_db.preview_url or "").startswith("https://example.com/")


def test_rename_conflict_does_not_overwrite(tmp_path: Path) -> None:
    parent = tmp_path / "Anno 1800"
    old = parent / "OldName"
    existing = parent / "NewName"
    old.mkdir(parents=True)
    existing.mkdir()
    (old / "keep.txt").write_text("old", encoding="utf-8")
    (existing / "other.txt").write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError):
        rename_modio_folder_for_title(old, "NewName")

    assert old.is_dir()
    assert (old / "keep.txt").read_text(encoding="utf-8") == "old"
    assert existing.is_dir()
    assert (existing / "other.txt").read_text(encoding="utf-8") == "existing"


def test_modio_rename_succeeds_after_cover_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: prior CoverLoader activity must not block Mod.io rename."""
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QApplication
    from services.cover_loader import CoverLoaderManager

    app = QApplication.instance() or QApplication([])
    CoverLoaderManager.reset_instance()

    parent = tmp_path / "Anno 1800"
    old = parent / "更大的油泵半径"
    old.mkdir(parents=True)
    info = old / INFO_DIR_NAME
    info.mkdir()
    cover = info / "cover.png"
    pix = QPixmap(32, 32)
    pix.fill()
    pix.save(str(cover), "PNG")

    mgr = CoverLoaderManager.instance()
    mgr.request("pre-rename", old, cover_ref=".info/cover.png", width=40, height=30)
    # Let the load start; cancel+wait happens inside rename.
    deadline = time.time() + 2.0
    while time.time() < deadline and mgr.inflight_count(old) > 0:
        app.processEvents()
        time.sleep(0.01)
    # Even if already finished, cancel path must still allow rename.
    new_path, renamed = rename_modio_folder_for_title(
        old, "Bigger Oil Pump Radius [Spice It Up]"
    )
    assert renamed is True
    assert new_path.name == "Bigger Oil Pump Radius [Spice It Up]"
    assert new_path.is_dir()
    assert not old.exists()
    assert (new_path / INFO_DIR_NAME / "cover.png").is_file()
    CoverLoaderManager.reset_instance()


def test_modio_rename_succeeds_after_metadata_read(
    tmp_path: Path,
) -> None:
    """Rename must succeed after metadata.json was read/written (handle closed)."""
    parent = tmp_path / "Anno 1800"
    old = parent / "更好的镜头缩放"
    old.mkdir(parents=True)
    info = old / INFO_DIR_NAME
    info.mkdir()
    meta = info / "metadata.json"
    meta.write_text(
        json.dumps({"title": "更好的镜头缩放", "source_type": "modio"}),
        encoding="utf-8",
    )
    # Simulate refresh path: read then write then rename.
    from services.file_ops import read_info_metadata_dict

    data = read_info_metadata_dict(old) or {}
    data["title"] = "Zoom Out In Further (Serp)"
    meta.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    new_path, renamed = rename_modio_folder_for_title(
        old, "Zoom Out In Further (Serp)"
    )
    assert renamed is True
    assert new_path.name == "Zoom Out In Further (Serp)"
    assert new_path.is_dir()
    assert not old.exists()


def test_refresh_conflict_returns_error_without_overwrite(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "mod"
    old = lib / "Anno 1800" / "OldName"
    clash = lib / "Anno 1800" / "NewName"
    old.mkdir(parents=True)
    clash.mkdir(parents=True)
    (old / INFO_DIR_NAME).mkdir()
    (old / INFO_DIR_NAME / "metadata.json").write_text(
        json.dumps(
            {
                "title": "OldName",
                "url": "https://mod.io/g/anno-1800/m/harborlife",
                "source_type": "modio",
            }
        ),
        encoding="utf-8",
    )
    display = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="harborlife",
        source_url="https://mod.io/g/anno-1800/m/harborlife",
        title="OldName",
        app_id=916440,
        game_name="Anno 1800",
    )
    details = map_mod_object(_sample_mod_object(name="NewName"))

    class FakeClient:
        def resolve_mod(self, **kwargs):
            return details

        def close(self):
            return None

    result = refresh_modio_mod_metadata(
        display.mod_id,
        old,
        library_root=lib,
        client=FakeClient(),  # type: ignore[arg-type]
        download_cover=False,
    )
    assert result.success is False
    assert "冲突" in result.error or "已存在" in result.error
    assert old.is_dir()
    assert clash.is_dir()


def test_cover_download_uses_logo_url(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "mod"
    folder = lib / "Anno 1800" / "CoverMod"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "title": "CoverMod",
                "url": "https://mod.io/g/anno-1800/m/harborlife",
                "source_type": "modio",
            }
        ),
        encoding="utf-8",
    )
    display = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="harborlife-cover",
        source_url="https://mod.io/g/anno-1800/m/harborlife",
        title="CoverMod",
        app_id=916440,
        game_name="Anno 1800",
    )
    details = map_mod_object(_sample_mod_object(name="CoverMod"))
    downloaded: list[str] = []

    class FakeClient:
        def resolve_mod(self, **kwargs):
            return details

        def download_file(self, url, dest):
            downloaded.append(url)
            Path(dest).write_bytes(b"\x89PNG\r\n\x1a\nxxxx")
            return Path(dest)

        def close(self):
            return None

    monkeypatch.setattr(
        "services.importers.image_picker.validate_cover_image",
        lambda path: Path(path),
    )

    result = refresh_modio_mod_metadata(
        display.mod_id,
        folder,
        library_root=lib,
        client=FakeClient(),  # type: ignore[arg-type]
        download_cover=True,
    )
    assert result.success
    assert downloaded == ["https://example.com/logo.png"]
    covers = list((result.managed_path / INFO_DIR_NAME).glob("cover.*"))
    assert covers


def test_ui_routes_modio_to_modio_worker(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from ui.mod_detail_panel import ModDetailPanel

    app = QApplication.instance() or QApplication([])
    lib = tmp_path / "mod"
    folder = lib / "Anno 1800" / "Harbor"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "title": "Harbor",
                "url": "https://mod.io/g/anno-1800/m/harborlife",
                "source_type": "modio",
                "published_file_id": "9000000000000999",
            }
        ),
        encoding="utf-8",
    )
    display = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="harborlife",
        source_url="https://mod.io/g/anno-1800/m/harborlife",
        title="Harbor",
        app_id=916440,
        game_name="Anno 1800",
        mod_id=9000000000000999,
    )
    # Align folder metadata id with registered row.
    data = json.loads((info / "metadata.json").read_text(encoding="utf-8"))
    data["published_file_id"] = str(display.mod_id)
    (info / "metadata.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    panel = ModDetailPanel()
    panel.show_mod(folder)
    app.processEvents()
    assert panel._current_platform == PLATFORM_MODIO

    started = {"n": 0}

    class FakeWorker:
        def __init__(self, *a, **k):
            self.refresh_started = type("S", (), {"connect": lambda *x: None})()
            self.refresh_finished = type("S", (), {"connect": lambda *x: None})()
            self.refresh_failed = type("S", (), {"connect": lambda *x: None})()
            self.finished = type("S", (), {"connect": lambda *x: None})()

        def start(self):
            started["n"] += 1

        def isRunning(self):
            return False

    monkeypatch.setattr(
        "ui.metadata_refresh_thread.ModRefreshWorker",
        FakeWorker,
    )
    steam_started = {"n": 0}

    class FakeSteam:
        def __init__(self, *a, **k):
            raise AssertionError("Unexpected refresh worker")

    monkeypatch.setattr(
        "ui.metadata_refresh_thread.MetadataRefreshWorker",
        FakeSteam,
    )

    panel._on_refresh_mod()
    assert started["n"] == 1


def test_steam_routing_unchanged(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from ui.mod_detail_panel import ModDetailPanel

    app = QApplication.instance() or QApplication([])
    lib = tmp_path / "mod"
    mid = "3413520661"
    folder = lib / "Game" / f"Unknown_Mod_{mid}"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": f"Unknown_Mod_{mid}",
                "fetch_error": "timeout",
            }
        ),
        encoding="utf-8",
    )
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title=f"Unknown_Mod_{mid}")
    )

    panel = ModDetailPanel()
    panel.show_mod(folder)
    app.processEvents()

    steam_n = {"n": 0}

    class FakeSteam:
        def __init__(self, *a, **k):
            self.refresh_started = type("S", (), {"connect": lambda *x: None})()
            self.refresh_finished = type("S", (), {"connect": lambda *x: None})()
            self.refresh_failed = type("S", (), {"connect": lambda *x: None})()
            self.finished = type("S", (), {"connect": lambda *x: None})()

        def start(self):
            steam_n["n"] += 1

        def isRunning(self):
            return False

    class FakeModio:
        def __init__(self, *a, **k):
            raise AssertionError("Mod.io worker must not run for Steam")

    monkeypatch.setattr(
        "ui.metadata_refresh_thread.ModRefreshWorker", FakeSteam
    )
    monkeypatch.setattr(
        "ui.metadata_refresh_thread.ModioMetadataRefreshWorker", FakeModio
    )

    panel._on_refresh_mod()
    assert steam_n["n"] == 1


def test_refresh_after_rename_fallback_updates_names_and_db(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: content-move fallback must still update Mod 名称 + DB."""
    lib = tmp_path / "mod"
    folder = lib / "Anno 1800" / "更好的镜头缩放"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "title": "更好的镜头缩放",
                "display_name": "更好的镜头缩放",
                "url": "https://mod.io/g/anno-1800/m/zoom-outin-further-serp",
                "source_type": "modio",
                "offline_page_path": str(info / "offline" / "index.html"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (info / "offline").mkdir()
    (info / "offline" / "index.html").write_text("<html/>", encoding="utf-8")
    (folder / "payload.txt").write_text("x", encoding="utf-8")

    display = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="zoom-outin-further-serp",
        source_url="https://mod.io/g/anno-1800/m/zoom-outin-further-serp",
        title="更好的镜头缩放",
        app_id=916440,
        game_name="Anno 1800",
    )
    db.update_mod_user_metadata(
        display.mod_id, {"display_name": "更好的镜头缩放"}
    )

    details = map_mod_object(
        _sample_mod_object(
            id=919191,
            name="Zoom Out In Further (Serp)",
            name_id="zoom-outin-further-serp",
            profile_url="https://mod.io/g/anno-1800/m/zoom-outin-further-serp",
        )
    )

    class FakeClient:
        def resolve_mod(self, **kwargs):
            return details

        def download_file(self, url, dest):
            Path(dest).write_bytes(b"\x89PNG\r\n\x1a\n")
            return Path(dest)

        def close(self):
            return None

    monkeypatch.setattr(
        "services.importers.image_picker.validate_cover_image",
        lambda path: Path(path),
    )
    monkeypatch.setattr(
        "services.modio_metadata_refresh.safe_directory_rename",
        MagicMock(
            side_effect=PermissionError(5, "Access is denied", str(folder))
        ),
    )
    monkeypatch.setattr(
        "services.modio_metadata_refresh.prepare_managed_folder_for_rename",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "services.modio_metadata_refresh.collect_directory_rename_diagnostics",
        lambda src, dst: {
            "source": str(src),
            "target": str(dst),
            "target_exists": False,
            "source_exists": True,
            "source_writable": True,
            "has_info": True,
            "metadata_mtime": "",
            "cover_inflight": 0,
            "cover_active_tokens": 0,
            "metadata_file": "closed",
            "process_cwd": str(tmp_path),
            "cwd_under_source": False,
        },
    )

    result = refresh_modio_mod_metadata(
        display.mod_id,
        folder,
        library_root=lib,
        client=FakeClient(),  # type: ignore[arg-type]
        download_cover=False,
    )
    assert result.success
    assert result.renamed is True
    assert result.managed_path is not None
    assert result.managed_path.name == "Zoom Out In Further (Serp)"
    assert not folder.exists()

    disk = json.loads(
        (result.managed_path / INFO_DIR_NAME / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert disk["title"] == "Zoom Out In Further (Serp)"
    assert disk["display_name"] == "更好的镜头缩放"

    info_db = db.get_mod_display_info(display.mod_id)
    assert info_db is not None
    assert info_db.steam_name == "Zoom Out In Further (Serp)"
    assert info_db.user_display_name == "更好的镜头缩放"
    assert info_db.display_name == "更好的镜头缩放"


def test_client_resolve_uses_name_id_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict]] = []

    # Do not pollute real QSettings game_id cache with fixture id 1111.
    monkeypatch.setattr("services.modio_api._cached_game_id", lambda slug: None)
    monkeypatch.setattr("services.modio_api._store_game_id", lambda slug, gid: None)
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: None,
    )

    client = ModioClient(api_key="test-key-not-secret")

    def fake_get(path, *, params=None):
        calls.append((path, dict(params or {})))
        if path == "games":
            return {"data": [{"id": 1111, "name_id": "anno-1800"}]}
        if path.endswith("/mods"):
            return {"data": [_sample_mod_object()]}
        return _sample_mod_object()

    monkeypatch.setattr(client, "_get", fake_get)
    details = client.resolve_mod(game_slug="anno-1800", mod_name_id="harborlife")
    assert details.name == "Harbor Life"
    assert any(p.endswith("/mods") and q.get("name_id") == "harborlife" for p, q in calls)
