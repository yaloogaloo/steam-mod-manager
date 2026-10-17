"""Phase 8: Legacy .info manifest reconstruction tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.cas_runtime import reset_cas_runtime_cache
from services.asset_manifest import MANIFEST_FILENAME, AssetManifest
from services.asset_store import AssetStore, sha256_bytes, sha256_file
from services.info_asset_runtime import ensure_live_offline_openable
from tools.archive.legacy_asset_tools.legacy_info_manifest_rebuild import (
    RebuildClass,
    audit_mod_manifest_rebuild,
    load_rebuild_checkpoint,
    rebuild_all_info_manifests,
    rebuild_mod_info_manifest,
    save_rebuild_checkpoint,
)


@pytest.fixture(autouse=True)
def _cas_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    reset_cas_runtime_cache()
    yield
    reset_cas_runtime_cache()


TINY_A = b"phase8-manifest-aaa"
TINY_B = b"phase8-manifest-bbb"


def _steam_mod(folder: Path, assets: dict[str, bytes], *, with_manifest: bool = False) -> Path:
    info = folder / ".info"
    ad = info / "assets"
    ad.mkdir(parents=True)
    for name, data in assets.items():
        (ad / name).write_bytes(data)
    imgs = "\n".join(f'<img src="./assets/{n}">' for n in assets)
    (info / "index.html").write_text(
        f"<html><body>{imgs}</body></html>", encoding="utf-8"
    )
    (info / "metadata.json").write_text("{}", encoding="utf-8")
    if with_manifest:
        from services.info_asset_migration import migrate_info_assets

        migrate_info_assets(folder, store=AssetStore(root=folder / "_s"))
    return folder


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AssetStore:
    root = tmp_path / "store"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: tmp_path / "ov")
    return AssetStore(root=root)


def test_rebuild_single_mod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A, "b.png": TINY_B})
    assert not (mod / ".info" / MANIFEST_FILENAME).exists()
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_manifest_rebuild.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    audit = audit_mod_manifest_rebuild("8", managed_path=mod)
    assert audit.classification == RebuildClass.CAN_MIGRATE
    result = rebuild_mod_info_manifest(
        "8", store=store, dry_run=False, verify_runtime=True, managed_path=mod
    )
    assert result.ok
    assert (mod / ".info" / MANIFEST_FILENAME).is_file()
    assert (mod / ".info" / "assets" / "a.png").is_file()  # NOT deleted
    man = AssetManifest.from_path(mod / ".info" / MANIFEST_FILENAME)
    assert len(man.assets) == 2
    for ref in man.assets:
        assert store.verify(ref.sha256)
    assert result.open_ok and result.repair_ok
    assert result.info_assets_untouched


def test_reuse_existing_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    # Pre-seed CAS
    obj = store.put_bytes(TINY_A)
    assert obj.created
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_manifest_rebuild.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    result = rebuild_mod_info_manifest(
        "1", store=store, dry_run=False, verify_runtime=False, managed_path=mod
    )
    assert result.ok
    assert result.reused_objects >= 1
    assert result.created_objects == 0


def test_create_new_cas_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_manifest_rebuild.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    assert not store.has(sha256_bytes(TINY_A))
    result = rebuild_mod_info_manifest(
        "2", store=store, dry_run=False, verify_runtime=False, managed_path=mod
    )
    assert result.ok
    assert result.created_objects == 1
    assert store.has(sha256_bytes(TINY_A))


def test_hash_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_manifest_rebuild.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    result = rebuild_mod_info_manifest(
        "3", store=store, dry_run=False, verify_runtime=False, managed_path=mod
    )
    assert result.ok
    man = AssetManifest.from_path(mod / ".info" / MANIFEST_FILENAME)
    assert man.assets[0].sha256 == sha256_file(mod / ".info" / "assets" / "a.png")


def test_invalid_asset_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    (mod / ".info" / "assets" / "evil.tmp").write_bytes(b"tmp")
    audit = audit_mod_manifest_rebuild("4", managed_path=mod)
    assert audit.classification == RebuildClass.UNKNOWN_FILE
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_manifest_rebuild.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    result = rebuild_mod_info_manifest(
        "4", store=store, dry_run=False, managed_path=mod
    )
    assert result.skipped
    assert not (mod / ".info" / MANIFEST_FILENAME).exists()


def test_rollback_on_verify_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_manifest_rebuild.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )

    def boom(*_a, **_k):
        raise RuntimeError("forced open fail")

    monkeypatch.setattr(
        "services.info_asset_runtime.ensure_live_offline_openable", boom
    )
    result = rebuild_mod_info_manifest(
        "5", store=store, dry_run=False, verify_runtime=True, managed_path=mod
    )
    assert not result.ok
    assert result.rolled_back
    assert not (mod / ".info" / MANIFEST_FILENAME).exists()
    assert (mod / ".info" / "assets" / "a.png").is_file()


def test_checkpoint_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mods = {}
    for mid, name in (("10", "A"), ("11", "B")):
        folder = _steam_mod(tmp_path / name, {"a.png": TINY_A + mid.encode()})
        mods[mid] = folder

    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_manifest_rebuild.resolve_mod_managed_path",
        lambda mid, db_path=None: mods.get(str(mid)),
    )
    cp = tmp_path / "cp.json"
    save_rebuild_checkpoint(
        {
            "schema_version": 1,
            "tool": "legacy_info_manifest_rebuild",
            "migrated_mod_ids": ["10"],
            "skipped_mod_ids": [],
            "failed_mod_ids": [],
            "files": 1,
            "bytes": 10,
            "new_objects": 1,
            "reused_objects": 0,
            "last_processed_mod_id": "10",
        },
        cp,
    )
    batch = rebuild_all_info_manifests(
        store=store,
        dry_run=False,
        resume=True,
        checkpoint_path=cp,
        mod_ids=["10", "11"],
        verify_runtime=False,
    )
    assert batch.ok
    assert not (mods["10"] / ".info" / MANIFEST_FILENAME).exists()  # skipped
    assert (mods["11"] / ".info" / MANIFEST_FILENAME).is_file()
    loaded = load_rebuild_checkpoint(cp)
    assert "11" in loaded["migrated_mod_ids"]


def test_open_and_miss_repair_after_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_manifest_rebuild.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    result = rebuild_mod_info_manifest(
        "6", store=store, dry_run=False, verify_runtime=True, managed_path=mod
    )
    assert result.ok and result.open_ok and result.miss_ok
    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is not None
    assert (opened.parent / "assets" / "a.png").is_file()
    # assets still on disk (Phase 8 does not delete)
    assert (mod / ".info" / "assets" / "a.png").is_file()


def test_idempotent_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_manifest_rebuild.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    first = rebuild_mod_info_manifest(
        "7", store=store, dry_run=False, verify_runtime=False, managed_path=mod
    )
    assert first.ok
    second = rebuild_mod_info_manifest(
        "7", store=store, dry_run=False, verify_runtime=False, managed_path=mod
    )
    assert second.ok and second.skipped
    assert second.reason == "already has manifest"
