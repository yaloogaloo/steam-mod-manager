"""CAS_ONLY lifecycle + background IO + cleanup performance regressions."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from core.cas_runtime import reset_cas_runtime_cache
from services.asset_manifest import MANIFEST_FILENAME, AssetManifest
from services.asset_store import AssetStore
from services.background_asset_task import (
    BackgroundAssetTask,
    run_in_thread,
)
from services.info_asset_runtime import (
    ensure_live_offline_openable,
    finalize_live_offline_to_cas,
    repair_live_from_cas,
)
from tools.archive.legacy_asset_tools.legacy_info_asset_cleanup import (
    OpenCheckMode,
    audit_mod_info_assets,
    cleanup_all_safe_info_assets,
    cleanup_mod_info_assets,
)
from services.offline.backup_closure import (
    snapshot_offline_closure,
)
from services.offline.manual_import import import_offline_snapshot


@pytest.fixture(autouse=True)
def _cas_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    reset_cas_runtime_cache()
    yield
    reset_cas_runtime_cache()


def _page(info: Path, *, n_assets: int = 4) -> None:
    assets = info / "assets"
    assets.mkdir(parents=True)
    names = []
    for i in range(n_assets):
        name = f"a{i}.bin"
        (assets / name).write_bytes(f"payload-{i}-".encode() * 8)
        names.append(name)
    (assets / "style.css").write_bytes(b"body{background:url(a0.bin)}")
    names.append("style.css")
    imgs = "\n".join(f'<img src="./assets/{n}">' for n in names if n.endswith(".bin"))
    (info / "index.html").write_text(
        f'<html><head><link rel="stylesheet" href="./assets/style.css"></head>'
        f"<body>{imgs}</body></html>",
        encoding="utf-8",
    )


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AssetStore:
    root = tmp_path / "store"
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: tmp_path / "ov")
    return AssetStore(root=root)


# ---------------------------------------------------------------------------
# Lifecycle: Steam-like / Nexus-like / GitHub-like
# ---------------------------------------------------------------------------


def test_steam_lifecycle_finalize_open_miss_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = tmp_path / "SteamMod"
    info = mod / ".info"
    _page(info)
    assert finalize_live_offline_to_cas(info, store=store, mod_id="101").ok
    assert not (info / "assets").exists()
    assert (info / MANIFEST_FILENAME).is_file()

    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is not None
    assert opened.name == "index.html"
    assert str(tmp_path / "ov") in str(opened.resolve())
    assert (opened.parent / "assets" / "style.css").is_file()

    # MISS / Repair: wipe LIVE manifest, restore from Backup CAS snapshot
    monkeypatch.setattr(
        "services.backup_asset_migration.backup_root",
        lambda mid: tmp_path / "backup" / str(mid),
    )
    dest = tmp_path / "backup" / "101" / "offline"
    assert snapshot_offline_closure(info / "index.html", dest)
    (info / MANIFEST_FILENAME).unlink()
    repair = repair_live_from_cas(mod, mod_id="101", store=store)
    assert repair.ok
    assert not (info / "assets").exists()
    assert (info / MANIFEST_FILENAME).is_file()
    opened2 = ensure_live_offline_openable(mod, store=store)
    assert opened2 is not None
    assert (opened2.parent / "assets" / "a0.bin").is_file()


def test_nexus_import_finalize_clears_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    src = tmp_path / "saved.html"
    src.write_text(
        "<html><body><img src='https://example.invalid/x.png'></body></html>",
        encoding="utf-8",
    )
    out = tmp_path / "ModN" / ".info" / "offline"
    # import_offline_snapshot finalizes internally
    index, count, fmt = import_offline_snapshot(src, out, title="n")
    assert fmt == "html"
    assert index.is_file()
    # After finalize under CAS_ONLY, durable assets dir should be gone or empty
    assets = out / "assets"
    leftover = (
        sum(1 for p in assets.rglob("*") if p.is_file()) if assets.is_dir() else 0
    )
    assert leftover == 0
    # Manifest may be absent if no local assets were downloaded (remote-only).
    # OPEN must not depend on durable assets.
    opened = ensure_live_offline_openable(tmp_path / "ModN", store=store)
    assert opened is not None
    from services.offline_view_cache import is_offline_view_path

    assert is_offline_view_path(opened)


def test_github_like_empty_assets_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = tmp_path / "GhMod"
    off = mod / ".info" / "offline"
    off.mkdir(parents=True)
    (off / "assets").mkdir()
    (off / "index.html").write_text(
        "<html><body>github</body></html>", encoding="utf-8"
    )
    result = finalize_live_offline_to_cas(off, store=store, mod_id="202")
    assert result.ok
    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is not None


def test_cas_only_open_never_returns_raw_when_cas_materialize_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = tmp_path / "Broken"
    info = mod / ".info"
    _page(info, n_assets=2)
    assert finalize_live_offline_to_cas(info, store=store).ok
    # Corrupt store so materialize fails
    man = AssetManifest.from_path(info / MANIFEST_FILENAME)
    for ref in man.assets:
        store.delete(ref.sha256)
    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is None


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


def test_background_task_runs_off_caller_thread_and_checkpoints(
    tmp_path: Path,
) -> None:
    task = BackgroundAssetTask(
        batch_size=10,
        batch_pause_ms=1,
        checkpoint_every_files=25,
        name="t",
    )
    items = list(range(60))
    checkpoints: list[int] = []
    caller = threading.get_ident()
    worker_ids: set[int] = set()

    def work() -> int:
        def process_one(i: int) -> int:
            worker_ids.add(threading.get_ident())
            time.sleep(0.001)
            return 1

        prog = task.run_batches(
            items,
            process_one,
            phase="x",
            on_checkpoint=lambda p: checkpoints.append(p.processed),
        )
        return prog.processed

    thread, results, errors = run_in_thread(work, name="bg-test")
    thread.join(timeout=10)
    assert not errors
    assert results and results[0] == 60
    assert caller not in worker_ids
    assert checkpoints  # at least one batch checkpoint


def test_background_task_cancel(
    tmp_path: Path,
) -> None:
    task = BackgroundAssetTask(batch_size=5, batch_pause_ms=5, name="c")

    def process_one(i: int) -> int:
        if i == 8:
            task.request_cancel()
        time.sleep(0.002)
        return 1

    prog = task.run_batches(range(100), process_one, phase="c")
    assert prog.cancelled
    assert prog.processed < 100


# ---------------------------------------------------------------------------
# Cleanup performance: trust-audit skips rehash; checkpoint batching
# ---------------------------------------------------------------------------


def test_from_audit_trust_skips_full_rehash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mod = tmp_path / "CleanMe"
    _page(mod / ".info", n_assets=6)
    assert finalize_live_offline_to_cas(mod / ".info", store=store, mod_id="77").ok
    # Recreate durable assets as legacy leftovers (Phase 7 scenario)
    assets = mod / ".info" / "assets"
    assets.mkdir(parents=True)
    for i in range(6):
        (assets / f"a{i}.bin").write_bytes(f"payload-{i}-".encode() * 8)
    (assets / "style.css").write_bytes(b"body{background:url(a0.bin)}")

    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path",
        lambda mid, db_path=None: mod,
    )
    audit = audit_mod_info_assets(
        "77", store=store, managed_path=mod, open_check=OpenCheckMode.MANIFEST
    )
    assert audit.is_safe_delete

    hash_calls = {"n": 0}
    real_sha = __import__(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup", fromlist=["_file_sha256"]
    )._file_sha256

    def counting_sha(path: Path) -> str:
        hash_calls["n"] += 1
        return real_sha(path)

    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup._file_sha256", counting_sha
    )

    result = cleanup_mod_info_assets(
        "77",
        store=store,
        dry_run=False,
        trust_audit=True,
        audit_snapshot=audit,
    )
    assert result.ok
    assert hash_calls["n"] == 0  # no per-file disk rehash
    assert not (mod / ".info" / "assets").exists()


def test_cleanup_checkpoint_batched_not_per_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path, monkeypatch)
    mods = []
    for i in range(3):
        mod = tmp_path / f"M{i}"
        _page(mod / ".info", n_assets=5)
        finalize_live_offline_to_cas(mod / ".info", store=store, mod_id=str(100 + i))
        # Recreate durable assets with the SAME bytes as captured (legacy leftover).
        _page(mod / ".info", n_assets=5)
        mods.append((str(100 + i), mod))

    mapping = {mid: path for mid, path in mods}

    def resolve(mid, db_path=None):
        return mapping.get(str(mid))

    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.resolve_mod_managed_path", resolve
    )

    snaps = {}
    for mid, path in mods:
        a = audit_mod_info_assets(
            mid, store=store, managed_path=path, open_check=OpenCheckMode.MANIFEST
        )
        assert a.is_safe_delete
        snaps[mid] = a

    cp_path = tmp_path / "ckpt.json"
    writes_before = {"n": 0}
    real_save = __import__(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup", fromlist=["save_cleanup_checkpoint"]
    ).save_cleanup_checkpoint

    def counting_save(data, path=None):
        writes_before["n"] += 1
        return real_save(data, path)

    monkeypatch.setattr(
        "tools.archive.legacy_asset_tools.legacy_info_asset_cleanup.save_cleanup_checkpoint", counting_save
    )

    batch = cleanup_all_safe_info_assets(
        store=store,
        dry_run=False,
        checkpoint_path=cp_path,
        mod_ids=[m[0] for m in mods],
        trust_audit=True,
        audit_by_mod=snaps,
        batch_size=100,
        batch_pause_ms=0,
        checkpoint_every_files=10_000,  # prefer mod-boundary checkpoints
    )
    assert batch.ok
    # 3 mods → ~3 checkpoint writes (not one per file)
    assert writes_before["n"] <= 6
    assert writes_before["n"] >= 3
    total_files = sum(5 + 1 for _ in mods)
    assert writes_before["n"] < total_files
