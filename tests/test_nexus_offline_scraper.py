"""Nexus offline HTML metadata scraper — unit and integration tests.

Test coverage:
  Section 1 — parse_nexus_offline_html (pure parser)
  Section 2 — Gallery image selection (must prefer Mod images over UI noise)
  Section 3 — apply_nexus_offline_candidates (fill-missing merge)
  Section 4 — attach_nexus_offline_page triggers scraper (integration)
  Section 5 — Refresh isolation: refresh_mod / reconcile MUST NOT call scraper
  Section 6 — Platform isolation: Steam/GitHub must NOT trigger Nexus scraper
  Section 7 — Idempotency: re-importing same HTML does not overwrite metadata
  Section 8 — Scraper failure is non-blocking
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.db_manager import DatabaseManager
from core.mod_platform import (
    OFFLINE_STATUS_ARCHIVED,
    PLATFORM_NEXUS,
    PLATFORM_STEAM,
    PROVIDER_NEXUS_MANUAL_IMPORT,
)
from services.file_ops import INFO_DIR_NAME
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from services.offline.manager import attach_nexus_offline_page
from services.offline.nexus_html_parser import (
    NexusOfflineCandidates,
    apply_nexus_offline_candidates,
    parse_nexus_offline_html,
)

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")

# ---------------------------------------------------------------------------
# Shared DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "scraper_test.db")
    yield manager
    DatabaseManager.reset_instance()


# ---------------------------------------------------------------------------
# HTML builder helpers
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{page_title}</title>
  <meta property="og:title" content="{og_title}"/>
  <meta property="og:url" content="{og_url}"/>
  <meta name="description" content="{description}"/>
</head>
<body class="site-nexusmods-b modpage">
  <section data-game-id="1303" data-mod-id="{mod_id}"></section>
  <div class="clearfix modimages" id="sidebargallery">
    <ul class="thumbgallery gallery clearfix">
      {gallery_items}
    </ul>
  </div>
</body>
</html>
"""


def _write_offline_html(
    managed_path: Path,
    *,
    og_title: str = "Teleport NPC Location",
    og_url: str = "https://www.nexusmods.com/stardewvalley/mods/10455",
    page_title: str = "Teleport NPC Location at Stardew Valley Nexus - Mods and community",
    description: str = "Teleports NPCs.",
    mod_id: str = "10455",
    gallery_items: str = "",
    create_asset: bool = True,
) -> Path:
    """Write .info/offline/index.html + optional gallery asset; return index path."""
    offline_dir = managed_path / INFO_DIR_NAME / "offline"
    offline_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = offline_dir / "assets"
    assets_dir.mkdir(exist_ok=True)

    if not gallery_items:
        if create_asset:
            (assets_dir / "mod_cover.png").write_bytes(b"\x89PNG\r\n")
            gallery_items = '<li><img src="./assets/mod_cover.png"/></li>'

    html = _HTML_TEMPLATE.format(
        og_title=og_title,
        og_url=og_url,
        page_title=page_title,
        description=description,
        mod_id=mod_id,
        gallery_items=gallery_items,
    )
    index = offline_dir / "index.html"
    index.write_text(html, encoding="utf-8")
    return index


def _register_nexus_mod(
    tmp_path: Path,
    db: DatabaseManager,
    *,
    title: str = "Unknown_Mod_Placeholder",
    nexus_id: str = "0",
    source_url: str = "",
) -> tuple[str, Path]:
    """Import a minimal Nexus mod; return (mod_id_str, managed_path)."""
    src = tmp_path / f"src_{nexus_id}"
    src.mkdir(exist_ok=True)
    (src / "mod.pak").write_bytes(b"pak")
    lib = tmp_path / "lib"

    result = NexusImporter(db=db).import_mod(
        source_folder=src,
        title=title,
        nexus_url=source_url or f"https://www.nexusmods.com/x/mods/{nexus_id}",
        nexus_id=nexus_id,
        library_root=lib,
        context=PALWORLD,
    )
    assert result.success, result.error
    mid = str(result.mod_id)
    if source_url == "":
        db.update_mod_identity_fields(mid, source_url="")
    return mid, Path(result.managed_path)


# ===========================================================================
# Section 1 — parse_nexus_offline_html: pure parsing
# ===========================================================================


