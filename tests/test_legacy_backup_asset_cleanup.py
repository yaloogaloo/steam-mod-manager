"""Phase 4: Legacy Backup offline/assets cleanup tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.asset_manifest import MANIFEST_FILENAME, AssetManifest, AssetReference
from services.asset_store import AssetStore, sha256_bytes, sha256_file
from services.backup_asset_migration import (
    repair_info_assets_from_backup_store,
    restore_backup_assets_from_store,
    sync_backup_offline_manifest,
)
from services.info_asset_migration import migrate_info_assets
from tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup import (
    CleanupReason,
    audit_mod_legacy_backup_assets,
    cleanup_mod_legacy_backup_assets,
    classify_backup_asset_file,
)
from services.offline.backup_closure import (
    snapshot_offline_closure,
    usable_backup_offline_index,
)
from services.offline.paths import resolve_offline_page

TINY_A = b"phase4-legacy-aaa"
TINY_B = b"phase4-legacy-bbb"
TINY_C = b"phase4-legacy-ccc"


def _steam_mod(folder: Path, assets: dict[str, bytes]) -> Path:
    info = folder / ".info"
    ad = info / "assets"
    ad.mkdir(parents=True)
    for name, data in assets.items():
        (ad / name).write_bytes(data)
    imgs = "\n".join(f'<img src="./assets/{n}">' for n in assets)
    (info / "index.html").write_text(
        f"<html><body>{imgs}</body></html>", encoding="utf-8"
    )
    return folder


def _prep_backup(
    tmp_path: Path, assets: dict[str, bytes], *, mod_id: str = "1"
) -> tuple[Path, Path, AssetStore]:
    """LIVE .info + Backup offline with Phase 2/3 manifests under isolated roots.

    Materializes Backup offline/assets after CAS-only snapshot so Phase 4
    cleanup tests still exercise legacy physical dual-write debt.
    """
    store = AssetStore(root=tmp_path / "store")
    mod = _steam_mod(tmp_path / f"mod_{mod_id}", assets)
    assert migrate_info_assets(mod, store=store, mod_id=mod_id).ok
    dest = tmp_path / "backup" / mod_id / "offline"
    assert snapshot_offline_closure(mod / ".info" / "index.html", dest)
    r = sync_backup_offline_manifest(
        dest,
        source_index=mod / ".info" / "index.html",
        store=store,
        mod_id=mod_id,
    )
    # Phase 5 snapshot clears assets; restore for cleanup SAFE-gate tests
    from services.backup_asset_migration import restore_backup_assets_from_store

    assert restore_backup_assets_from_store(dest, store=store).ok
    assert (dest / "assets").is_dir()
    return mod, dest, store


def test_safe_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod, dest, store = _prep_backup(tmp_path, {"a.png": TINY_A})

    def fake_root(mid: str | int) -> Path:
        return tmp_path / "backup" / str(mid)

    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup.backup_root", fake_root
    )
    audit = audit_mod_legacy_backup_assets("1", store=store)
    assert audit.asset_files == 1
    assert audit.verdicts[0].reason == CleanupReason.SAFE


def test_hash_mismatch_not_safe(tmp_path: Path) -> None:
    _, dest, store = _prep_backup(tmp_path, {"a.png": TINY_A})
    path = dest / "assets" / "a.png"
    path.write_bytes(TINY_B)
    man = AssetManifest.from_path(dest / MANIFEST_FILENAME)
    by_path = {a.path: a for a in man.assets}
    v = classify_backup_asset_file(
        mod_id="1",
        dest_offline=dest,
        file_path=path,
        manifest=man,
        manifest_error="",
        store=store,
        by_path=by_path,
    )
    assert v.reason == CleanupReason.MANIFEST_MISMATCH
    assert v.verification == "unsafe"


def test_size_mismatch_not_safe(tmp_path: Path) -> None:
    _, dest, store = _prep_backup(tmp_path, {"a.png": TINY_A})
    man = AssetManifest.from_path(dest / MANIFEST_FILENAME)
    # Corrupt size in-memory without rewriting file
    bad = AssetManifest(
        assets=[
            AssetReference(
                path="assets/a.png",
                sha256=man.assets[0].sha256,
                size=man.assets[0].size + 99,
            )
        ]
    )
    path = dest / "assets" / "a.png"
    v = classify_backup_asset_file(
        mod_id="1",
        dest_offline=dest,
        file_path=path,
        manifest=bad,
        manifest_error="",
        store=store,
        by_path={a.path: a for a in bad.assets},
    )
    assert v.reason == CleanupReason.MANIFEST_MISMATCH


def test_missing_cas_not_safe(tmp_path: Path) -> None:
    _, dest, store = _prep_backup(tmp_path, {"a.png": TINY_A})
    digest = sha256_bytes(TINY_A)
    store.delete(digest)
    man = AssetManifest.from_path(dest / MANIFEST_FILENAME)
    path = dest / "assets" / "a.png"
    v = classify_backup_asset_file(
        mod_id="1",
        dest_offline=dest,
        file_path=path,
        manifest=man,
        manifest_error="",
        store=store,
        by_path={a.path: a for a in man.assets},
    )
    assert v.reason == CleanupReason.MISSING_CAS


def test_corrupt_cas_not_safe(tmp_path: Path) -> None:
    _, dest, store = _prep_backup(tmp_path, {"a.png": TINY_A})
    digest = sha256_bytes(TINY_A)
    cas = store.get_path(digest)
    cas.write_bytes(b"CORRUPTED-CAS-BYTES!!!!!")
    man = AssetManifest.from_path(dest / MANIFEST_FILENAME)
    path = dest / "assets" / "a.png"
    v = classify_backup_asset_file(
        mod_id="1",
        dest_offline=dest,
        file_path=path,
        manifest=man,
        manifest_error="",
        store=store,
        by_path={a.path: a for a in man.assets},
    )
    assert v.reason == CleanupReason.CORRUPT_CAS


def test_missing_manifest_not_safe(tmp_path: Path) -> None:
    _, dest, store = _prep_backup(tmp_path, {"a.png": TINY_A})
    (dest / MANIFEST_FILENAME).unlink()
    path = dest / "assets" / "a.png"
    v = classify_backup_asset_file(
        mod_id="1",
        dest_offline=dest,
        file_path=path,
        manifest=None,
        manifest_error="missing",
        store=store,
        by_path=None,
    )
    assert v.reason == CleanupReason.MISSING_MANIFEST


def test_unreferenced_file_unknown_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dest, store = _prep_backup(tmp_path, {"a.png": TINY_A})
    extra = dest / "assets" / "debug.bin"
    extra.write_bytes(b"untracked-debug")
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    audit = audit_mod_legacy_backup_assets("1", store=store)
    reasons = {v.relative_path: v.reason for v in audit.verdicts}
    assert reasons["assets/a.png"] == CleanupReason.SAFE
    assert reasons["assets/debug.bin"] == CleanupReason.UNREFERENCED

    result = cleanup_mod_legacy_backup_assets(
        "1", store=store, dry_run=False, verify_restore_after=True
    )
    assert result.ok
    assert result.deleted == 1
    assert extra.is_file()  # unknown retained
    assert not (dest / "assets" / "a.png").is_file()


def test_path_traversal_not_safe(tmp_path: Path) -> None:
    _, dest, store = _prep_backup(tmp_path, {"a.png": TINY_A})
    # Simulate classify with an escaped relative path via a file outside assets
    evil = dest / "escape.png"
    evil.write_bytes(TINY_A)
    man = AssetManifest.from_path(dest / MANIFEST_FILENAME)
    # File not under assets/ → relative_manifest_path fails → PATH_UNSAFE
    v = classify_backup_asset_file(
        mod_id="1",
        dest_offline=dest,
        file_path=evil,
        manifest=man,
        manifest_error="",
        store=store,
        by_path={a.path: a for a in man.assets},
    )
    assert v.reason == CleanupReason.PATH_UNSAFE


def test_delete_then_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, dest, store = _prep_backup(
        tmp_path, {"a.png": TINY_A, "b.png": TINY_B}
    )
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    before = sha256_file(dest / "assets" / "a.png")
    result = cleanup_mod_legacy_backup_assets(
        "1", store=store, dry_run=False, verify_restore_after=True
    )
    assert result.ok
    assert result.deleted == 2
    assert result.restore_ok is True
    assert not (dest / "assets").exists() or not any(
        (dest / "assets").rglob("*")
    )

    restore = restore_backup_assets_from_store(dest, store=store)
    assert restore.ok
    assert restore.written == 2
    assert sha256_file(dest / "assets" / "a.png") == before
    assert usable_backup_offline_index(dest) is not None


def test_miss_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MISS contract: entity/backup/manifest/CAS retained; assets recoverable."""
    mod, dest, store = _prep_backup(tmp_path, {"a.png": TINY_A})
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    # Simulate LIVE source assets missing (MISS-like)
    for p in (mod / ".info" / "assets").iterdir():
        p.unlink()
    (mod / ".info" / "assets").rmdir()

    result = cleanup_mod_legacy_backup_assets(
        "1", store=store, dry_run=False, verify_restore_after=True
    )
    assert result.ok
    assert (dest / MANIFEST_FILENAME).is_file()
    assert (dest / "index.html").is_file()
    assert store.has(sha256_bytes(TINY_A))

    restore = restore_backup_assets_from_store(dest, store=store)
    assert restore.ok
    assert (dest / "assets" / "a.png").is_file()


