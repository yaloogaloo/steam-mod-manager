"""Offline HTML local resource → cover candidate after CAS finalize.

The gallery scraper must resolve ``./assets/...`` through the current
resource authority once capture staging and sibling ``.info/offline/assets``
are gone. Remote gallery URLs stay unresolved-as-covers.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from core.cas_runtime import reset_cas_runtime_cache
from core.db_manager import DatabaseManager
from services.asset_manifest import MANIFEST_FILENAME, AssetManifest
from services.file_ops import INFO_DIR_NAME, read_info_metadata_dict
from services.importers.image_picker import apply_cover_to_mod
from services.importers.importer_base import ImportContext
from services.importers.nexus import NexusImporter
from services.offline.html_rewriter import rewrite_imported_html
from services.offline.manual_import import import_offline_snapshot
from services.offline.nexus_html_parser import (
    NO_LOCAL_COVER_REFERENCE,
    REMOTE_RESOURCE_SKIPPED,
    RESOLVED_LOCAL_COVER,
    UNRESOLVED_LOCAL_RESOURCE,
    apply_nexus_offline_candidates,
    parse_nexus_offline_html,
)
from services.offline.staging import capture_assets_dir, capture_staging_root

PALWORLD = ImportContext(game_id=1623730, game_name="Palworld")
PNG = b"\x89PNG\r\n\x1a\n" + b"captured-local-cover"
USER_PNG = b"\x89PNG\r\n\x1a\n" + b"USER-OWNED-COVER"
OG_URL = "https://www.nexusmods.com/stardewvalley/mods/10455"
TITLE = "Teleport NPC Location"
DESCRIPTION = "Teleports NPCs."
NEXUS_ID = "10455"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "cover_lifecycle.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def _isolated_asset_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = tmp_path / "asset_store"
    cache = tmp_path / "cache_temp"
    store.mkdir()
    cache.mkdir()
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store)
    monkeypatch.setattr("core.paths.cache_temp_dir", lambda: cache)
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    reset_cas_runtime_cache()
    yield store
    reset_cas_runtime_cache()


def _register(tmp_path: Path, db: DatabaseManager) -> tuple[str, Path]:
    src = tmp_path / "src_mod"
    src.mkdir()
    (src / "mod.pak").write_bytes(b"pak")
    result = NexusImporter(db=db).import_mod(
        source_folder=src,
        title="Unknown_Mod_Placeholder",
        nexus_url="https://www.nexusmods.com/x/mods/0",
        nexus_id="0",
        library_root=tmp_path / "lib",
        context=PALWORLD,
    )
    assert result.success, result.error
    mid = str(result.mod_id)
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET title = 'Unknown_Mod', display_name = '', "
            "source_url = '', external_id = '', workspace_id = '', "
            "cover_path = '' WHERE mod_id = ?",
            (int(mid),),
        )
        db._conn.commit()
    return mid, Path(result.managed_path)


def _saved_page(folder: Path, png: bytes, *, image_name: str = "cover.png") -> Path:
    """Browser-save style page: HTML plus a local companion image."""
    folder.mkdir(parents=True, exist_ok=True)
    image = folder / "assets" / "images" / image_name
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(png)
    html = f"""<!DOCTYPE html>
<html><head>
<meta property="og:title" content="{TITLE}"/>
<meta property="og:url" content="{OG_URL}"/>
<meta property="og:image" content="https://cdn.example/remote-og.png"/>
<meta name="description" content="{DESCRIPTION}"/>
</head><body>
<section data-mod-id="{NEXUS_ID}"></section>
<div id="sidebargallery">
  <ul class="thumbgallery gallery">
    <li><img src="./assets/images/{image_name}"/></li>
  </ul>