class TestParseNexusOfflineHtml:
    def test_extracts_og_title(self, tmp_path: Path) -> None:
        index = _write_offline_html(tmp_path, og_title="Stardrop")
        result = parse_nexus_offline_html(index)
        assert result.title == "Stardrop"
        assert result.confidence["title"] == "high"

    def test_extracts_og_url_clean(self, tmp_path: Path) -> None:
        index = _write_offline_html(
            tmp_path, og_url="https://www.nexusmods.com/stardewvalley/mods/10455"
        )
        result = parse_nexus_offline_html(index)
        assert result.source_url == "https://www.nexusmods.com/stardewvalley/mods/10455"
        assert result.confidence["source_url"] == "high"

    def test_og_url_query_stripped(self, tmp_path: Path) -> None:
        index = _write_offline_html(
            tmp_path,
            og_url="https://www.nexusmods.com/stardewvalley/mods/10455?tab=description",
        )
        result = parse_nexus_offline_html(index)
        assert "?" not in result.source_url
        assert result.source_url == "https://www.nexusmods.com/stardewvalley/mods/10455"

    def test_extracts_mod_id_data_attribute(self, tmp_path: Path) -> None:
        index = _write_offline_html(tmp_path, mod_id="10455")
        result = parse_nexus_offline_html(index)
        assert result.external_id == "10455"
        assert result.confidence["external_id"] == "high"

    def test_game_id_not_confused_with_mod_id(self, tmp_path: Path) -> None:
        offline_dir = tmp_path / INFO_DIR_NAME / "offline"
        offline_dir.mkdir(parents=True, exist_ok=True)
        html = textwrap.dedent("""\
            <!DOCTYPE html><html><head>
            <meta property="og:url" content="https://www.nexusmods.com/sdv/mods/10455"/>
            </head><body>
            <section data-game-id="1303" data-mod-id="10455"></section>
            </body></html>
        """)
        index = offline_dir / "index.html"
        index.write_text(html, encoding="utf-8")
        result = parse_nexus_offline_html(index)
        assert result.external_id == "10455"
        assert result.external_id != "1303"

    def test_mod_id_fallback_from_url(self, tmp_path: Path) -> None:
        offline_dir = tmp_path / INFO_DIR_NAME / "offline"
        offline_dir.mkdir(parents=True, exist_ok=True)
        html = textwrap.dedent("""\
            <!DOCTYPE html><html><head>
            <meta property="og:title" content="My Mod"/>
            <meta property="og:url" content="https://www.nexusmods.com/bg3/mods/9999"/>
            </head><body></body></html>
        """)
        (offline_dir / "index.html").write_text(html, encoding="utf-8")
        result = parse_nexus_offline_html(offline_dir / "index.html")
        assert result.external_id == "9999"

    def test_extracts_gallery_cover_local(self, tmp_path: Path) -> None:
        index = _write_offline_html(tmp_path)
        result = parse_nexus_offline_html(index)
        assert result.cover_asset_path is not None
        assert result.cover_asset_path.is_file()
        assert result.confidence["cover"] in ("high", "medium")

    def test_gallery_remote_url_skipped(self, tmp_path: Path) -> None:
        items = '<li><img src="https://cdn.nexusmods.com/images/10455.jpg"/></li>'
        index = _write_offline_html(tmp_path, gallery_items=items, create_asset=False)
        result = parse_nexus_offline_html(index)
        assert result.cover_asset_path is None

    def test_extracts_description(self, tmp_path: Path) -> None:
        index = _write_offline_html(tmp_path, description="A cool mod.")
        result = parse_nexus_offline_html(index)
        assert result.description == "A cool mod."

    def test_title_fallback_to_page_title(self, tmp_path: Path) -> None:
        offline_dir = tmp_path / INFO_DIR_NAME / "offline"
        offline_dir.mkdir(parents=True, exist_ok=True)
        html = textwrap.dedent("""\
            <!DOCTYPE html><html><head>
            <title>Zoom Mod at Stardew Valley Nexus - Mods and community</title>
            </head><body><section data-mod-id="99"></section></body></html>
        """)
        index = offline_dir / "index.html"
        index.write_text(html, encoding="utf-8")
        result = parse_nexus_offline_html(index)
        assert result.title == "Zoom Mod"
        assert result.confidence["title"] == "medium"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        result = parse_nexus_offline_html(tmp_path / "does_not_exist.html")
        assert not result.any_useful()

    def test_malformed_html_does_not_raise(self, tmp_path: Path) -> None:
        offline_dir = tmp_path / INFO_DIR_NAME / "offline"
        offline_dir.mkdir(parents=True, exist_ok=True)
        index = offline_dir / "index.html"
        index.write_bytes(b"\xff\xfe broken \x00 garbage")
        result = parse_nexus_offline_html(index)
        assert isinstance(result, NexusOfflineCandidates)

    def test_no_gallery_cover_is_none(self, tmp_path: Path) -> None:
        offline_dir = tmp_path / INFO_DIR_NAME / "offline"
        offline_dir.mkdir(parents=True, exist_ok=True)
        html = textwrap.dedent("""\
            <!DOCTYPE html><html><head>
            <meta property="og:title" content="Bare Mod"/>
            <meta property="og:url" content="https://www.nexusmods.com/bg3/mods/1"/>
            </head><body><section data-mod-id="1"></section></body></html>
        """)
        index = offline_dir / "index.html"
        index.write_text(html, encoding="utf-8")
        result = parse_nexus_offline_html(index)
        assert result.title == "Bare Mod"
        assert result.cover_asset_path is None

    def test_metadata_json_fallback_for_url(self, tmp_path: Path) -> None:
        offline_dir = tmp_path / INFO_DIR_NAME / "offline"
        offline_dir.mkdir(parents=True, exist_ok=True)
        html = textwrap.dedent("""\
            <!DOCTYPE html><html><head>
            <title>X Mod at Game Nexus</title>
            </head><body><section data-mod-id="777"></section></body></html>
        """)
        index = offline_dir / "index.html"
        index.write_text(html, encoding="utf-8")
        (offline_dir / "metadata.json").write_text(
            json.dumps(
                {"original_url": "https://www.nexusmods.com/bg3/mods/777?tab=desc"}
            ),
            encoding="utf-8",
        )
        result = parse_nexus_offline_html(index)
        assert result.source_url == "https://www.nexusmods.com/bg3/mods/777"


