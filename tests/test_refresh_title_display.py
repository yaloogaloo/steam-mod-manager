"""Regression: Steam refresh must surface Workshop title in Detail UI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata, is_unknown_mod_title
from core.steam_api import SteamWorkshopClient
from services.file_ops import INFO_DIR_NAME
from services.metadata_refresh import refresh_steam_mod_metadata
from ui.mod_detail_panel import ModDetailPanel


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "title_fix.db")
    yield manager
    DatabaseManager.reset_instance()


def test_placeholder_title_detection() -> None:
    assert is_unknown_mod_title("Unknown Mod")
    assert is_unknown_mod_title("Unknown_Mod_1", published_file_id="1")
    assert is_unknown_mod_title("Unknown Mod 1", published_file_id="1")
    assert not is_unknown_mod_title("Test Workshop Mod")


def test_refresh_replaces_unknown_display_name_with_steam_title(
    qapp: QApplication,
    tmp_path: Path,
    db: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Live bug: mods.title updates to Workshop name, but mods.display_name stays
    Unknown_Mod_* and Detail header prefers display_name.
    """
    workshop = "3413520661"
    lib = tmp_path / "library"
    folder = lib / "Game" / f"Unknown_Mod_{workshop}"
    folder.mkdir(parents=True)

    created = create_steam_test_mod(
        db, external_id=workshop, title=f"Unknown_Mod_{workshop}"
    )
    pk = prove_managed_folder(
        db,
        folder,
        handle=created.mod_id,
        title=f"Unknown_Mod_{workshop}",
        extra={
            "display_name": f"Unknown_Mod_{workshop}",
            "fetch_error": "timeout",
            "description": "old desc",
        },
    )

    db.update_mod_user_metadata(pk, {"display_name": f"Unknown_Mod_{workshop}"})

    # Raw column still holds the placeholder (UI must not prefer it).
    raw = db._conn.execute(  # noqa: SLF001
        "SELECT title, display_name FROM mods WHERE mod_id = ?",
        (int(pk),),
    ).fetchone()
    assert raw["display_name"] == f"Unknown_Mod_{workshop}"

    fresh = ModMetadata(
        published_file_id=workshop,
        title="Test Workshop Mod",
        description="Fresh workshop description",
        preview_url="https://example.com/preview.jpg",
        creator_steam_id="76561198000000000",
        app_id=0,
    )
    monkeypatch.setattr(
        SteamWorkshopClient,
        "refresh_details",
        lambda self, ids, **k: [fresh],
    )
    monkeypatch.setattr(
        SteamWorkshopClient,
        "fetch_and_save_cover",
        lambda *a, **k: None,
    )
    # Isolate catalog write from path rename / Path Lifecycle (tested elsewhere).
    monkeypatch.setattr(
        "services.metadata_refresh.rename_managed_folder_for_title",
        lambda folder, meta, **k: (Path(folder), False),
    )

    result = refresh_steam_mod_metadata(
        pk, folder, library_root=lib, force=True, download_cover=False
    )
    assert result.success is True
    assert result.title == "Test Workshop Mod"
    assert result.managed_path is not None

    # Stored metadata contains the Steam title.
    disk = json.loads(
        (result.managed_path / INFO_DIR_NAME / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert disk["title"] == "Test Workshop Mod"
    dn = str(disk.get("display_name") or "").strip()
    assert not dn or not is_unknown_mod_title(dn, published_file_id=workshop)

    info = db.get_mod_display_info(pk)
    assert info is not None
    assert info.steam_name == "Test Workshop Mod"
    assert info.display_name == "Test Workshop Mod"

    raw_after = db._conn.execute(  # noqa: SLF001
        "SELECT title, display_name FROM mods WHERE mod_id = ?",
        (int(pk),),
    ).fetchone()
    assert raw_after["title"] == "Test Workshop Mod"
    assert not str(raw_after["display_name"] or "").strip() or not is_unknown_mod_title(
        str(raw_after["display_name"]), published_file_id=workshop
    )

    # Detail panel must show the Workshop title, not Unknown Mod.
    panel = ModDetailPanel()
    panel.show_mod(result.managed_path, mod_id=pk)
    qapp.processEvents()
    title_text = (panel.view_title.text() or "").replace("\u200b", "")
    assert "Test Workshop Mod" in title_text
    assert "Unknown" not in title_text


def test_display_info_ignores_stale_unknown_override_without_refresh(
    db: DatabaseManager,
) -> None:
    workshop = "99"
    created = create_steam_test_mod(
        db, external_id=workshop, title="Already Fixed Title"
    )
    pk = str(created.mod_id)

    db.update_mod_user_metadata(pk, {"display_name": f"Unknown_Mod_{workshop}"})
    info = db.get_mod_display_info(pk)
    assert info is not None
    assert info.display_name == "Already Fixed Title"
    assert info.steam_name == "Already Fixed Title"
