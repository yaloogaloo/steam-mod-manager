"""Phase 10: cache/ is regenerable; data/ has no cache-type directories."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

import core.paths as paths
from core.cas_runtime import reset_cas_runtime_cache
from core.paths import CACHE_SUBDIR_NAMES, migrate_legacy_data_caches, project_root
from services.asset_store import AssetStore
from services.info_asset_runtime import (
    ensure_live_offline_openable,
    finalize_live_offline_to_cas,
)

ROOT = project_root()
DATA = ROOT / "data"
SERVICES = ROOT / "services"
UI = ROOT / "ui"
STARTUP_FILES = (
    ROOT / "main.py",
    ROOT / "ui" / "startup_lifecycle.py",
)

FORBIDDEN_DATA_CACHE_DIRS = frozenset(CACHE_SUBDIR_NAMES)
TINY = b"phase10-cache-sot-aaa"


@pytest.fixture(autouse=True)
def _cas_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    reset_cas_runtime_cache()
    yield
    reset_cas_runtime_cache()


def test_docs_and_audit_artifacts_exist() -> None:
    assert (ROOT / "docs" / "cache_layout.md").is_file()
    assert (ROOT / "_tmp" / "phase10_cache_migration_audit.json").is_file()
    assert (ROOT / "_tmp" / "phase10_cache_migration.json").is_file()


def test_cache_helpers_live_under_get_cache_dir_not_data() -> None:
    cache = paths.get_cache_dir().resolve()
    data = paths.data_dir().resolve()
    helpers = (
        paths.offline_view_cache_dir(),
        paths.asset_cache_dir(),
        paths.import_cache_dir(),
        paths.headers_cache_dir(),
        paths.cache_temp_dir(),
    )
    for path in helpers:
        resolved = path.resolve()
        assert resolved.parent == cache
        try:
            resolved.relative_to(data)
            raise AssertionError(f"{path.name} resolved under data/: {resolved}")
        except ValueError:
            pass
    assert paths.asset_store_dir().resolve().parent == data
    assert paths.database_path().resolve().parent == data


def test_production_data_has_no_cache_type_directories() -> None:
    assert DATA.is_dir()
    present = {child.name for child in DATA.iterdir()}
    leftover = sorted(present & FORBIDDEN_DATA_CACHE_DIRS)
    assert leftover == [], f"data/ still has cache-type dirs: {leftover}"
    assert ".asset_cache_prune_stamp" not in present


def test_legacy_data_cache_helpers_do_not_create() -> None:
    isolated = paths.data_dir()
    before = {p.name for p in isolated.iterdir()} if isolated.is_dir() else set()
    assert not paths.legacy_data_asset_cache_dir().exists()
    assert not paths.legacy_data_import_cache_dir().exists()
    assert not paths.legacy_data_headers_dir().exists()
    after = {p.name for p in isolated.iterdir()} if isolated.is_dir() else set()
    created = (after - before) & FORBIDDEN_DATA_CACHE_DIRS
    assert not created


def test_migrate_is_directory_level_and_skips_business(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    cache_root = tmp_path / "cache"
    (data_root / "asset_cache").mkdir(parents=True)
    (data_root / "asset_cache" / "aa.bin").write_bytes(b"url-cache")
    (data_root / "import_cache" / "job").mkdir(parents=True)
    (data_root / "import_cache" / "job" / "x.txt").write_text("x", encoding="utf-8")
    (data_root / "headers").mkdir()
    (data_root / "headers" / "1.jpg").write_bytes(b"jpg")
    (data_root / ".asset_cache_prune_stamp").write_text("1", encoding="utf-8")
    store = data_root / "asset_store" / "sha256"
    store.mkdir(parents=True)
    (store / "obj").write_bytes(b"cas")
    backup = data_root / "mod_backup" / "1"
    backup.mkdir(parents=True)
    (backup / "keep.txt").write_text("b", encoding="utf-8")
    db = data_root / "mod_manager.db"
    db.write_bytes(b"sqlite")

    result = migrate_legacy_data_caches(data_root=data_root, cache_root=cache_root)
    assert result["dirs"]["asset_cache"]["status"] == "moved"
    assert result["dirs"]["import_cache"]["status"] == "moved"
    assert result["dirs"]["headers"]["status"] == "moved"
    assert result["stamp"]["status"] == "moved"

    assert not (data_root / "asset_cache").exists()
    assert not (data_root / "import_cache").exists()
    assert not (data_root / "headers").exists()
    assert (cache_root / "asset_cache" / "aa.bin").read_bytes() == b"url-cache"
    assert (cache_root / "import_cache" / "job" / "x.txt").read_text(
        encoding="utf-8"
    ) == "x"
    assert (cache_root / "headers" / "1.jpg").read_bytes() == b"jpg"
    assert (cache_root / "asset_cache" / ".asset_cache_prune_stamp").read_text(
        encoding="utf-8"
    ) == "1"
    assert (store / "obj").read_bytes() == b"cas"
    assert (backup / "keep.txt").read_text(encoding="utf-8") == "b"
    assert db.read_bytes() == b"sqlite"


def test_wipe_cache_library_open_store_and_db_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    data = tmp_path / "data"
    store_root = data / "asset_store"
    db_file = data / "mod_manager.db"
    data.mkdir()
    db_file.write_bytes(b"sqlite-header")
    monkeypatch.setattr(
        paths,
        "get_cache_dir",
        lambda: cache.mkdir(parents=True, exist_ok=True) or cache,
    )
    monkeypatch.setattr(paths, "asset_store_dir", lambda: store_root)
    monkeypatch.setattr(
        paths,
        "offline_view_cache_dir",
        lambda: (cache / "offline_view").mkdir(parents=True, exist_ok=True)
        or (cache / "offline_view"),
    )

    store = AssetStore(root=store_root)
    mod = tmp_path / "Mod"
    info = mod / ".info"
    assets = info / "assets"
    assets.mkdir(parents=True)
    (assets / "a.png").write_bytes(TINY)
    (info / "index.html").write_text(
        '<html><body><img src="./assets/a.png"></body></html>',
        encoding="utf-8",
    )
    (info / "internal_id").write_text("1010", encoding="utf-8")
    assert finalize_live_offline_to_cas(mod, store=store, mod_id="1010").ok
    assert not assets.exists()

    opened = ensure_live_offline_openable(mod, store=store, mod_id="1010")
    assert opened is not None
    store_before = sorted(p.name for p in store_root.rglob("*") if p.is_file())
    db_before = db_file.read_bytes()

    from core.db_manager import DatabaseManager

    count_before = DatabaseManager.instance().count_mods_and_type_bindings()[0]

    shutil.rmtree(cache)
    assert not cache.exists()

    # Helpers recreate empty cache/; Library/DB and Asset Store stay put.
    assert paths.get_cache_dir() == cache
    assert cache.is_dir()
    assert db_file.read_bytes() == db_before
    count_after = DatabaseManager.instance().count_mods_and_type_bindings()[0]
    assert count_after == count_before
    store_after = sorted(p.name for p in store_root.rglob("*") if p.is_file())
    assert store_after == store_before

    rebuilt = ensure_live_offline_openable(mod, store=store, mod_id="1010")
    assert rebuilt is not None
    assert (rebuilt.parent / "assets" / "a.png").is_file()
    assert not (mod / ".info" / "assets").exists()


def test_startup_modules_do_not_scan_cache() -> None:
    needles = (
        "get_cache_dir",
        "asset_cache_dir",
        "import_cache_dir",
        "headers_cache_dir",
        "offline_view_cache_dir",
        "os.walk",
        ".rglob(",
    )
    for path in STARTUP_FILES:
        text = path.read_text(encoding="utf-8")
        hits = [n for n in needles if n in text]
        assert hits == [], f"{path.name} must not scan cache at startup: {hits}"


def test_business_code_does_not_hardcode_data_cache_paths() -> None:
    forbidden = (
        'data_dir() / "asset_cache"',
        "data_dir() / 'asset_cache'",
        'data_dir() / "import_cache"',
        "data_dir() / 'import_cache'",
        'data_dir() / "headers"',
        "data_dir() / 'headers'",
        '"data/asset_cache"',
        '"data/import_cache"',
        '"data/headers"',
    )
    offenders: list[str] = []
    for base in (SERVICES, UI):
        for path in base.rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if "__pycache__" in rel:
                continue
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{rel}: {needle}")
    assert offenders == []


def test_core_paths_is_the_authority_for_cache_layout() -> None:
    src = ast.parse((ROOT / "core" / "paths.py").read_text(encoding="utf-8"))
    names = {node.name for node in src.body if isinstance(node, ast.FunctionDef)}
    for required in (
        "get_cache_dir",
        "asset_cache_dir",
        "import_cache_dir",
        "headers_cache_dir",
        "offline_view_cache_dir",
        "cache_temp_dir",
        "migrate_legacy_data_caches",
    ):
        assert required in names
