"""Mod.io metadata refresh — Anno works; BG3 needs game-scoped API host fallback."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import PLATFORM_MODIO, PLATFORM_STEAM
from services.file_ops import INFO_DIR_NAME
from services.mod_refresh import refresh_mod
from services.modio_api import (
    ModioAPIError,
    ModioClient,
    game_scoped_api_base,
    map_mod_object,
)
from services.modio_metadata_refresh import refresh_modio_mod_metadata


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "modio_host_fallback.db")
    yield manager
    DatabaseManager.reset_instance()


def _anno_mod_object(**overrides) -> dict:
    data = {
        "id": 424242,
        "game_id": 4169,
        "name": "Harbor Life",
        "name_id": "harborlife",
        "summary": "Short",
        "description": "Anno description",
        "profile_url": "https://mod.io/g/anno-1800/m/harborlife",
        "logo": {"original": "https://example.com/anno.png"},
        "submitted_by": {"username": "anno_author"},
    }
    data.update(overrides)
    return data


def _bg3_mod_object(**overrides) -> dict:
    data = {
        "id": 555001,
        "game_id": 6715,
        "name": "Polyamory Fixes",
        "name_id": "polyamoryfixes",
        "summary": "BG3 short",
        "description": "BG3 full description",
        "profile_url": "https://mod.io/g/baldursgate3/m/polyamoryfixes",
        "logo": {"original": "https://example.com/bg3.png"},
        "submitted_by": {"username": "bg3_author"},
    }
    data.update(overrides)
    return data


def test_game_scoped_api_base() -> None:
    assert game_scoped_api_base(6715) == "https://g-6715.modapi.io/v1"
    assert game_scoped_api_base("4169") == "https://g-4169.modapi.io/v1"


def test_find_mod_retries_game_scoped_host_on_global_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BG3-style: api.mod.io 404 → g-{id}.modapi.io succeeds (generic fallback)."""
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: None,
    )
    client = ModioClient(api_key="test-key-not-secret")
    calls: list[tuple[str, str]] = []

    def fake_get(path, *, params=None):
        calls.append((client.base_url, path))
        if client.base_url.rstrip("/").endswith("api.mod.io/v1"):
            raise ModioAPIError("Mod.io 未找到该资源", status_code=404)
        assert "g-6715.modapi.io" in client.base_url
        return {"data": [_bg3_mod_object()]}

    monkeypatch.setattr(client, "_get", fake_get)
    details = client.find_mod_by_name_id(6715, "polyamoryfixes")
    assert details.name == "Polyamory Fixes"
    assert details.game_id == 6715
    assert any("api.mod.io" in base for base, _ in calls)
    assert any("g-6715.modapi.io" in base for base, _ in calls)


