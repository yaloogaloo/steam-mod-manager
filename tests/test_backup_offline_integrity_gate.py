"""Backup Offline Snapshot = index.html + local dependency closure."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import OFFLINE_STATUS_ARCHIVED, PLATFORM_STEAM
from services.asset_manifest import MANIFEST_FILENAME, AssetManifest
from services.file_ops import INFO_DIR_NAME, persist_unified_metadata_dict
from services.identity_service import create_mod_identity, identity_create_scope
from services.metadata_backup import (
    BACKUP_OFFLINE_DIR,
    BACKUP_OFFLINE_INDEX,
    backup_root,
    mark_missing,
    snapshot_from_mod_folder,
)
from services.metadata_backup_sync import (
    drain_backup_queue,
    rebuild_missing_metadata_backup,
    sync_after_metadata_change,
)
from services.metadata_backup_validator import validate_backup
from services.mod_presence import backup_offline_index, ENTITY_MISS, entity_state
from services.offline.backup_closure import (
    backup_offline_snapshot_valid,
    collect_offline_closure,
    ensure_backup_offline_openable,
    snapshot_offline_closure,
    validate_offline_snapshot,
)
from services.offline.backup_offline_repair import repair_live_offline_backups
from services.offline.paths import resolve_offline_page
from tests.helpers.identity import bind_managed_path, write_info_sidecar

APP_ID = 4242
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\r\x18\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "offline_gate.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name="GameA", folder_name="GameA"))
    yield manager
    drain_backup_queue(timeout=5.0)
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    monkeypatch.setattr("core.paths.data_dir", lambda: root)
    return root


@pytest.fixture(autouse=True)
def _patch_get_db(db: DatabaseManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)


def _seed(
    db: DatabaseManager,
    tmp_path: Path,
    *,
    workshop_id: str,
    title: str,
) -> tuple[Path, str]:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=workshop_id,
            workshop_id=workshop_id,
            title=title,
            app_id=APP_ID,
            game_name="GameA",
        )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "")
    folder = tmp_path / "mod" / "GameA" / title
    folder.mkdir(parents=True)
    (folder / "payload.bin").write_bytes(b"mod-payload")
    write_info_sidecar(
        folder,
        internal_id=frozen,
        title=title,
        external_id=workshop_id,
        workspace_id=workshop_id,
        app_id=APP_ID,
        game_name="GameA",
        extra={
            "source_url": f"https://example.test/{workshop_id}",
            "source_type": "steam",
            "offline_status": OFFLINE_STATUS_ARCHIVED,
        },
    )
    bind_managed_path(db, pk, folder, game_name="GameA", title=title)
    return folder, pk


def _write_steam_page(folder: Path) -> Path:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    assets = info / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "theme.css").write_text(
        'body{color:red;background:url("hero.png")}@font-face{src:url(font.woff)}',
        encoding="utf-8",
    )
    (assets / "hero.png").write_bytes(b"\x89PNG" + b"h" * 20)
    (assets / "font.woff").write_bytes(b"WOFFDATA")
    (assets / "unused.gif").write_bytes(b"GIF89aUNUSED")
    html = (
        '<html><head><link rel="stylesheet" href="./assets/theme.css">'
        "</head><body><img src=\"./assets/hero.png\" alt=\"x\"></body></html>"
    )
    index = info / "index.html"
    index.write_text(html, encoding="utf-8")
    return index


def _write_canonical_page(folder: Path) -> Path:
    offline = folder / INFO_DIR_NAME / "offline"
    offline.mkdir(parents=True, exist_ok=True)
    assets = offline / "assets"
    assets.mkdir(exist_ok=True)
    (assets / "all.css").write_text("body{color:blue}", encoding="utf-8")
    (assets / "skip.bin").write_bytes(b"NOCOPY")
    html = (
        '<html><head><link rel="stylesheet" href="./assets/all.css"></head>'
        "<body>canonical</body></html>"
    )
    index = offline / "index.html"
    index.write_text(html, encoding="utf-8")
    return index


def _backup_offline(pk: str) -> Path:
    return backup_root(pk) / BACKUP_OFFLINE_DIR


def _manifest_asset_paths(dest: Path) -> set[str]:
    man_path = dest / MANIFEST_FILENAME
    assert man_path.is_file()
    return {a.path for a in AssetManifest.from_path(man_path).assets}


def _assert_cas_only_offline(dest: Path, *expected_assets: str) -> None:
    """Phase 5: durable offline is index + manifest; assets live in CAS."""
    assert (dest / BACKUP_OFFLINE_INDEX).is_file()
    assert (dest / MANIFEST_FILENAME).is_file()
    assert not (dest / "assets").exists()
    if expected_assets:
        paths = _manifest_asset_paths(dest)
        for rel in expected_assets:
            assert rel in paths
    assert backup_offline_snapshot_valid(dest)


def test_steam_info_index_discovered(tmp_path: Path) -> None:
    folder = tmp_path / "mod" / "GameA" / "SteamLegacy"
    _write_steam_page(folder)
    found = resolve_offline_page(folder)
    assert found is not None
    assert found.name == "index.html"
    assert found.parent.name == INFO_DIR_NAME


def test_canonical_offline_index_discovered(tmp_path: Path) -> None:
    folder = tmp_path / "mod" / "GameA" / "Canon"
    _write_canonical_page(folder)
    found = resolve_offline_page(folder)
    assert found is not None
    assert found.as_posix().endswith(".info/offline/index.html")


def test_writer_uses_same_resolver_as_open(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="3748388534", title="CaseA")
    src = _write_steam_page(folder)
    assert resolve_offline_page(folder) == src.resolve()
    snap = snapshot_from_mod_folder(folder, owner_mod_id=pk)
    assert snap is not None
    assert Path(snap.offline_path).is_file()
    assert Path(snap.offline_path).read_text(encoding="utf-8") == src.read_text(
        encoding="utf-8"
    )


def test_snapshot_copies_index_css_image_and_css_url(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="3752077777", title="CaseB")
    _write_steam_page(folder)
    snap = snapshot_from_mod_folder(folder, owner_mod_id=pk)
    assert snap is not None
    dest = _backup_offline(pk)
    _assert_cas_only_offline(
        dest,
        "assets/theme.css",
        "assets/hero.png",
        "assets/font.woff",
    )
    assert "assets/unused.gif" not in _manifest_asset_paths(dest)
    assert not (backup_root(pk) / "payload.bin").exists()
    assert not validate_offline_snapshot(dest)


def test_unrelated_assets_not_copied(tmp_path: Path) -> None:
    src_root = tmp_path / "src"
    src_root.mkdir()
    (src_root / "index.html").write_text("<html>plain</html>", encoding="utf-8")
    extra = src_root / "assets"
    extra.mkdir()
    (extra / "all.css").write_text("body{}", encoding="utf-8")
    dest = tmp_path / "dest"
    snapshot_offline_closure(src_root / "index.html", dest)
    assert (dest / "index.html").is_file()
    assert not (dest / "assets").exists()


def test_missing_required_asset_is_invalid(tmp_path: Path) -> None:
    dest = tmp_path / "offline"
    dest.mkdir()
    (dest / "index.html").write_text(
        '<html><link href="./assets/theme.css" rel="stylesheet"></html>',
        encoding="utf-8",
    )
    issues = validate_offline_snapshot(dest)
    assert any("theme.css" in i for i in issues)
    assert not backup_offline_snapshot_valid(dest)


def test_valid_dependency_closure_is_valid(tmp_path: Path) -> None:
    dest = tmp_path / "offline"
    assets = dest / "assets"
    assets.mkdir(parents=True)
    (assets / "theme.css").write_text("body{color:red}", encoding="utf-8")
    (dest / "index.html").write_text(
        '<html><link href="./assets/theme.css" rel="stylesheet"></html>',
        encoding="utf-8",
    )
    assert backup_offline_snapshot_valid(dest)


def test_existing_file_outside_snapshot_is_a_leak(tmp_path: Path) -> None:
    live = tmp_path / "mod" / ".info" / "assets"
    live.mkdir(parents=True)
    (live / "theme.css").write_text("body{color:red}", encoding="utf-8")
    dest = tmp_path / "backup" / "offline"
    dest.mkdir(parents=True)
    rel = Path(os.path.relpath(live / "theme.css", dest)).as_posix()
    (dest / "index.html").write_text(
        f'<html><link href="{rel}" rel="stylesheet"></html>',
        encoding="utf-8",
    )
    assert not backup_offline_snapshot_valid(dest)


def test_broken_relative_http_css_url_is_not_a_live_leak(tmp_path: Path) -> None:
    dest = tmp_path / "offline"
    assets = dest / "assets"
    assets.mkdir(parents=True)
    (assets / "theme.css").write_text(
        "body{font-family:url(../../Users/henrysnopek/projects/"
        "share-button/http:/fonts.gstatic.com/s/lato/v11/v0SdcGFAl2aezM9Vq_aFTQ.ttf)}",
        encoding="utf-8",
    )
    (dest / "index.html").write_text(
        '<html><link href="./assets/theme.css" rel="stylesheet"></html>',
        encoding="utf-8",
    )
    assert backup_offline_snapshot_valid(dest)


def test_css_url_dangling_font_does_not_invalidate_html_closure(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "offline"
    assets = dest / "assets"
    assets.mkdir(parents=True)
    (assets / "theme.css").write_text(
        "@font-face{src:url(../webfonts/fa-brands-400.woff2)}",
        encoding="utf-8",
    )
    (dest / "index.html").write_text(
        '<html><link href="./assets/theme.css" rel="stylesheet"></html>',
        encoding="utf-8",
    )
    assert backup_offline_snapshot_valid(dest)


def test_source_font_is_required_in_backup_when_present(tmp_path: Path) -> None:
    src = tmp_path / "src"
    assets = src / "assets"
    assets.mkdir(parents=True)
    (assets / "theme.css").write_text(
        "@font-face{src:url(font.woff)}", encoding="utf-8"
    )
    (assets / "font.woff").write_bytes(b"WOFF")
    (src / "index.html").write_text(
        '<html><link href="./assets/theme.css" rel="stylesheet"></html>',
        encoding="utf-8",
    )
    dest = tmp_path / "offline"
    dest.mkdir()
    (dest / "index.html").write_text(
        (src / "index.html").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assets_dest = dest / "assets"
    assets_dest.mkdir()
    (assets_dest / "theme.css").write_text(
        (assets / "theme.css").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert not backup_offline_snapshot_valid(dest, source_index=src / "index.html")
    snapshot_offline_closure(src / "index.html", dest)
    _assert_cas_only_offline(dest, "assets/theme.css", "assets/font.woff")
    assert backup_offline_snapshot_valid(dest, source_index=src / "index.html")


def test_index_exists_alone_is_not_valid_when_css_missing(tmp_path: Path) -> None:
    dest = tmp_path / "offline"
    dest.mkdir()
    (dest / "index.html").write_text(
        '<link href="./assets/theme.css" rel="stylesheet">',
        encoding="utf-8",
    )
    assert (dest / "index.html").is_file()
    assert not backup_offline_snapshot_valid(dest)


def test_metadata_backup_does_not_drop_offline_closure(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981101", title="MetaKeepCss")
    _write_steam_page(folder)
    sync_after_metadata_change(pk, folder, "import", wait=True)
    _assert_cas_only_offline(_backup_offline(pk), "assets/theme.css")
    data = json.loads(
        (folder / INFO_DIR_NAME / "metadata.json").read_text(encoding="utf-8")
    )
    data["title"] = "MetaKeepCss-edited"
    persist_unified_metadata_dict(folder, data, sync_backup=False)
    sync_after_metadata_change(pk, folder, "edit", wait=True)
    _assert_cas_only_offline(_backup_offline(pk), "assets/theme.css")


def test_cover_update_does_not_drop_offline_closure(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981102", title="CoverKeepCss")
    _write_steam_page(folder)
    sync_after_metadata_change(pk, folder, "import", wait=True)
    (folder / INFO_DIR_NAME / "cover.png").write_bytes(TINY_PNG)
    sync_after_metadata_change(pk, folder, "cover_change", wait=True)
    _assert_cas_only_offline(_backup_offline(pk), "assets/theme.css")


def test_startup_rebuild_repairs_missing_snapshot(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981103", title="RebuildCss")
    _write_steam_page(folder)
    dest = backup_root(pk)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(folder / INFO_DIR_NAME / "metadata.json", dest / "metadata.json")
    assert not (_backup_offline(pk) / BACKUP_OFFLINE_INDEX).exists()
    created = rebuild_missing_metadata_backup(tmp_path / "mod")
    assert created >= 1
    _assert_cas_only_offline(_backup_offline(pk), "assets/theme.css")


def test_repeated_rebuild_is_idempotent(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981104", title="RebuildIdem")
    _write_steam_page(folder)
    sync_after_metadata_change(pk, folder, "import", wait=True)
    first = collect_offline_closure(_backup_offline(pk) / BACKUP_OFFLINE_INDEX)
    created = rebuild_missing_metadata_backup(tmp_path / "mod")
    assert created == 0
    second = collect_offline_closure(_backup_offline(pk) / BACKUP_OFFLINE_INDEX)
    assert set(first) == set(second)
    assert backup_offline_snapshot_valid(_backup_offline(pk))


def test_batch_repair_missing_snapshot_and_skips_valid(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    broken_folder, broken_pk = _seed(
        db, tmp_path, workshop_id="981105", title="RepairBroken"
    )
    _write_steam_page(broken_folder)
    dest = backup_root(broken_pk)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        broken_folder / INFO_DIR_NAME / "metadata.json", dest / "metadata.json"
    )
    (dest / BACKUP_OFFLINE_DIR).mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        broken_folder / INFO_DIR_NAME / "index.html",
        dest / BACKUP_OFFLINE_DIR / BACKUP_OFFLINE_INDEX,
    )
    assert not backup_offline_snapshot_valid(_backup_offline(broken_pk))

    good_folder, good_pk = _seed(db, tmp_path, workshop_id="981106", title="RepairGood")
    _write_steam_page(good_folder)
    sync_after_metadata_change(good_pk, good_folder, "import", wait=True)
    before = {
        p.relative_to(_backup_offline(good_pk)).as_posix()
        for p in _backup_offline(good_pk).rglob("*")
        if p.is_file()
    }
    meta_before = (backup_root(good_pk) / "metadata.json").read_text(encoding="utf-8")

    stats = repair_live_offline_backups(library_root=tmp_path / "mod", db=db)
    assert stats["source_exists_backup_invalid"] == 0
    assert stats["backup_repaired"] >= 1
    assert backup_offline_snapshot_valid(_backup_offline(broken_pk))
    after = {
        p.relative_to(_backup_offline(good_pk)).as_posix()
        for p in _backup_offline(good_pk).rglob("*")
        if p.is_file()
    }
    assert after == before
    assert (backup_root(good_pk) / "metadata.json").read_text(encoding="utf-8") == meta_before


def test_case_a_live_index_missing_backup_repaired(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="3748388534", title="SparkLeader")
    _write_steam_page(folder)
    dest = backup_root(pk)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(folder / INFO_DIR_NAME / "metadata.json", dest / "metadata.json")
    assert resolve_offline_page(folder) is not None
    assert not (_backup_offline(pk) / BACKUP_OFFLINE_INDEX).exists()
    stats = repair_live_offline_backups(library_root=tmp_path / "mod", db=db)
    assert stats["backup_repaired"] >= 1
    assert backup_offline_snapshot_valid(_backup_offline(pk))
    assert backup_root(pk).name == pk


def test_case_b_index_without_assets_becomes_usable(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(
        db, tmp_path, workshop_id="3752077777", title="VampireMasquerade"
    )
    _write_steam_page(folder)
    dest = _backup_offline(pk)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(folder / INFO_DIR_NAME / "index.html", dest / BACKUP_OFFLINE_INDEX)
    (backup_root(pk) / "metadata.json").write_text(
        (folder / INFO_DIR_NAME / "metadata.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    assert not backup_offline_snapshot_valid(dest)
    stats = repair_live_offline_backups(library_root=tmp_path / "mod", db=db)
    assert stats["source_exists_backup_invalid"] == 0
    _assert_cas_only_offline(dest, "assets/theme.css", "assets/font.woff")
    html = (dest / BACKUP_OFFLINE_INDEX).read_text(encoding="utf-8")
    assert "./assets/theme.css" in html
    assert "../" not in html or ".info" not in html


def test_source_exists_invalid_backup_is_repair_candidate(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981107", title="RepairCand")
    _write_steam_page(folder)
    dest = _backup_offline(pk)
    dest.mkdir(parents=True)
    (dest / BACKUP_OFFLINE_INDEX).write_text(
        (folder / INFO_DIR_NAME / "index.html").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (backup_root(pk) / "metadata.json").write_text(
        (folder / INFO_DIR_NAME / "metadata.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    assert resolve_offline_page(folder) is not None
    assert not backup_offline_snapshot_valid(dest)
    result = validate_backup(pk)
    assert result["offline_ok"] is False
    stats = repair_live_offline_backups(library_root=tmp_path / "mod", db=db)
    assert stats["backup_repaired"] >= 1
    assert backup_offline_snapshot_valid(dest)


def test_miss_takeover_requires_valid_snapshot(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    folder, pk = _seed(db, tmp_path, workshop_id="981108", title="MissInvalid")
    _write_steam_page(folder)
    dest = _backup_offline(pk)
    dest.mkdir(parents=True)
    (dest / BACKUP_OFFLINE_INDEX).write_text(
        (folder / INFO_DIR_NAME / "index.html").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (backup_root(pk) / "metadata.json").write_text(
        (folder / INFO_DIR_NAME / "metadata.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    shutil.rmtree(folder)
    mark_missing(pk)
    assert entity_state(pk, db=db) == ENTITY_MISS
    assert backup_offline_index(pk) is None

    folder2, pk2 = _seed(db, tmp_path, workshop_id="981109", title="MissValid")
    _write_steam_page(folder2)
    sync_after_metadata_change(pk2, folder2, "import", wait=True)
    shutil.rmtree(folder2)
    mark_missing(pk2)
    found = backup_offline_index(pk2)
    assert found is not None and found.is_file()
    assert backup_offline_snapshot_valid(found.parent)


@pytest.mark.playwright
def test_backup_file_url_loads_stylesheets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    view_root = tmp_path / "offline_view"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: view_root)
    src = tmp_path / "src"
    assets = src / "assets"
    assets.mkdir(parents=True)
    (assets / "theme.css").write_text("body{color:rgb(1,2,3)}", encoding="utf-8")
    (src / "index.html").write_text(
        '<html><head><link rel="stylesheet" href="./assets/theme.css"></head>'
        "<body><div id=\"root\">layout</div></body></html>",
        encoding="utf-8",
    )
    dest = tmp_path / "offline"
    snapshot_offline_closure(src / "index.html", dest)
    _assert_cas_only_offline(dest, "assets/theme.css")
    # OPEN materializes into offline_view; durable Backup stays CAS-only.
    index = ensure_backup_offline_openable(dest)
    assert index is not None and index.is_file()
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    failed: list[int] = []
    try:
        playwright_cm = sync_playwright()
        playwright = playwright_cm.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"playwright driver unavailable: {exc}")
    try:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Playwright Chromium not launchable: {exc}")
        page = browser.new_page()
        page.on(
            "response",
            lambda response: failed.append(response.status)
            if response.status >= 400
            else None,
        )
        page.goto(index.resolve().as_uri())
        sheets = page.evaluate("() => document.styleSheets.length")
        color = page.evaluate("() => getComputedStyle(document.body).color")
        text = page.evaluate("() => document.body.innerText")
        browser.close()
    finally:
        playwright.stop()
    assert not failed
    assert int(sheets) >= 1
    assert "layout" in str(text)
    assert "rgb(1, 2, 3)" in str(color)
    assert not (dest / "assets").exists()