# ===========================================================================
# Section 2 — Gallery image selection
# ===========================================================================


class TestGalleryImageSelection:
    def test_selects_gallery_image_not_ui_noise(self, tmp_path: Path) -> None:
        """Images outside #sidebargallery (logo, avatar) must not be selected."""
        offline_dir = tmp_path / INFO_DIR_NAME / "offline"
        offline_dir.mkdir(parents=True, exist_ok=True)
        assets = offline_dir / "assets"
        assets.mkdir()
        (assets / "logo.png").write_bytes(b"logo")
        (assets / "avatar.webp").write_bytes(b"avatar")
        (assets / "mod_screenshot.png").write_bytes(b"modimg")

        html = textwrap.dedent("""\
            <!DOCTYPE html><html><head>
            <meta property="og:title" content="Cool Mod"/>
            <meta property="og:url" content="https://www.nexusmods.com/sdv/mods/10455"/>
            </head><body>
            <section data-mod-id="10455"></section>
            <header><img src="./assets/logo.png"/></header>
            <div class="user-avatar"><img src="./assets/avatar.webp"/></div>
            <div class="clearfix modimages" id="sidebargallery">
              <ul class="thumbgallery gallery clearfix">
                <li><img src="./assets/mod_screenshot.png"/></li>
              </ul>
            </div>
            </body></html>
        """)
        index = offline_dir / "index.html"
        index.write_text(html, encoding="utf-8")
        result = parse_nexus_offline_html(index)
        assert result.cover_asset_path is not None
        assert result.cover_asset_path.name == "mod_screenshot.png"

    def test_missing_asset_file_returns_none(self, tmp_path: Path) -> None:
        offline_dir = tmp_path / INFO_DIR_NAME / "offline"
        offline_dir.mkdir(parents=True, exist_ok=True)
        (offline_dir / "assets").mkdir()
        html = textwrap.dedent("""\
            <!DOCTYPE html><html><head>
            <meta property="og:title" content="Mod"/>
            <meta property="og:url" content="https://www.nexusmods.com/x/mods/1"/>
            </head><body>
            <section data-mod-id="1"></section>
            <div id="sidebargallery">
              <ul class="thumbgallery gallery clearfix">
                <li><img src="./assets/ghost.png"/></li>
              </ul>
            </div>
            </body></html>
        """)
        index = offline_dir / "index.html"
        index.write_text(html, encoding="utf-8")
        result = parse_nexus_offline_html(index)
        assert result.cover_asset_path is None


# ===========================================================================
# Section 3 — apply_nexus_offline_candidates (merge logic)
# ===========================================================================