def test_find_mod_anno_global_host_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anno-style: first host succeeds — never hits game-scoped host."""
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: None,
    )
    client = ModioClient(api_key="test-key-not-secret")
    calls: list[str] = []

    def fake_get(path, *, params=None):
        calls.append(client.base_url)
        return {"data": [_anno_mod_object()]}

    monkeypatch.setattr(client, "_get", fake_get)
    details = client.find_mod_by_name_id(4169, "harborlife")
    assert details.name == "Harbor Life"
    assert all("api.mod.io" in base for base in calls)
    assert not any("g-4169.modapi.io" in base for base in calls)


def test_refresh_modio_anno_metadata(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (folder / "mod.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    display = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="harborlife",
        source_url="https://mod.io/g/anno-1800/m/harborlife",
        title="Harbor",
        app_id=916440,
        game_name="Anno 1800",
    )
    details = map_mod_object(_anno_mod_object(name="Harbor Life"))

    class FakeClient:
        def resolve_mod(self, **kwargs):
            return details

        def download_file(self, url, dest):
            Path(dest).write_bytes(b"\x89PNG\r\n\x1a\n")
            return Path(dest)

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "services.modio_metadata_refresh.ModioClient",
        lambda *a, **k: FakeClient(),
    )
    result = refresh_modio_mod_metadata(
        display.mod_id,
        folder,
        library_root=lib,
        client=FakeClient(),  # type: ignore[arg-type]
        download_cover=True,
        db=db,
    )
    assert result.success
    assert not result.skipped
    assert result.title == "Harbor Life"
    disk = json.loads(
        (result.managed_path / INFO_DIR_NAME / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert disk["title"] == "Harbor Life"
    assert disk["description"] == "Anno description"
    assert disk["modio_game_id"] == 4169


def test_refresh_modio_bg3_metadata_via_scoped_host(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "mod"
    folder = lib / "Baldurs Gate 3" / "polyamoryfixes"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "title": "polyamoryfixes",
                "url": "https://mod.io/g/baldursgate3/m/polyamoryfixes#description",
                "source_type": "modio",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (folder / "pak.bin").write_bytes(b"bg3")

    display = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="polyamoryfixes",
        source_url="https://mod.io/g/baldursgate3/m/polyamoryfixes#description",
        title="polyamoryfixes",
        app_id=1086940,
        game_name="Baldur's Gate 3",
    )

    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: None,
    )
    monkeypatch.setattr("services.modio_api._cached_game_id", lambda slug: 6715)
    monkeypatch.setattr("services.modio_api._store_game_id", lambda slug, gid: None)

    client = ModioClient(api_key="test-key-not-secret")

    def fake_get(path, *, params=None):
        # Global catalog can resolve the game slug, but Mod Objects 404 there.
        if path == "games":
            return {"data": [{"id": 6715, "name_id": "baldursgate3"}]}
        if "api.mod.io" in client.base_url:
            raise ModioAPIError("Mod.io 未找到该资源", status_code=404)
        assert "g-6715.modapi.io" in client.base_url
        if path.endswith("/mods"):
            return {"data": [_bg3_mod_object()]}
        return _bg3_mod_object()

    monkeypatch.setattr(client, "_get", fake_get)

    def download_file(url, dest):
        Path(dest).write_bytes(b"\x89PNG\r\n\x1a\n")
        return Path(dest)

    monkeypatch.setattr(client, "download_file", download_file)

    result = refresh_modio_mod_metadata(
        display.mod_id,
        folder,
        library_root=lib,
        client=client,
        download_cover=True,
        db=db,
    )
    assert result.success
    assert result.title == "Polyamory Fixes"
    final = result.managed_path or folder
    disk = json.loads(
        (final / INFO_DIR_NAME / "metadata.json").read_text(encoding="utf-8")
    )
    assert disk["title"] == "Polyamory Fixes"
    assert disk["description"] == "BG3 full description"
    assert disk["modio_game_id"] == 6715
    assert disk["modio_mod_id"] == 555001
    assert disk.get("cover_path") or result.cover_path


def test_refresh_mod_non_modio_skips_modio_provider(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = tmp_path / "mod"
    folder = lib / "Palworld" / "LocalMod"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps({"title": "LocalMod", "source_type": "other"}, indent=2),
        encoding="utf-8",
    )
    (folder / "a.txt").write_text("x", encoding="utf-8")

    display = db.register_external_mod(
        platform="other",
        external_id="local-1",
        source_url="",
        title="LocalMod",
        app_id=1623730,
        game_name="Palworld",
    )

    spy = MagicMock()
    monkeypatch.setattr(
        "services.modio_metadata_refresh.refresh_modio_mod_metadata",
        spy,
    )
    steam_spy = MagicMock()
    monkeypatch.setattr(
        "services.metadata_refresh.refresh_steam_mod_metadata",
        steam_spy,
    )

    result = refresh_mod(
        display.mod_id,
        folder,
        platform="other",
        library_root=lib,
        db=db,
    )
    assert result.success
    assert result.official_attempted is False
    spy.assert_not_called()
    steam_spy.assert_not_called()


def test_rename_case_only_difference_is_noop(tmp_path: Path) -> None:
    """Windows-style: polyamoryfixes vs PolyamoryFixes must not abort refresh."""
    from services.modio_metadata_refresh import rename_modio_folder_for_title

    folder = tmp_path / "polyamoryfixes"
    folder.mkdir()
    (folder / "x.txt").write_text("x", encoding="utf-8")
    # Simulate resolve landing on the same directory with different casing request.
    new_path, renamed = rename_modio_folder_for_title(folder, "PolyamoryFixes")
    assert renamed is False
    assert new_path.resolve() == folder.resolve()
    assert folder.is_dir()


def test_refresh_modio_case_only_title_still_writes_metadata(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API returns differently-cased title; metadata must still be saved."""
    lib = tmp_path / "mod"
    folder = lib / "Baldurs Gate 3" / "polyamoryfixes"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "title": "polyamoryfixes",
                "url": "https://mod.io/g/baldursgate3/m/polyamoryfixes",
                "source_type": "modio",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (folder / "pak.bin").write_bytes(b"bg3")

    display = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="polyamoryfixes",
        source_url="https://mod.io/g/baldursgate3/m/polyamoryfixes",
        title="polyamoryfixes",
        app_id=1086940,
        game_name="Baldur's Gate 3",
    )
    details = map_mod_object(
        _bg3_mod_object(name="PolyamoryFixes", name_id="polyamoryfixes")
    )

    class FakeClient:
        def resolve_mod(self, **kwargs):
            # Must use name_id from URL, never workspace / internal id.
            assert kwargs.get("mod_name_id") == "polyamoryfixes"
            assert int(kwargs.get("mod_id") or 0) == 0
            return details

        def download_file(self, url, dest):
            Path(dest).write_bytes(b"\x89PNG\r\n\x1a\n")
            return Path(dest)

        def close(self) -> None:
            return None

    result = refresh_modio_mod_metadata(
        display.mod_id,
        folder,
        library_root=lib,
        client=FakeClient(),  # type: ignore[arg-type]
        download_cover=True,
        db=db,
    )
    assert result.success
    assert result.title == "PolyamoryFixes"
    disk = json.loads(
        (folder / INFO_DIR_NAME / "metadata.json").read_text(encoding="utf-8")
    )
    assert disk["title"] == "PolyamoryFixes"
    assert disk["description"] == "BG3 full description"
    assert disk["modio_mod_id"] == 555001