def test_repair_from_store_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    from core.cas_runtime import reset_cas_runtime_cache

    reset_cas_runtime_cache()
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: tmp_path / "ov")
    mod, dest, store = _prep_backup(tmp_path, {"a.png": TINY_A})
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    assert cleanup_mod_legacy_backup_assets(
        "1", store=store, dry_run=False
    ).ok
    # Wipe LIVE assets
    for p in (mod / ".info" / "assets").rglob("*"):
        if p.is_file():
            p.unlink()
    repair = repair_info_assets_from_backup_store(mod, mod_id="1", store=store)
    assert repair.ok
    # Phase 6: Repair verifies CAS; does not recreate durable .info/assets
    assert not (mod / ".info" / "assets").exists() or not any(
        (mod / ".info" / "assets").rglob("*")
    )
    from services.info_asset_runtime import ensure_live_offline_openable

    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is not None
    assert sha256_file(opened.parent / "assets" / "a.png") == sha256_bytes(TINY_A)


def test_offline_open_live_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod, dest, store = _prep_backup(tmp_path, {"a.png": TINY_A})
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    assert cleanup_mod_legacy_backup_assets(
        "1", store=store, dry_run=False
    ).ok
    # Phase 4 keeps .info/assets — Offline OPEN uses LIVE
    page = resolve_offline_page(mod)
    assert page is not None
    assert (mod / ".info" / "assets" / "a.png").is_file()
    assert page.is_file()


