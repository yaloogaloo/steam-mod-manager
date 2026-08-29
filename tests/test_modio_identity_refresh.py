"""Mod.io identity — internal mod_id must not be sent as platform mod id."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    NON_STEAM_MOD_ID_BASE,
    PLATFORM_MODIO,
    is_internal_mod_id,
    is_modio_api_mod_id,
)
from services.file_ops import INFO_DIR_NAME
from services.modio_api import ModioClient, ModioModDetails, map_mod_object
from services.modio_metadata_refresh import refresh_modio_mod_metadata


INTERNAL_MOD_ID = 9_000_000_000_003_410
REAL_MODIO_MOD_ID = 4_503_767
BG3_GAME_ID = 6715
MODIO_URL = "https://mod.io/g/baldursgate3/m/super-skip-ship-sss"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "modio_identity.db")
    yield manager
    DatabaseManager.reset_instance()


def _bg3_details(**overrides) -> ModioModDetails:
    data = {
        "id": REAL_MODIO_MOD_ID,
        "game_id": BG3_GAME_ID,
        "name": "Super Skip Ship, SSS",
        "name_id": "super-skip-ship-sss",
        "summary": "summary",
        "description": "description",
        "profile_url": MODIO_URL,
        "logo": {"original": "https://example.com/logo.png"},
    }
    data.update(overrides)
    return map_mod_object(data)


def test_internal_mod_id_not_used_as_modio_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Internal SQLite id must fall through to name_id lookup, not GET …/mods/{id}."""
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: None,
    )
    client = ModioClient(api_key="test-key-not-secret")
    get_mod_calls: list[tuple[int, int]] = []
    find_calls: list[tuple[int, str]] = []
    details = _bg3_details()

    def fake_get_mod(game_id: int, mod_id: int) -> ModioModDetails:
        get_mod_calls.append((game_id, mod_id))
        return details

    def fake_find_mod_by_name_id(game_id: int, name_id: str) -> ModioModDetails:
        find_calls.append((game_id, name_id))
        return details

    monkeypatch.setattr(client, "resolve_game_id", lambda slug: BG3_GAME_ID)
    monkeypatch.setattr(client, "get_mod", fake_get_mod)
    monkeypatch.setattr(client, "find_mod_by_name_id", fake_find_mod_by_name_id)

    out = client.resolve_mod(
        game_slug="baldursgate3",
        mod_name_id="super-skip-ship-sss",
        mod_id=INTERNAL_MOD_ID,
    )

    assert out.mod_id == REAL_MODIO_MOD_ID
    assert get_mod_calls == []
    assert find_calls == [(BG3_GAME_ID, "super-skip-ship-sss")]


def test_real_modio_numeric_id_still_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real Mod.io numeric ids still use GET …/mods/{id}."""
    monkeypatch.setattr(
        "services.archive.archive_proxies_dict",
        lambda proxy_url=None: None,
    )
    client = ModioClient(api_key="test-key-not-secret")
    get_mod_calls: list[tuple[int, int]] = []
    find_calls: list[tuple[int, str]] = []
    details = _bg3_details()

    def fake_get_mod(game_id: int, mod_id: int) -> ModioModDetails:
        get_mod_calls.append((game_id, mod_id))
        return details

    def fake_find_mod_by_name_id(game_id: int, name_id: str) -> ModioModDetails:
        find_calls.append((game_id, name_id))
        return details

    monkeypatch.setattr(client, "get_mod", fake_get_mod)
    monkeypatch.setattr(client, "find_mod_by_name_id", fake_find_mod_by_name_id)

    out = client.resolve_mod(game_id=BG3_GAME_ID, mod_id=REAL_MODIO_MOD_ID)

    assert out.mod_id == REAL_MODIO_MOD_ID
    assert get_mod_calls == [(BG3_GAME_ID, REAL_MODIO_MOD_ID)]
    assert find_calls == []


def test_mod_platform_internal_id_helpers() -> None:
    assert is_internal_mod_id(INTERNAL_MOD_ID)
    assert not is_internal_mod_id(REAL_MODIO_MOD_ID)
    assert is_modio_api_mod_id(REAL_MODIO_MOD_ID)
    assert not is_modio_api_mod_id(INTERNAL_MOD_ID)
    assert INTERNAL_MOD_ID >= NON_STEAM_MOD_ID_BASE


def test_ensure_mod_stub_non_steam_external_id_not_numeric(
    db: DatabaseManager,
) -> None:
    mid = db.allocate_mod_id()
    db._ensure_mod_stub(mid)  # noqa: SLF001
    info = db.get_mod_display_info(str(mid))
    assert info is not None
    assert info.external_id == f"stub:{mid}"
    assert not info.external_id.isdigit()


def test_update_mod_identity_clears_modio_external_pollution(
    db: DatabaseManager,
) -> None:
    mid = db.allocate_mod_id()
    db._ensure_mod_stub(mid)  # noqa: SLF001
    with db._lock:  # noqa: SLF001
        db._conn.execute(  # noqa: SLF001
            "UPDATE mods SET external_id = ? WHERE mod_id = ?",
            (str(mid), mid),
        )
        db._conn.commit()  # noqa: SLF001
    db.update_mod_identity_fields(
        mid,
        platform=PLATFORM_MODIO,
        source_url=MODIO_URL,
        external_id=str(mid),
    )
    info = db.get_mod_display_info(str(mid))
    assert info is not None
    assert info.external_id == ""


def test_refresh_polluted_external_id_uses_name_id(
    db: DatabaseManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for mod 9000000000003410-style polluted DB rows."""
    lib = tmp_path / "mod"
    folder = lib / "Baldur's Gate 3" / "super-skip-ship-sss"
    info_dir = folder / INFO_DIR_NAME
    info_dir.mkdir(parents=True)
    mid = str(db.allocate_mod_id())
    (info_dir / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "url": MODIO_URL,
                "source_type": "modio",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(folder / "payload.zip", "w") as zf:
        zf.writestr("mod.txt", "x")

    db._ensure_mod_stub(int(mid))  # noqa: SLF001
    db.update_mod_identity_fields(
        int(mid),
        platform=PLATFORM_MODIO,
        source_url=MODIO_URL,
        external_id=mid,
        last_known_path=str(folder.resolve()),
    )

    client = MagicMock()
    client.resolve_mod = MagicMock(return_value=_bg3_details())
    client.download_file = MagicMock(side_effect=lambda url, dest: Path(dest))
    client.close = MagicMock()

    monkeypatch.setattr(
        "services.importers.image_picker.validate_cover_image",
        lambda path: Path(path),
    )

    result = refresh_modio_mod_metadata(
        mid,
        folder,
        library_root=lib,
        client=client,
        download_cover=False,
        db=db,
    )

    assert result.success is True
    client.resolve_mod.assert_called_once()
    kwargs = client.resolve_mod.call_args.kwargs
    assert kwargs["mod_name_id"] == "super-skip-ship-sss"
    assert kwargs["mod_id"] == 0
