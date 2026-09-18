"""Zero leftover durable asset trees (LIVE ``.info/assets`` / Backup ``offline/assets``)."""

from __future__ import annotations

from pathlib import Path

import pytest

import core.paths as paths
from core.cas_runtime import reset_cas_runtime_cache
from services.asset_store import AssetStore
from services.info_asset_runtime import (
    ensure_live_offline_openable,
    finalize_live_offline_to_cas,
    repair_live_from_cas,
)


def _info_asset_dirs(root: Path) -> list[Path]:
    found: list[Path] = []
    if not root.exists():
        return found
    for path in root.rglob("*"):
        if not path.is_dir() or path.name != "assets":
            continue
        if ".info" not in path.parts:
            continue
        idx = path.parts.index(".info")
        rest = path.parts[idx + 1 :]
        if rest in (("assets",), ("offline", "assets")):
            found.append(path)
    return found


def _backup_asset_dirs(root: Path) -> list[Path]:
    found: list[Path] = []
    if not root.exists():
        return found
    for path in root.rglob("*"):
        if path.is_dir() and path.name == "assets":
            parts = path.parts
            if len(parts) >= 2 and parts[-2] == "offline":
                found.append(path)
    return found


@pytest.fixture(autouse=True)
def _cas_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    reset_cas_runtime_cache()
    yield
    reset_cas_runtime_cache()


def test_production_live_info_assets_empty() -> None:
    leftover = _info_asset_dirs(paths.default_mod_library())
    assert leftover == [], leftover


def test_production_backup_offline_assets_empty() -> None:
    leftover = _backup_asset_dirs(paths.data_dir() / "mod_backup")
    assert leftover == [], leftover


def test_finalize_open_repair_leave_zero_info_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssetStore(root=tmp_path / "store")
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store.root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: tmp_path / "ov")
    monkeypatch.setattr("core.paths.cache_temp_dir", lambda: tmp_path / "tmp")

    mod = tmp_path / "Mod"
    info = mod / ".info"
    assets = info / "assets"
    assets.mkdir(parents=True)
    (assets / "a.png").write_bytes(b"phase17-zero-aaa")
    (info / "index.html").write_text(
        '<html><body><img src="./assets/a.png"></body></html>', encoding="utf-8"
    )
    (info / "internal_id").write_text("9009", encoding="utf-8")
    assert finalize_live_offline_to_cas(mod, store=store, mod_id="9009").ok
    assert not assets.exists(), "finalize must drop durable .info/assets"

    opened = ensure_live_offline_openable(mod, store=store, mod_id="9009")
    assert opened is not None
    assert opened.is_file()
    assert not (mod / ".info" / "assets").exists(), "OPEN must not recreate .info/assets"

    repair = repair_live_from_cas(mod, mod_id="9009", store=store)
    assert repair.ok
    assert getattr(repair, "written", 0) == 0
    assert not (mod / ".info" / "assets").exists(), "repair must not recreate .info/assets"