class TestApplyNexusOfflineCandidates:

    # T1 — all missing → all filled ----------------------------------------

    def test_fills_all_missing(self, tmp_path: Path, db: DatabaseManager) -> None:
        mid, dest = _register_nexus_mod(tmp_path, db, nexus_id="501")
        db.update_mod_identity_fields(mid, source_url="", external_id="")

        assets_dir = dest / INFO_DIR_NAME / "offline" / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        cover_asset = assets_dir / "cover.png"
        cover_asset.write_bytes(b"\x89PNG\r\n")

        candidates = NexusOfflineCandidates(
            title="Teleport NPC Location",
            source_url="https://www.nexusmods.com/stardewvalley/mods/5009",
            external_id="5009",
            cover_asset_path=cover_asset,
            description="Teleport NPCs.",
        )
        apply_nexus_offline_candidates(mid, dest, candidates, db=db)

        row = db.get_mod_display_info(mid)
        assert row is not None
        assert row.source_url == "https://www.nexusmods.com/stardewvalley/mods/5009"
        assert row.external_id == "5009"
        covers = list((dest / INFO_DIR_NAME).glob("cover.*"))
        assert covers, "Expected cover.* to be created"

    # T2 — all existing → nothing overwritten --------------------------------

    def test_does_not_overwrite_existing(self, tmp_path: Path, db: DatabaseManager) -> None:
        mid, dest = _register_nexus_mod(
            tmp_path, db, title="My Title", nexus_id="42",
            source_url="https://www.nexusmods.com/x/mods/42",
        )
        info_dir = dest / INFO_DIR_NAME
        info_dir.mkdir(parents=True, exist_ok=True)
        existing_cover = info_dir / "cover.png"
        existing_cover.write_bytes(b"ORIGINAL")
        db.update_mod_cover_path(mid, "cover.png")

        assets_dir = dest / INFO_DIR_NAME / "offline" / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        new_asset = assets_dir / "new.png"
        new_asset.write_bytes(b"\x89PNG\r\n")

        candidates = NexusOfflineCandidates(
            title="HTML Title",
            source_url="https://www.nexusmods.com/x/mods/99",
            external_id="99",
            cover_asset_path=new_asset,
        )
        apply_nexus_offline_candidates(mid, dest, candidates, db=db)

        row = db.get_mod_display_info(mid)
        assert row.source_url == "https://www.nexusmods.com/x/mods/42"
        assert row.external_id == "42"
        assert existing_cover.read_bytes() == b"ORIGINAL"

    # T3 — partial: url missing, title present --------------------------------

    def test_fills_only_missing_url(self, tmp_path: Path, db: DatabaseManager) -> None:
        mid, dest = _register_nexus_mod(tmp_path, db, title="My Title", nexus_id="43")
        db.update_mod_identity_fields(mid, source_url="")

        candidates = NexusOfflineCandidates(
            title="HTML Title",
            source_url="https://www.nexusmods.com/bg3/mods/366",
            external_id="366",
        )
        apply_nexus_offline_candidates(mid, dest, candidates, db=db)

        row = db.get_mod_display_info(mid)
        assert row.source_url == "https://www.nexusmods.com/bg3/mods/366"

    # T4 — cover missing → filled --------------------------------------------

    def test_fills_missing_cover(self, tmp_path: Path, db: DatabaseManager) -> None:
        mid, dest = _register_nexus_mod(tmp_path, db, nexus_id="44")
        db.update_mod_cover_path(mid, "")

        assets_dir = dest / INFO_DIR_NAME / "offline" / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        cover_asset = assets_dir / "mod.png"
        cover_asset.write_bytes(b"\x89PNG\r\n")

        candidates = NexusOfflineCandidates(cover_asset_path=cover_asset)
        apply_nexus_offline_candidates(mid, dest, candidates, db=db)

        covers = list((dest / INFO_DIR_NAME).glob("cover.*"))
        assert covers

    # T5 — existing cover → not overwritten ----------------------------------

    def test_preserves_existing_cover(self, tmp_path: Path, db: DatabaseManager) -> None:
        mid, dest = _register_nexus_mod(tmp_path, db, nexus_id="45")
        info_dir = dest / INFO_DIR_NAME
        info_dir.mkdir(parents=True, exist_ok=True)
        original = info_dir / "cover.png"
        original.write_bytes(b"KEEP_ME")
        db.update_mod_cover_path(mid, "cover.png")

        assets_dir = dest / INFO_DIR_NAME / "offline" / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        new_asset = assets_dir / "new.png"
        new_asset.write_bytes(b"\x89PNG\r\n")

        candidates = NexusOfflineCandidates(cover_asset_path=new_asset)
        apply_nexus_offline_candidates(mid, dest, candidates, db=db)
        assert original.read_bytes() == b"KEEP_ME"

    # T6 — user override on display_name -------------------------------------

    def test_respects_display_name_override(self, tmp_path: Path, db: DatabaseManager) -> None:
        mid, dest = _register_nexus_mod(tmp_path, db, nexus_id="46")
        db.set_user_override_field(mid, "display_name", overridden=True)

        candidates = NexusOfflineCandidates(title="HTML Title Should Be Ignored")
        apply_nexus_offline_candidates(mid, dest, candidates, db=db)

        row = db.get_mod_display_info(mid)
        assert row.display_name != "HTML Title Should Be Ignored"

    # T7 — empty candidates → no-op ------------------------------------------

    def test_empty_candidates_noop(self, tmp_path: Path, db: DatabaseManager) -> None:
        mid, dest = _register_nexus_mod(
            tmp_path, db, nexus_id="47",
            source_url="https://www.nexusmods.com/x/mods/47",
        )
        candidates = NexusOfflineCandidates()
        apply_nexus_offline_candidates(mid, dest, candidates, db=db)
        row = db.get_mod_display_info(mid)
        assert row.source_url == "https://www.nexusmods.com/x/mods/47"

    # T8 — only available fields filled, others skipped ---------------------

    def test_partial_candidates_fill_available(
        self, tmp_path: Path, db: DatabaseManager
    ) -> None:
        mid, dest = _register_nexus_mod(tmp_path, db, nexus_id="48")
        db.update_mod_identity_fields(mid, source_url="")

        candidates = NexusOfflineCandidates(
            source_url="https://www.nexusmods.com/sdv/mods/88",
        )
        apply_nexus_offline_candidates(mid, dest, candidates, db=db)

        row = db.get_mod_display_info(mid)
        assert row.source_url == "https://www.nexusmods.com/sdv/mods/88"


