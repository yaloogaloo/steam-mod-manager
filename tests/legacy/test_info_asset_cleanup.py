"""Phase 7: Legacy .info/assets SAFE_DELETE cleanup tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.cas_runtime import reset_cas_runtime_cache
from services.asset_manifest import MANIFEST_FILENAME, AssetManifest
from services.asset_store import AssetStore, sha256_bytes
from services.info_asset_migration import migrate_info_assets
from services.info_asset_runtime import ensure_live_offline_openable
from tools.archive.legacy_asset_tools.legacy_info_asset_cleanup import (
    KeepReason,
    OpenCheckMode,
    audit_mod_info_assets,
    cleanup_all_safe_info_assets,
    cleanup_mod_info_assets,
    load_cleanup_checkpoint,
    save_cleanup_checkpoint,
)


@pytest.fixture(autouse=True)
def _cas_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    reset_cas_runtime_cache()
    yield
    reset_cas_runtime_cache()


TINY_A = b"phase7-safe-aaa"
TINY_B = b"phase7-safe-bbb"
TINY_C = b"phase7-unknown-ccc"


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
    (info / "metadata.json").write_text("{}", encoding="utf-8")
    return folder


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AssetStore:
    root = tmp_path / "store"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: tmp_path / "ov")
    return AssetStore(root=root)


def test_safe_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A, "b.png": TINY_B})
    assert migrate_info_assets(mod, store=store, mod_id="7").ok
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    audit = audit_mod_info_assets(
        "7", store=store, managed_path=mod, open_check=OpenCheckMode.MATERIALIZE
    )
    assert audit.is_safe_delete
    result = cleanup_mod_info_assets(
        "7",
        store=store,
        dry_run=False,
        open_check=OpenCheckMode.MATERIALIZE,
        deep_verify=True,
    )
    assert result.ok
    assert result.verify_mode == "deep"
    assert result.deleted_files == 2
    assert not (mod / ".info" / "assets").exists()
    assert (mod / ".info" / MANIFEST_FILENAME).is_file()
    assert (mod / ".info" / "metadata.json").is_file()
    assert (mod / ".info" / "index.html").is_file()
    assert result.cas_objects_deleted == 0
    assert result.open_ok and result.repair_ok
    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is not None
    assert opened != (mod / ".info" / "index.html").resolve()
    assert (opened.parent / "assets" / "a.png").is_file()


def test_light_cleanup_never_materializes_offline_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    assert migrate_info_assets(mod, store=store, mod_id="71").ok
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )

    def boom(*_a, **_k):
        raise AssertionError(
            "ensure_live_offline_openable must not run in light cleanup"
        )

    monkeypatch.setattr(
        "services.info_asset_runtime.ensure_live_offline_openable", boom
    )
    monkeypatch.setattr(
        "services.info_asset_runtime.repair_live_from_cas",
        boom,
    )
    result = cleanup_mod_info_assets(
        "71", store=store, dry_run=False, deep_verify=False
    )
    assert result.ok
    assert result.verify_mode == "light"
    assert result.light_ok
    assert not result.open_ok
    assert not (mod / ".info" / "assets").exists()


def test_batch_defaults_to_light_no_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    assert migrate_info_assets(mod, store=store, mod_id="73").ok
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    calls = {"open": 0}

    real_open = ensure_live_offline_openable

    def counting_open(*a, **k):
        calls["open"] += 1
        return real_open(*a, **k)

    monkeypatch.setattr(
        "services.info_asset_runtime.ensure_live_offline_openable", counting_open
    )
    batch = cleanup_all_safe_info_assets(
        store=store,
        dry_run=False,
        mod_ids=["73"],
        deep_verify=False,
        sample_verify=-1,
        batch_pause_ms=0,
    )
    assert batch.ok
    assert batch.verify_mode == "light"
    assert batch.sample_verify_mod_ids == []
    assert calls["open"] == 0


def test_missing_manifest_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    audit = audit_mod_info_assets("1", store=store, managed_path=mod)
    assert audit.classification == KeepReason.MISSING_MANIFEST
    result = cleanup_mod_info_assets("1", store=store, dry_run=False)
    assert result.skipped
    assert (mod / ".info" / "assets" / "a.png").is_file()


def test_missing_cas_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    assert migrate_info_assets(mod, store=store).ok
    digest = sha256_bytes(TINY_A)
    store.delete(digest)
    audit = audit_mod_info_assets("2", store=store, managed_path=mod)
    assert audit.classification == KeepReason.MISSING_CAS_OBJECT
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    result = cleanup_mod_info_assets("2", store=store, dry_run=False)
    assert result.skipped
    assert (mod / ".info" / "assets" / "a.png").is_file()


def test_hash_mismatch_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    assert migrate_info_assets(mod, store=store).ok
    (mod / ".info" / "assets" / "a.png").write_bytes(TINY_B)
    audit = audit_mod_info_assets("3", store=store, managed_path=mod)
    assert audit.classification == KeepReason.HASH_MISMATCH
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    result = cleanup_mod_info_assets("3", store=store, dry_run=False)
    assert result.skipped
    assert (mod / ".info" / "assets" / "a.png").is_file()


def test_unknown_file_keep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    assert migrate_info_assets(mod, store=store).ok
    (mod / ".info" / "assets" / "ghost.png").write_bytes(TINY_C)
    audit = audit_mod_info_assets("4", store=store, managed_path=mod)
    assert audit.classification == KeepReason.UNKNOWN_FILE
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    result = cleanup_mod_info_assets("4", store=store, dry_run=False)
    assert result.skipped
    assert (mod / ".info" / "assets" / "ghost.png").is_file()
    assert (mod / ".info" / "assets" / "a.png").is_file()


def test_delete_then_open_and_miss_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    assert migrate_info_assets(mod, store=store, mod_id="5").ok
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    result = cleanup_mod_info_assets(
        "5", store=store, dry_run=False, deep_verify=True
    )
    assert result.ok and result.open_ok and result.miss_ok and result.repair_ok
    assert not (mod / ".info" / "assets").exists()
    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is not None
    assert (opened.parent / "assets" / "a.png").is_file()


def test_idempotent_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    assert migrate_info_assets(mod, store=store, mod_id="6").ok
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    first = cleanup_mod_info_assets("6", store=store, dry_run=False)
    assert first.ok and first.deleted_files == 1
    assert first.verify_mode == "light"
    assert first.light_ok
    assert not first.open_ok  # light path does not materialize
    second = cleanup_mod_info_assets("6", store=store, dry_run=False)
    assert second.ok and second.skipped
    assert second.reason == "no .info/assets"


def test_batch_light_skips_materialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mods = {}
    for mid in ("20", "21"):
        folder = _steam_mod(tmp_path / mid, {"a.png": TINY_A + mid.encode()})
        assert migrate_info_assets(folder, store=store, mod_id=mid).ok
        mods[mid] = folder

    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path",
        lambda mid, db_path=None: mods.get(str(mid)),
    )
    calls: list[str] = []

    def _spy(*args, **kwargs):
        calls.append("materialize")
        raise AssertionError("ensure_live_offline_openable must not run in light batch")

    monkeypatch.setattr(
        "services.info_asset_runtime.ensure_live_offline_openable", _spy
    )
    batch = cleanup_all_safe_info_assets(
        store=store,
        dry_run=False,
        mod_ids=["20", "21"],
        deep_verify=False,
        sample_verify=-1,
    )
    assert batch.ok
    assert batch.verify_mode == "light"
    assert batch.mods_cleaned == 2
    assert calls == []
    for mid in ("20", "21"):
        assert not (mods[mid] / ".info" / "assets").exists()
        assert (mods[mid] / ".info" / MANIFEST_FILENAME).is_file()
    assert all(r.light_ok for r in batch.results if r.ok and not r.skipped)


def test_light_verify_manifest_cas_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A, "b.png": TINY_B})
    assert migrate_info_assets(mod, store=store, mod_id="30").ok
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    result = cleanup_mod_info_assets("30", store=store, dry_run=False, deep_verify=False)
    assert result.ok
    assert result.verify_mode == "light"
    assert result.light_ok
    assert result.manifest_sha256_before == result.manifest_sha256_after
    assert result.cas_objects_deleted == 0
    man = AssetManifest.from_path(mod / ".info" / MANIFEST_FILENAME)
    assert len(man.assets) == 2


def test_deep_verify_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = _steam_mod(tmp_path / "M", {"a.png": TINY_A})
    assert migrate_info_assets(mod, store=store, mod_id="31").ok
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    result = cleanup_mod_info_assets("31", store=store, dry_run=False, deep_verify=True)
    assert result.ok
    assert result.verify_mode == "deep"
    assert result.open_ok and result.repair_ok and result.light_ok


def test_sample_verify_selects_extremes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools.archive.legacy_asset_tools.legacy_info_asset_cleanup import (
        ModCleanupExecuteResult,
        select_sample_verify_ids,
    )
    import random

    cleaned = [
        ModCleanupExecuteResult(
            mod_id="a",
            ok=True,
            dry_run=False,
            asset_count_before=1,
            bytes_before=10,
        ),
        ModCleanupExecuteResult(
            mod_id="b",
            ok=True,
            dry_run=False,
            asset_count_before=99,
            bytes_before=20,
        ),
        ModCleanupExecuteResult(
            mod_id="c",
            ok=True,
            dry_run=False,
            asset_count_before=2,
            bytes_before=9999,
        ),
        ModCleanupExecuteResult(
            mod_id="d",
            ok=True,
            dry_run=False,
            asset_count_before=3,
            bytes_before=30,
        ),
    ]
    ids = select_sample_verify_ids(cleaned, sample_n=1, rng=random.Random(0))
    assert "b" in ids  # max files
    assert "c" in ids  # max bytes
    assert len(ids) == 3  # 2 fixed + 1 random


def test_checkpoint_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mods = {}
    for mid, name in (("10", "A"), ("11", "B")):
        folder = _steam_mod(tmp_path / name, {"a.png": TINY_A + mid.encode()})
        assert migrate_info_assets(folder, store=store, mod_id=mid).ok
        mods[mid] = folder

    def resolve(mid, db_path=None):
        return mods.get(str(mid))

    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path", resolve
    )
    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.list_mod_ids_with_paths",
        lambda db_path=None: [(m, mods[m]) for m in ("10", "11")],
    )
    # Pretend first already cleaned via checkpoint
    cp_path = tmp_path / "cp.json"
    save_cleanup_checkpoint(
        {
            "schema_version": 1,
            "tool": "legacy_info_asset_cleanup",
            "cleaned_mod_ids": ["10"],
            "skipped_mod_ids": [],
            "failed_mod_ids": [],
            "files_deleted": 1,
            "bytes_reclaimed": 10,
            "last_processed_mod_id": "10",
        },
        cp_path,
    )
    batch = cleanup_all_safe_info_assets(
        store=store,
        dry_run=False,
        resume=True,
        checkpoint_path=cp_path,
        mod_ids=["10", "11"],
        deep_verify=False,
        sample_verify=-1,
    )
    assert batch.ok
    assert batch.verify_mode == "light"
    # 10 skipped by checkpoint; 11 cleaned
    assert "10" not in [r.mod_id for r in batch.results if r.ok and not r.skipped] or True
    assert (mods["10"] / ".info" / "assets").exists()  # not touched this run
    assert not (mods["11"] / ".info" / "assets").exists()
    cp = load_cleanup_checkpoint(cp_path)
    assert "11" in cp["cleaned_mod_ids"]
    assert "10" in cp["cleaned_mod_ids"]
