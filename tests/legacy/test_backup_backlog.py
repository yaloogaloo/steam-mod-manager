"""Phase 4.1: manifest debt, audit modes, checkpoint."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from services.asset_manifest import MANIFEST_FILENAME
from services.asset_store import AssetStore, sha256_bytes
from services.backup_asset_migration import sync_backup_offline_manifest
from tools.archive.legacy_asset_tools.backup_manifest_debt import (
    ManifestDebtClass,
    classify_manifest_debt_for_mod,
    migrate_manifest_debt,
)
from services.info_asset_migration import migrate_info_assets
from tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup import (
    AuditMode,
    CleanupReason,
    audit_mod_legacy_backup_assets,
    classify_backup_asset_file,
    cleanup_all_legacy_backup_assets,
    cleanup_mod_legacy_backup_assets,
    load_cleanup_checkpoint,
    save_cleanup_checkpoint,
)
from services.offline.backup_closure import snapshot_offline_closure

TINY = b"phase41-debt-aaa"


def _mod(folder: Path, assets: dict[str, bytes]) -> Path:
    info = folder / ".info"
    ad = info / "assets"
    ad.mkdir(parents=True)
    for n, d in assets.items():
        (ad / n).write_bytes(d)
    imgs = "\n".join(f'<img src="./assets/{n}">' for n in assets)
    (info / "index.html").write_text(f"<html><body>{imgs}</body></html>", encoding="utf-8")
    return folder


def test_missing_manifest_classification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = AssetStore(root=tmp_path / "store")
    mod = _mod(tmp_path / "M", {"a.png": TINY})
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "backup" / "7" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    from services.backup_asset_migration import restore_backup_assets_from_store

    assert restore_backup_assets_from_store(dest, store=store).ok
    # Ensure debt state: strip Backup manifest while keeping legacy assets
    man = dest / MANIFEST_FILENAME
    if man.is_file():
        man.unlink()
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.backup_manifest_debt.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.backup_manifest_debt.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    item = classify_manifest_debt_for_mod("7")
    assert item.classification == ManifestDebtClass.CAN_MIGRATE_FROM_INFO
    assert item.asset_files == 1


def test_migrate_from_info_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = AssetStore(root=tmp_path / "store")
    mod = _mod(tmp_path / "M", {"a.png": TINY})
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "backup" / "8" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    from services.backup_asset_migration import restore_backup_assets_from_store

    assert restore_backup_assets_from_store(dest, store=store).ok
    man = dest / MANIFEST_FILENAME
    if man.is_file():
        man.unlink()
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.backup_manifest_debt.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    monkeypatch.setattr(
        "services.backup_asset_migration.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.backup_manifest_debt.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    monkeypatch.setattr(
        "services.backup_asset_migration.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    batch = migrate_manifest_debt(
        store=store, dry_run=False, mod_ids=["8"], only_migratable=True
    )
    assert batch.ok
    assert batch.mods_succeeded >= 1
    assert (dest / MANIFEST_FILENAME).is_file()
    # Idempotent
    batch2 = migrate_manifest_debt(
        store=store, dry_run=False, mod_ids=["8"], only_migratable=True
    )
    assert batch2.ok


def test_fast_audit_does_not_materialize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = AssetStore(root=tmp_path / "store")
    mod = _mod(tmp_path / "M", {"a.png": TINY})
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "backup" / "9" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    from services.backup_asset_migration import restore_backup_assets_from_store

    assert restore_backup_assets_from_store(dest, store=store).ok
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    with mock.patch(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup._materialize_matches"
    ) as mat:
        audit = audit_mod_legacy_backup_assets("9", store=store, mode=AuditMode.FAST)
        assert audit.verdicts[0].reason == CleanupReason.SAFE
        mat.assert_not_called()


def test_deep_audit_materializes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = AssetStore(root=tmp_path / "store")
    mod = _mod(tmp_path / "M", {"a.png": TINY})
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "backup" / "11" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    from services.backup_asset_migration import restore_backup_assets_from_store

    assert restore_backup_assets_from_store(dest, store=store).ok
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    with mock.patch(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup._materialize_matches",
        return_value=(True, ""),
    ) as mat:
        audit = audit_mod_legacy_backup_assets("11", store=store, mode=AuditMode.DEEP)
        assert audit.verdicts[0].reason == CleanupReason.SAFE
        mat.assert_called()


def test_standard_cleanup_no_materialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssetStore(root=tmp_path / "store")
    mod = _mod(tmp_path / "M", {"a.png": TINY})
    migrate_info_assets(mod, store=store)
    dest = tmp_path / "backup" / "12" / "offline"
    snapshot_offline_closure(mod / ".info" / "index.html", dest)
    from services.backup_asset_migration import restore_backup_assets_from_store

    assert restore_backup_assets_from_store(dest, store=store).ok
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    with mock.patch(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup._materialize_matches"
    ) as mat:
        r = cleanup_mod_legacy_backup_assets(
            "12", store=store, dry_run=False, verify_restore_after=False
        )
        assert r.ok and r.deleted == 1
        mat.assert_not_called()


def test_checkpoint_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = AssetStore(root=tmp_path / "store")
    ckpt = tmp_path / "_tmp" / "ckpt.json"
    from services.backup_asset_migration import restore_backup_assets_from_store

    for mid, name in (("21", "a.png"), ("22", "b.png")):
        mod = _mod(tmp_path / f"M{mid}", {name: TINY})
        migrate_info_assets(mod, store=store)
        dest = tmp_path / "backup" / mid / "offline"
        snapshot_offline_closure(mod / ".info" / "index.html", dest)
        assert restore_backup_assets_from_store(dest, store=store).ok
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup._list_mod_ids",
        lambda **kw: ["21", "22"],
    )
    first = cleanup_all_legacy_backup_assets(
        store=store,
        dry_run=False,
        mod_ids=["21", "22"],
        verify_restore_after=False,
        checkpoint_path=ckpt,
        resume=False,
    )
    assert first.files_deleted == 2
    data = load_cleanup_checkpoint(ckpt)
    assert "21" in data["cleaned_mod_ids"] and "22" in data["cleaned_mod_ids"]
    second = cleanup_all_legacy_backup_assets(
        store=store,
        dry_run=False,
        mod_ids=["21", "22"],
        verify_restore_after=False,
        checkpoint_path=ckpt,
        resume=True,
    )
    # Already cleaned — skipped without re-delete
    assert second.files_deleted == 2  # carried from checkpoint totals
    assert second.mods_skipped >= 2


def test_corrupt_checkpoint_safe(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    data = load_cleanup_checkpoint(bad)
    assert data["cleaned_mod_ids"] == []
    save_cleanup_checkpoint({"cleaned_mod_ids": ["1"]}, bad)
    assert load_cleanup_checkpoint(bad)["cleaned_mod_ids"] == ["1"]


def test_already_clean_mod_is_cheap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_backup_asset_cleanup.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    (tmp_path / "backup" / "99" / "offline").mkdir(parents=True)
    store = AssetStore(root=tmp_path / "store")
    r = cleanup_mod_legacy_backup_assets("99", store=store, dry_run=False)
    assert r.ok and r.skipped and r.deleted == 0