def test_idempotency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, dest, store = _prep_backup(tmp_path, {"a.png": TINY_A})
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    first = cleanup_mod_legacy_backup_assets("1", store=store, dry_run=False)
    assert first.ok and first.deleted == 1
    second = cleanup_mod_legacy_backup_assets("1", store=store, dry_run=False)
    assert second.ok
    assert second.deleted == 0
    assert second.skipped or second.reason.startswith("no legacy")


def test_cross_mod_shared_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssetStore(root=tmp_path / "store")
    a = _steam_mod(tmp_path / "A", {"x.png": TINY_A})
    b = _steam_mod(tmp_path / "B", {"y.png": TINY_A})
    migrate_info_assets(a, store=store)
    migrate_info_assets(b, store=store)
    da = tmp_path / "backup" / "10" / "offline"
    db = tmp_path / "backup" / "20" / "offline"
    snapshot_offline_closure(a / ".info" / "index.html", da)
    snapshot_offline_closure(b / ".info" / "index.html", db)
    from services.backup_asset_migration import restore_backup_assets_from_store

    assert restore_backup_assets_from_store(da, store=store).ok
    assert restore_backup_assets_from_store(db, store=store).ok
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    digest = sha256_bytes(TINY_A)
    assert cleanup_mod_legacy_backup_assets("10", store=store, dry_run=False).ok
    assert store.has(digest)
    assert (db / "assets" / "y.png").is_file()
    assert sha256_file(db / "assets" / "y.png") == digest
    # Mod B still SAFE / restorable
    restore = restore_backup_assets_from_store(db, store=store)
    assert restore.ok or (db / "assets" / "y.png").is_file()