# ===========================================================================
# Section 4 — attach_nexus_offline_page integration
# ===========================================================================


class TestAttachTriggersScraperIntegration:

    def test_attach_fills_missing_source_url(
        self, tmp_path: Path, db: DatabaseManager
    ) -> None:
        """After attach_nexus_offline_page, empty source_url should be filled."""
        mid, dest = _register_nexus_mod(tmp_path, db, nexus_id="600")
        db.update_mod_identity_fields(mid, source_url="")

        # Create local gallery asset
        assets_dir = dest / INFO_DIR_NAME / "offline" / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / "mod.png").write_bytes(b"\x89PNG\r\n")

        html_text = _HTML_TEMPLATE.format(
            og_title="Stardrop",
            og_url="https://www.nexusmods.com/stardewvalley/mods/10455",
            page_title="Stardrop at Stardew Valley Nexus",
            description="A mod launcher.",
            mod_id="10455",
            gallery_items='<li><img src="./assets/mod.png"/></li>',
        )
        html_source = tmp_path / "stardrop.html"
        html_source.write_text(html_text, encoding="utf-8")

        attach_nexus_offline_page(
            mid, html_source, managed_path=dest,
            library_root=dest.parents[1],
        )

        row = db.get_mod_display_info(mid)
        assert row is not None
        assert row.source_url == "https://www.nexusmods.com/stardewvalley/mods/10455"
        assert row.offline_status == OFFLINE_STATUS_ARCHIVED

    def test_attach_does_not_overwrite_existing_source_url(
        self, tmp_path: Path, db: DatabaseManager
    ) -> None:
        """Existing source_url must survive attach_nexus_offline_page."""
        mid, dest = _register_nexus_mod(
            tmp_path, db, nexus_id="601",
            source_url="https://www.nexusmods.com/x/mods/601",
        )

        html_text = _HTML_TEMPLATE.format(
            og_title="Different Mod",
            og_url="https://www.nexusmods.com/sdv/mods/9999",
            page_title="Different Mod at Nexus",
            description="",
            mod_id="9999",
            gallery_items="",
        )
        html_source = tmp_path / "different.html"
        html_source.write_text(html_text, encoding="utf-8")

        attach_nexus_offline_page(
            mid, html_source, managed_path=dest,
            library_root=dest.parents[1],
        )

        row = db.get_mod_display_info(mid)
        assert row.source_url == "https://www.nexusmods.com/x/mods/601"


# ===========================================================================
# Section 5 — Refresh isolation
# ===========================================================================