</div>
</body></html>
"""
    page = folder / "page.html"
    page.write_text(html, encoding="utf-8")
    return page


def _clear_identity(db: DatabaseManager, mid: str) -> None:
    with db._lock:
        db._conn.execute(
            "UPDATE mods SET source_url = '', external_id = '', workspace_id = '' "
            "WHERE mod_id = ?",
            (int(mid),),
        )
        db._conn.commit()


def _cover_bytes(dest: Path) -> bytes:
    covers = list((dest / INFO_DIR_NAME).glob("cover.*"))
    assert covers, "expected .info/cover.*"
    return covers[0].read_bytes()


def _db_cover_path(db: DatabaseManager, mid: str) -> str:
    with db._lock:
        row = db._conn.execute(
            "SELECT cover_path FROM mods WHERE mod_id = ?",
            (int(mid),),
        ).fetchone()
    assert row is not None
    return str(row[0] or "")


def _assert_cas_only(offline: Path, store: Path) -> None:
    staging = capture_staging_root(offline)
    assert not staging.exists() or not any(staging.rglob("*"))
    sibling = offline / "assets"
    if sibling.exists():
        assert not any(p.is_file() for p in sibling.rglob("*"))
    manifest = AssetManifest.from_path(offline / MANIFEST_FILENAME)
    assert any(ref.path == "assets/images/cover.png" for ref in manifest.assets)
    assert any(p.is_file() for p in store.rglob("*"))
    html = (offline / "index.html").read_text(encoding="utf-8")
    assert "./assets/images/cover.png" in html


def test_import_snapshot_cas_only_cover_enters_apply_lifecycle(
    tmp_path: Path, db: DatabaseManager, _isolated_asset_authority: Path
) -> None:
    mid, dest = _register(tmp_path, db)
    meta_before = read_info_metadata_dict(dest) or {}
    internal_id = str(meta_before.get("internal_id") or "")
    assert internal_id
    saved = tmp_path / "saved"
    page = _saved_page(saved, PNG)
    original_html = page.read_bytes()
    offline = dest / INFO_DIR_NAME / "offline"

    index, count, source_format = import_offline_snapshot(page, offline)
    assert source_format == "html"
    assert count >= 1
    assert page.read_bytes() == original_html
    _assert_cas_only(offline, _isolated_asset_authority)

    parsed = parse_nexus_offline_html(index)
    assert parsed.title == TITLE
    assert parsed.description == DESCRIPTION
    assert parsed.source_url == OG_URL
    assert parsed.external_id == NEXUS_ID
    assert parsed.cover_resolution == RESOLVED_LOCAL_COVER
    assert parsed.cover_asset_path is not None
    assert parsed.cover_asset_path.is_file()
    assert parsed.cover_asset_path.read_bytes() == PNG
    assert _db_cover_path(db, mid) == ""

    apply_nexus_offline_candidates(mid, dest, parsed, db=db)

    assert _db_cover_path(db, mid)
    assert _cover_bytes(dest) == PNG
    meta = read_info_metadata_dict(dest) or {}
    assert str(meta.get("internal_id") or "") == internal_id
    assert str(meta.get("description") or "") == DESCRIPTION
    cover_rel = str(meta.get("cover_path") or "")
    assert cover_rel
    assert (dest / cover_rel).is_file()
    assert (dest / cover_rel).read_bytes() == PNG
    row = db.get_mod_display_info(mid)
    assert row is not None
    assert row.steam_name == TITLE
    assert row.source_url == OG_URL
    assert row.external_id == NEXUS_ID
    assert (offline / MANIFEST_FILENAME).is_file()


def test_authority_migration_keeps_cover_candidate(
    tmp_path: Path, db: DatabaseManager, _isolated_asset_authority: Path
) -> None:
    """Staging today and CAS after cleanup must both yield a cover candidate."""
    from services.info_asset_runtime import require_cas_finalize
    from services.offline.staging import cleanup_capture_staging

    _mid, dest = _register(tmp_path, db)
    offline = dest / INFO_DIR_NAME / "offline"
    offline.mkdir(parents=True)
    page = _saved_page(tmp_path / "saved", PNG)
    rewritten, copied = rewrite_imported_html(
        page.read_text(encoding="utf-8"),
        html_path=page,
        output_dir=offline,
    )
    assert copied >= 1
    index = offline / "index.html"
    index.write_text(rewritten, encoding="utf-8")

    staged_assets = capture_assets_dir(offline, create=False)
    assert any(p.is_file() for p in staged_assets.rglob("*"))
    while_staged = parse_nexus_offline_html(index)
    assert while_staged.cover_resolution == RESOLVED_LOCAL_COVER
    assert while_staged.cover_asset_path is not None
    assert while_staged.cover_asset_path.read_bytes() == PNG

    require_cas_finalize(offline, context="lifecycle")
    cleanup_capture_staging(offline)
    _assert_cas_only(offline, _isolated_asset_authority)

    after = parse_nexus_offline_html(index)
    assert after.cover_resolution == RESOLVED_LOCAL_COVER
    assert after.cover_asset_path is not None
    assert after.cover_asset_path.read_bytes() == PNG
    sibling = offline / "assets"
    if sibling.exists():
        try:
            after.cover_asset_path.resolve().relative_to(sibling.resolve())
        except ValueError:
            pass
        else:
            raise AssertionError("cover candidate still depends on sibling assets")
    assert after.title == TITLE
    assert after.description == DESCRIPTION
    assert after.source_url == OG_URL
    assert after.external_id == NEXUS_ID


def test_unresolved_local_reference_is_not_no_cover(
    tmp_path: Path, db: DatabaseManager, caplog: pytest.LogCaptureFixture
) -> None:
    mid, dest = _register(tmp_path, db)
    offline = dest / INFO_DIR_NAME / "offline"
    offline.mkdir(parents=True)
    index = offline / "index.html"
    index.write_text(
        """<!DOCTYPE html><html><head>
        <meta property="og:title" content="Teleport NPC Location"/>
        <meta property="og:url" content="https://www.nexusmods.com/stardewvalley/mods/10455"/>
        </head><body>
        <section data-mod-id="10455"></section>
        <div id="sidebargallery"><ul class="thumbgallery gallery">
          <li><img src="./assets/images/missing.png"/></li>
        </ul></div>
        </body></html>""",
        encoding="utf-8",
    )
    assert not capture_staging_root(offline).exists() or not any(
        capture_staging_root(offline).rglob("*")
    )
    assert not (offline / MANIFEST_FILENAME).exists()

    parsed = parse_nexus_offline_html(index)
    assert parsed.cover_asset_path is None
    assert parsed.cover_resolution == UNRESOLVED_LOCAL_RESOURCE
    assert parsed.cover_resolution != NO_LOCAL_COVER_REFERENCE

    caplog.set_level(logging.INFO)
    apply_nexus_offline_candidates(mid, dest, parsed, db=db)
    assert UNRESOLVED_LOCAL_RESOURCE in caplog.text
    assert "no gallery cover found" not in caplog.text
    assert not list((dest / INFO_DIR_NAME).glob("cover.*"))
    assert _db_cover_path(db, mid) == ""


def test_remote_gallery_and_og_image_are_not_local_covers(tmp_path: Path) -> None:
    offline = tmp_path / INFO_DIR_NAME / "offline"
    offline.mkdir(parents=True)
    index = offline / "index.html"
    index.write_text(
        """<!DOCTYPE html><html><head>
        <meta property="og:title" content="Teleport NPC Location"/>
        <meta property="og:image" content="https://cdn.example/og.png"/>
        <meta property="og:url" content="https://www.nexusmods.com/stardewvalley/mods/10455"/>
        </head><body>
        <div id="sidebargallery"><ul class="thumbgallery gallery">
          <li><img src="https://cdn.example/gallery.jpg"/></li>
        </ul></div>
        </body></html>""",
        encoding="utf-8",
    )
    parsed = parse_nexus_offline_html(index)
    assert parsed.cover_asset_path is None
    assert parsed.cover_resolution == REMOTE_RESOURCE_SKIPPED
    assert parsed.title == TITLE


def test_page_without_cover_reference_is_distinct(tmp_path: Path) -> None:
    offline = tmp_path / INFO_DIR_NAME / "offline"
    offline.mkdir(parents=True)
    index = offline / "index.html"
    index.write_text(
        """<!DOCTYPE html><html><head>
        <meta property="og:title" content="Bare"/>
        <meta property="og:image" content="https://cdn.example/og.png"/>
        </head><body><section data-mod-id="1"></section></body></html>""",
        encoding="utf-8",
    )
    parsed = parse_nexus_offline_html(index)
    assert parsed.cover_asset_path is None
    assert parsed.cover_resolution == NO_LOCAL_COVER_REFERENCE


def test_existing_user_cover_is_not_replaced(
    tmp_path: Path, db: DatabaseManager, _isolated_asset_authority: Path
) -> None:
    mid, dest = _register(tmp_path, db)
    user_file = tmp_path / "user.png"
    user_file.write_bytes(USER_PNG)
    rel = apply_cover_to_mod(
        dest,
        user_file,
        mod_id=mid,
        update_db=True,
        sync_backup=False,
        mark_user_override=True,
    )
    assert rel
    assert _cover_bytes(dest) == USER_PNG
    _clear_identity(db, mid)

    page = _saved_page(tmp_path / "saved", PNG)
    offline = dest / INFO_DIR_NAME / "offline"
    index, _count, _fmt = import_offline_snapshot(page, offline)
    parsed = parse_nexus_offline_html(index)
    assert parsed.cover_resolution == RESOLVED_LOCAL_COVER
    assert parsed.cover_asset_path is not None

    apply_nexus_offline_candidates(mid, dest, parsed, db=db)
    assert _cover_bytes(dest) == USER_PNG
    assert _db_cover_path(db, mid)
    assert PNG not in _cover_bytes(dest)