def test_official_failure_surfaces_as_ui_failure(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.metadata_refresh import MetadataRefreshResult
    from services.mod_refresh import refresh_mod

    lib = tmp_path / "mod"
    folder = lib / "Baldurs Gate 3" / "polyamoryfixes"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "title": "polyamoryfixes",
                "url": "https://mod.io/g/baldursgate3/m/polyamoryfixes",
                "source_type": "modio",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (folder / "a.txt").write_text("x", encoding="utf-8")
    display = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="polyamoryfixes",
        source_url="https://mod.io/g/baldursgate3/m/polyamoryfixes",
        title="polyamoryfixes",
        app_id=1086940,
        game_name="Baldur's Gate 3",
    )
    monkeypatch.setattr(
        "services.modio_metadata_refresh.refresh_modio_mod_metadata",
        lambda *a, **k: MetadataRefreshResult(
            mod_id=str(display.mod_id),
            success=False,
            managed_path=folder,
            old_path=folder,
            error="目录已存在，无法重命名",
        ),
    )
    result = refresh_mod(
        display.mod_id,
        folder,
        platform=PLATFORM_MODIO,
        library_root=lib,
        db=db,
    )
    compat = result.to_metadata_refresh_result()
    assert result.official_attempted
    assert not result.official_success
    assert compat.success is False
    assert "目录已存在" in (compat.error or "")

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
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (folder / "mod.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    display = db.register_external_mod(
        platform=PLATFORM_MODIO,
        external_id="harborlife",
        source_url="https://mod.io/g/anno-1800/m/harborlife",
        title="Harbor",
        app_id=916440,
        game_name="Anno 1800",
    )

    from services.metadata_refresh import MetadataRefreshResult

    monkeypatch.setattr(
        "services.modio_metadata_refresh.refresh_modio_mod_metadata",
        lambda *a, **k: MetadataRefreshResult(
            mod_id=str(display.mod_id),
            success=True,
            skipped=False,
            managed_path=folder,
            old_path=folder,
            title="Harbor Life",
            message="ok",
        ),
    )
    steam_spy = MagicMock()
    monkeypatch.setattr(
        "services.metadata_refresh.refresh_steam_mod_metadata",
        steam_spy,
    )

    result = refresh_mod(
        display.mod_id,
        folder,
        platform=PLATFORM_MODIO,
        library_root=lib,
        source_url="https://mod.io/g/anno-1800/m/harborlife",
        db=db,
    )
    assert result.official_attempted is True
    assert result.official_success is True
    steam_spy.assert_not_called()
    assert PLATFORM_STEAM != PLATFORM_MODIO