class TestRefreshIsolation:
    """Contract C: refresh_mod() and reconcile_local_state() MUST NOT call scraper."""

    def test_refresh_mod_does_not_call_scraper(
        self, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T9 / T19 (hardest regression test): refresh_mod must not invoke parser."""
        from services.mod_refresh import refresh_mod

        mid, dest = _register_nexus_mod(tmp_path, db, nexus_id="700")
        # Plant an offline HTML so the scraper *could* run if wrongly wired
        _write_offline_html(dest, og_title="Should Never Be Read", mod_id="700")

        parser_mock = MagicMock(return_value=NexusOfflineCandidates())
        monkeypatch.setattr(
            "services.offline.nexus_html_parser.parse_nexus_offline_html",
            parser_mock,
        )

        refresh_mod(
            mid,
            dest,
            platform=PLATFORM_NEXUS,
            library_root=dest.parents[1],
            db=db,
        )

        parser_mock.assert_not_called()

    def test_reconcile_does_not_call_scraper(
        self, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T10: reconcile_local_state must not invoke parser."""
        from services.mod_refresh import reconcile_local_state

        mid, dest = _register_nexus_mod(tmp_path, db, nexus_id="800")

        parser_mock = MagicMock(return_value=NexusOfflineCandidates())
        monkeypatch.setattr(
            "services.offline.nexus_html_parser.parse_nexus_offline_html",
            parser_mock,
        )

        reconcile_local_state(mid, dest, library_root=dest.parents[1], db=db)

        parser_mock.assert_not_called()


# ===========================================================================
# Section 6 — Platform isolation
# ===========================================================================


class TestPlatformIsolation:

    def test_steam_import_does_not_trigger_scraper(
        self, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T11: Steam platform must never reach the Nexus scraper."""
        from ui.import_thread import ImportWorker
        from core.game_info import GameInfo

        db.upsert_game(GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld"))

        parser_mock = MagicMock(return_value=NexusOfflineCandidates())
        monkeypatch.setattr(
            "services.offline.nexus_html_parser.parse_nexus_offline_html",
            parser_mock,
        )

        html = tmp_path / "steam_page.html"
        html.write_text("<html><body>Steam</body></html>", encoding="utf-8")

        worker = ImportWorker(
            platform=PLATFORM_STEAM,
            library_root=tmp_path / "lib",
            params={
                "workshop_id": "9001",
                "title": "SteamMod",
                "game_id": 1623730,
                "game_name": "Palworld",
                "offline_html_path": str(html),
            },
        )
        worker._do_import()
        parser_mock.assert_not_called()


# ===========================================================================
# Section 7 — Idempotency
# ===========================================================================


class TestIdempotency:

    def test_reimport_same_html_does_not_change_existing(
        self, tmp_path: Path, db: DatabaseManager
    ) -> None:
        """T12: importing the same HTML twice must be idempotent."""
        mid, dest = _register_nexus_mod(tmp_path, db, nexus_id="999")
        db.update_mod_identity_fields(mid, source_url="")

        assets_dir = dest / INFO_DIR_NAME / "offline" / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / "cover.png").write_bytes(b"\x89PNG\r\n")

        html_text = _HTML_TEMPLATE.format(
            og_title="Teleport NPC Location",
            og_url="https://www.nexusmods.com/stardewvalley/mods/10455",
            page_title="Teleport NPC at Stardew Valley Nexus",
            description="Teleport NPCs",
            mod_id="10455",
            gallery_items='<li><img src="./assets/cover.png"/></li>',
        )
        html_source = tmp_path / "mod_page.html"
        html_source.write_text(html_text, encoding="utf-8")

        # First import
        attach_nexus_offline_page(
            mid, html_source, managed_path=dest, library_root=dest.parents[1]
        )
        row1 = db.get_mod_display_info(mid)
        url1 = row1.source_url
        ext1 = row1.external_id

        # Second import — same HTML, same expectations
        attach_nexus_offline_page(
            mid, html_source, managed_path=dest, library_root=dest.parents[1]
        )
        row2 = db.get_mod_display_info(mid)
        assert row2.source_url == url1
        assert row2.external_id == ext1


# ===========================================================================
# Section 7b — Real HTML + import-default regression
# ===========================================================================


REAL_OFFLINE_HTML = Path("data/mod_backup/9000000000000406/offline/index.html")


def _find_real_user_html() -> Path | None:
    for path in Path("mod").rglob("Empty Mod f20722b2/.info/offline/index.html"):
        if path.is_file():
            return path
    return None


REAL_USER_HTML = _find_real_user_html()
REAL_USER_EXPECTED_URL = "https://www.nexusmods.com/stardewvalley/mods/10062"
REAL_USER_EXPECTED_ID = "10062"
REAL_USER_EXPECTED_TITLE = "Hugs and Kisses"


@pytest.mark.skipif(not REAL_OFFLINE_HTML.is_file(), reason="real offline HTML sample missing")
class TestRealOfflineHtmlParsing:
    def test_real_html_extracts_title(self) -> None:
        result = parse_nexus_offline_html(REAL_OFFLINE_HTML)
        assert result.title == "Stardrop"
        assert result.confidence.get("title") == "high"

    def test_real_html_extracts_source_url(self) -> None:
        result = parse_nexus_offline_html(REAL_OFFLINE_HTML)
        assert result.source_url == "https://www.nexusmods.com/stardewvalley/mods/10455"
        assert "/stardewvalley/mods/10455" in result.source_url

    def test_real_html_extracts_external_id(self) -> None:
        result = parse_nexus_offline_html(REAL_OFFLINE_HTML)
        assert result.external_id == "10455"

    def test_real_html_has_title_url_and_cover(self) -> None:
        result = parse_nexus_offline_html(REAL_OFFLINE_HTML)
        assert result.title
        assert result.source_url
        assert result.external_id
        assert result.cover_asset_path is not None
        assert result.cover_asset_path.is_file()


class TestImportDefaultMerge:
    def test_folder_title_and_wrong_url_updated_from_html(
        self, tmp_path: Path, db: DatabaseManager
    ) -> None:
        """Import folder name + auto-generated wrong-game URL → HTML canonical wins."""
        src = tmp_path / "ZoomMod"
        src.mkdir()
        (src / "mod.pak").write_bytes(b"x")
        lib = tmp_path / "lib"
        result = NexusImporter(db=db).import_mod(
            source_folder=src,
            title="ZoomMod",
            nexus_url="",
            nexus_id="10455",
            library_root=lib,
            context=PALWORLD,
        )
        assert result.success
        mid, dest = str(result.mod_id), Path(result.managed_path)

        if REAL_OFFLINE_HTML.is_file():
            html_source = tmp_path / "real.html"
            html_source.write_text(
                REAL_OFFLINE_HTML.read_text(encoding="utf-8"), encoding="utf-8"
            )
        else:
            html_source = tmp_path / "fallback.html"
            html_source.write_text(
                _HTML_TEMPLATE.format(
                    og_title="Stardrop",
                    og_url="https://www.nexusmods.com/stardewvalley/mods/10455",
                    page_title="Stardrop at Stardew Valley Nexus",
                    description="desc",
                    mod_id="10455",
                    gallery_items="",
                ),
                encoding="utf-8",
            )

        attach_nexus_offline_page(
            mid, html_source, managed_path=dest, library_root=lib
        )
        row = db.get_mod_display_info(mid)
        assert row.steam_name == "Stardrop"
        assert row.display_name == "Stardrop"
        assert row.source_url == "https://www.nexusmods.com/stardewvalley/mods/10455"

    def test_placeholder_title_synced_to_db(
        self, tmp_path: Path, db: DatabaseManager
    ) -> None:
        mid, dest = _register_nexus_mod(tmp_path, db, title="Unknown_Mod_8888", nexus_id="8888")
        db.update_mod_identity_fields(mid, source_url="")

        html = tmp_path / "page.html"
        html.write_text(
            _HTML_TEMPLATE.format(
                og_title="Stardrop",
                og_url="https://www.nexusmods.com/stardewvalley/mods/10455",
                page_title="Stardrop",
                description="desc",
                mod_id="10455",
                gallery_items="",
            ),
            encoding="utf-8",
        )
        attach_nexus_offline_page(mid, html, managed_path=dest, library_root=dest.parents[1])
        row = db.get_mod_display_info(mid)
        assert row.steam_name == "Stardrop"
        assert row.display_name == "Stardrop"
        assert row.source_url == "https://www.nexusmods.com/stardewvalley/mods/10455"


@pytest.mark.skipif(
    REAL_USER_HTML is None or not REAL_USER_HTML.is_file(),
    reason="real user offline HTML missing",
)
class TestRealUserOfflineHtml:
    def test_real_user_html_parser(self) -> None:
        result = parse_nexus_offline_html(REAL_USER_HTML)
        assert result.title == REAL_USER_EXPECTED_TITLE
        assert result.source_url == REAL_USER_EXPECTED_URL
        assert result.external_id == REAL_USER_EXPECTED_ID

    def test_fake_folder_state_merge_from_real_html(
        self, tmp_path: Path, db: DatabaseManager
    ) -> None:
        """Folder-name external_id + fake URL must be corrected by real HTML merge."""
        from services.offline.nexus_html_parser import (
            _patch_mod_db_identity,
            apply_nexus_offline_candidates,
        )

        folder = tmp_path / "Empty Mod f20722b2"
        folder.mkdir()
        (folder / "mod.pak").write_bytes(b"x")
        lib = tmp_path / "lib"
        result = NexusImporter(db=db).import_mod(
            source_folder=folder,
            title="Empty Mod f20722b2",
            nexus_url="",
            nexus_id="",
            library_root=lib,
            context=PALWORLD,
        )
        assert result.success
        mid = str(result.mod_id)
        dest = Path(result.managed_path)
        fake_url = "https://www.nexusmods.com/星露谷物语/mods/Empty Mod f20722b2"
        _patch_mod_db_identity(
            db,
            mid,
            title="Empty Mod f20722b2",
            source_url=fake_url,
            external_id="Empty Mod f20722b2",
        )

        candidates = parse_nexus_offline_html(REAL_USER_HTML)
        apply_nexus_offline_candidates(mid, dest, candidates, db=db)

        row = db.get_mod_display_info(mid)
        assert row is not None
        assert row.steam_name == REAL_USER_EXPECTED_TITLE
        assert row.external_id == REAL_USER_EXPECTED_ID
        assert row.source_url == REAL_USER_EXPECTED_URL

    def test_existing_mod_attach_from_real_html(
        self, tmp_path: Path, db: DatabaseManager
    ) -> None:
        """Case B: existing mod with fake URL → attach real offline HTML."""
        from services.offline.nexus_html_parser import _patch_mod_db_identity

        folder = tmp_path / "Empty Mod f20722b2"
        folder.mkdir()
        (folder / "mod.pak").write_bytes(b"x")
        lib = tmp_path / "lib"
        result = NexusImporter(db=db).import_mod(
            source_folder=folder,
            title="Empty Mod f20722b2",
            nexus_url="",
            nexus_id="",
            library_root=lib,
            context={"game_id": 413150, "game_name": "星露谷物语"},
        )
        assert result.success
        mid = str(result.mod_id)
        dest = Path(result.managed_path)
        fake_url = "https://www.nexusmods.com/星露谷物语/mods/Empty Mod f20722b2"
        _patch_mod_db_identity(
            db,
            mid,
            title="Empty Mod f20722b2",
            source_url=fake_url,
            external_id="Empty Mod f20722b2",
        )

        html_copy = tmp_path / "real_user.html"
        html_copy.write_text(REAL_USER_HTML.read_text(encoding="utf-8"), encoding="utf-8")
        attach_nexus_offline_page(
            mid,
            html_copy,
            managed_path=dest,
            library_root=lib,
        )

        row = db.get_mod_display_info(mid)
        assert row is not None
        assert row.steam_name == REAL_USER_EXPECTED_TITLE
        assert row.external_id == REAL_USER_EXPECTED_ID
        assert row.source_url == REAL_USER_EXPECTED_URL


class TestNexusImporterNoFakeUrl:
    def test_no_fake_url_without_nexus_id(self, tmp_path: Path, db: DatabaseManager) -> None:
        folder = tmp_path / "Empty Mod f20722b2"
        folder.mkdir()
        (folder / "mod.pak").write_bytes(b"x")
        lib = tmp_path / "lib"
        result = NexusImporter(db=db).import_mod(
            source_folder=folder,
            title="Empty Mod f20722b2",
            nexus_url="",
            nexus_id="",
            library_root=lib,
            context=PALWORLD,
        )
        assert result.success
        assert result.source_url == ""
        assert result.external_id == "Empty Mod f20722b2"
        assert "nexusmods.com" not in (result.source_url or "")
        info = db.get_mod_display_info(result.mod_id)
        assert info is not None
        assert (info.source_url or "") == ""
        assert info.external_id == "Empty Mod f20722b2"

    def test_import_plus_real_html_e2e(self, tmp_path: Path, db: DatabaseManager) -> None:
        """Case A: import mod without Nexus ID, then attach real offline HTML."""
        if REAL_USER_HTML is None or not REAL_USER_HTML.is_file():
            pytest.skip("real user offline HTML missing")
        folder = tmp_path / "Empty Mod f20722b2"
        folder.mkdir()
        (folder / "mod.pak").write_bytes(b"x")
        lib = tmp_path / "lib"
        result = NexusImporter(db=db).import_mod(
            source_folder=folder,
            title="Empty Mod f20722b2",
            nexus_url="",
            nexus_id="",
            library_root=lib,
            context={"game_id": 413150, "game_name": "星露谷物语"},
        )
        assert result.success
        mid = str(result.mod_id)
        dest = Path(result.managed_path)
        assert (result.source_url or "") == ""

        html_copy = tmp_path / "real_user.html"
        html_copy.write_text(REAL_USER_HTML.read_text(encoding="utf-8"), encoding="utf-8")
        attach_nexus_offline_page(mid, html_copy, managed_path=dest, library_root=lib)

        row = db.get_mod_display_info(mid)
        assert row is not None
        assert row.steam_name == REAL_USER_EXPECTED_TITLE
        assert row.external_id == REAL_USER_EXPECTED_ID
        assert row.source_url == REAL_USER_EXPECTED_URL


# ===========================================================================
# Section 8 — Scraper failure is non-blocking
# ===========================================================================


class TestScraperFailureNonBlocking:

    def test_parse_exception_does_not_fail_attach(
        self, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """T8: scraper exception must not propagate to attach_nexus_offline_page."""
        mid, dest = _register_nexus_mod(tmp_path, db, nexus_id="111")

        def exploding_parse(path: Path) -> NexusOfflineCandidates:
            raise RuntimeError("Intentional test failure")

        monkeypatch.setattr(
            "services.offline.nexus_html_parser.parse_nexus_offline_html",
            exploding_parse,
        )

        html = tmp_path / "page.html"
        html.write_text(
            "<html><head>"
            "<meta property='og:title' content='X'/>"
            "<meta property='og:url' content='https://www.nexusmods.com/x/mods/1'/>"
            "</head><body><section data-mod-id='1'></section></body></html>",
            encoding="utf-8",
        )

        result = attach_nexus_offline_page(
            mid, html, managed_path=dest, library_root=dest.parents[1]
        )
        assert result.status == OFFLINE_STATUS_ARCHIVED

    def test_apply_exception_does_not_fail_attach(
        self, tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """apply_nexus_offline_candidates raising must not propagate."""
        mid, dest = _register_nexus_mod(tmp_path, db, nexus_id="112")
        _write_offline_html(dest, mod_id="112")

        monkeypatch.setattr(
            "services.offline.nexus_html_parser.apply_nexus_offline_candidates",
            MagicMock(side_effect=RuntimeError("apply failure")),
        )

        html = tmp_path / "page2.html"
        html.write_text(
            "<html><head>"
            "<meta property='og:title' content='X'/>"
            "<meta property='og:url' content='https://www.nexusmods.com/x/mods/2'/>"
            "</head><body><section data-mod-id='2'></section></body></html>",
            encoding="utf-8",
        )
        result = attach_nexus_offline_page(
            mid, html, managed_path=dest, library_root=dest.parents[1]
        )
        assert result.status == OFFLINE_STATUS_ARCHIVED
