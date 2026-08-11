"""Regression: Steam refresh must surface Workshop title in Detail UI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.db_manager import DatabaseManager
from core.models import ModMetadata, is_unknown_mod_title
from core.steam_api import SteamWorkshopClient
from services.file_ops import INFO_DIR_NAME, ModFileManager
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
    mid = "3413520661"
    lib = tmp_path / "library"
    folder = lib / "Game" / f"Unknown_Mod_{mid}"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "metadata.json").write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": f"Unknown_Mod_{mid}",
                "display_name": f"Unknown_Mod_{mid}",
                "fetch_error": "timeout",
                "description": "old desc",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # Seed DB the same way the bug appears in production.
    db.upsert_mod(
        ModMetadata(
            published_file_id=mid,
            title=f"Unknown_Mod_{mid}",
            description="old desc",
        )
    )
    db.update_mod_user_metadata(mid, {"display_name": f"Unknown_Mod_{mid}"})

    # Raw column still holds the placeholder (UI must not prefer it).
    raw = db._conn.execute(  # noqa: SLF001
        "SELECT title, display_name FROM mods WHERE mod_id = ?",
        (int(mid),),
    ).fetchone()
    assert raw["display_name"] == f"Unknown_Mod_{mid}"

    fresh = ModMetadata(
        published_file_id=mid,
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

    result = refresh_steam_mod_metadata(
        mid, folder, library_root=lib, force=True, download_cover=False
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
    assert not dn or not is_unknown_mod_title(dn, published_file_id=mid)

    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.steam_name == "Test Workshop Mod"
    assert info.display_name == "Test Workshop Mod"

    raw_after = db._conn.execute(  # noqa: SLF001
        "SELECT title, display_name FROM mods WHERE mod_id = ?",
        (int(mid),),
    ).fetchone()
    assert raw_after["title"] == "Test Workshop Mod"
    assert not str(raw_after["display_name"] or "").strip() or not is_unknown_mod_title(
        str(raw_after["display_name"]), published_file_id=mid
    )

    # Detail panel must show the Workshop title, not Unknown Mod.
    panel = ModDetailPanel()
    panel.show_mod(result.managed_path)
    qapp.processEvents()
    assert "Test Workshop Mod" in (panel.view_title.text() or "")
    assert "Unknown" not in (panel.view_title.text() or "")


def test_display_info_ignores_stale_unknown_override_without_refresh(
    db: DatabaseManager,
) -> None:
    mid = "99"
    db.upsert_mod(
        ModMetadata(published_file_id=mid, title="Already Fixed Title")
    )
    db.update_mod_user_metadata(mid, {"display_name": f"Unknown_Mod_{mid}"})
    info = db.get_mod_display_info(mid)
    assert info is not None
    assert info.display_name == "Already Fixed Title"
    assert info.steam_name == "Already Fixed Title"
