"""Durable Asset Store foundation tests (Phase 1)."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from services.asset_store import (
    AssetCorruption,
    AssetNotFound,
    AssetStore,
    AssetStoreValueError,
    normalize_sha256,
    sha256_bytes,
)


PAYLOAD_A = b"durable-cas-payload-aaa"
PAYLOAD_B = b"durable-cas-payload-bbb"


@pytest.fixture()
def store(tmp_path: Path) -> AssetStore:
    return AssetStore(root=tmp_path / "asset_store")


def test_put_bytes_creates_sha256_object(store: AssetStore) -> None:
    obj = store.put_bytes(PAYLOAD_A)
    digest = sha256_bytes(PAYLOAD_A)
    assert obj.sha256 == digest
    assert obj.created is True
    assert obj.path.is_file()
    assert obj.path.read_bytes() == PAYLOAD_A
    assert obj.path.name == digest
    assert obj.path.parent.name == digest[:2]


def test_same_bytes_same_object(store: AssetStore) -> None:
    a = store.put_bytes(PAYLOAD_A)
    b = store.put_bytes(PAYLOAD_A)
    assert a.sha256 == b.sha256
    assert a.path == b.path
    assert b.created is False


def test_different_bytes_different_object(store: AssetStore) -> None:
    a = store.put_bytes(PAYLOAD_A)
    b = store.put_bytes(PAYLOAD_B)
    assert a.sha256 != b.sha256
    assert a.path != b.path


def test_existing_object_not_overwritten(store: AssetStore) -> None:
    first = store.put_bytes(PAYLOAD_A)
    mtime = first.path.stat().st_mtime_ns
    again = store.put_bytes(PAYLOAD_A)
    assert again.created is False
    assert first.path.stat().st_mtime_ns == mtime
    assert first.path.read_bytes() == PAYLOAD_A


def test_get_path_and_open(store: AssetStore) -> None:
    obj = store.put_bytes(PAYLOAD_A)
    path = store.get_path(obj.sha256)
    assert path == obj.path
    with store.open(obj.sha256) as fh:
        assert fh.read() == PAYLOAD_A


def test_missing_object_raises(store: AssetStore) -> None:
    digest = sha256_bytes(b"never-stored")
    with pytest.raises(AssetNotFound):
        store.get_path(digest)
    with pytest.raises(AssetNotFound):
        store.open(digest)
    with pytest.raises(AssetNotFound):
        store.verify(digest)
    assert store.has(digest) is False


def test_verify_ok(store: AssetStore) -> None:
    obj = store.put_bytes(PAYLOAD_A)
    verified = store.verify(obj.sha256)
    assert verified.sha256 == obj.sha256
    assert verified.size == len(PAYLOAD_A)


def test_corrupted_object_detected(store: AssetStore) -> None:
    obj = store.put_bytes(PAYLOAD_A)
    # Corrupt published file in place (simulates bit-rot).
    obj.path.write_bytes(b"corrupted-bytes-not-matching-hash")
    with pytest.raises(AssetCorruption):
        store.verify(obj.sha256)


def test_wrong_expected_hash_rejected(store: AssetStore) -> None:
    wrong = sha256_bytes(PAYLOAD_B)
    with pytest.raises(AssetStoreValueError):
        store.put_bytes(PAYLOAD_A, expected_sha256=wrong)


def test_empty_object_rejected(store: AssetStore) -> None:
    with pytest.raises(AssetStoreValueError):
        store.put_bytes(b"")


def test_put_file(store: AssetStore, tmp_path: Path) -> None:
    src = tmp_path / "src.bin"
    src.write_bytes(PAYLOAD_A)
    obj = store.put_file(src)
    assert obj.sha256 == sha256_bytes(PAYLOAD_A)
    assert store.get_path(obj.sha256).read_bytes() == PAYLOAD_A


def test_temp_publish_leaves_no_partial_named_object(store: AssetStore) -> None:
    obj = store.put_bytes(PAYLOAD_A)
    # Only the full hash name under sha256/<shard>/ — no .part under objects.
    shard = store.root / "sha256" / obj.sha256[:2]
    names = [p.name for p in shard.iterdir()]
    assert names == [obj.sha256]
    assert not any(n.endswith(".part") for n in names)


def test_hash_mismatch_temp_not_published(store: AssetStore, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real = __import__("services.asset_store", fromlist=["sha256_file"]).sha256_file

    def flaky(path: Path, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        # First call (pre-publish verify of temp) lies.
        if calls["n"] == 1:
            return "0" * 64
        return real(path, **kwargs)

    monkeypatch.setattr("services.asset_store.sha256_file", flaky)
    with pytest.raises(AssetCorruption):
        store.put_bytes(PAYLOAD_A)
    # No durable object under the true digest.
    assert not store.has(sha256_bytes(PAYLOAD_A))
    # Temps cleaned or only under .tmp
    objects = list((store.root / "sha256").rglob("*")) if (store.root / "sha256").exists() else []
    files = [p for p in objects if p.is_file()]
    assert files == []


def test_concurrent_same_content_one_valid_object(store: AssetStore) -> None:
    results: list = []
    errors: list = []

    def worker() -> None:
        try:
            results.append(store.put_bytes(PAYLOAD_A))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert results
    digests = {r.sha256 for r in results}
    assert digests == {sha256_bytes(PAYLOAD_A)}
    paths = {r.path.resolve() for r in results}
    assert len(paths) == 1
    assert store.verify(sha256_bytes(PAYLOAD_A)).size == len(PAYLOAD_A)
    # Exactly one object file for this hash
    shard = store.root / "sha256" / sha256_bytes(PAYLOAD_A)[:2]
    assert len([p for p in shard.iterdir() if p.is_file()]) == 1


def test_concurrent_threadpool_same_hash(store: AssetStore) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(store.put_bytes, PAYLOAD_A) for _ in range(16)]
        outs = [f.result() for f in as_completed(futs)]
    assert all(o.sha256 == sha256_bytes(PAYLOAD_A) for o in outs)
    assert store.verify(sha256_bytes(PAYLOAD_A)).path.is_file()


def test_cleanup_temps_does_not_remove_objects(store: AssetStore) -> None:
    obj = store.put_bytes(PAYLOAD_A)
    store.ensure_layout()
    orphan = store.root / ".tmp" / "put_deadbeef.part"
    orphan.write_bytes(b"orphan")
    removed = store.cleanup_temps()
    assert removed >= 1
    assert not orphan.exists()
    assert obj.path.is_file()


def test_normalize_sha256_rejects_invalid() -> None:
    with pytest.raises(AssetStoreValueError):
        normalize_sha256("abc")
    with pytest.raises(AssetStoreValueError):
        normalize_sha256("g" * 64)


def test_open_rejects_write_modes(store: AssetStore) -> None:
    obj = store.put_bytes(PAYLOAD_A)
    with pytest.raises(AssetStoreValueError):
        store.open(obj.sha256, "wb")


def test_delete(store: AssetStore) -> None:
    obj = store.put_bytes(PAYLOAD_A)
    assert store.delete(obj.sha256) is True
    assert store.has(obj.sha256) is False
    assert store.delete(obj.sha256) is False


def test_production_dirs_unchanged_when_using_tmp_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 1 must not populate production asset_store or mutate .info/Backup/cache."""
    from core.paths import project_root

    root = project_root()
    info_probe = root / "mod"
    backup = root / "data" / "mod_backup"
    cache = root / "cache" / "asset_cache"
    prod_store = root / "data" / "asset_store"

    def _shallow_dir_stats(path: Path) -> tuple[int, int]:
        """File count + bytes for one directory level (no deep walk)."""
        if not path.is_dir():
            return 0, 0
        files = 0
        bytes_ = 0
        try:
            for entry in path.iterdir():
                try:
                    if entry.is_file():
                        files += 1
                        bytes_ += int(entry.stat().st_size)
                except OSError:
                    continue
        except OSError:
            return 0, 0
        return files, bytes_

    def _info_assets_sample() -> tuple[int, int]:
        files = bytes_ = 0
        scanned = 0
        if not info_probe.is_dir():
            return 0, 0
        for game in info_probe.iterdir():
            if not game.is_dir():
                continue
            for mod in game.iterdir():
                if not mod.is_dir():
                    continue
                for assets in (
                    mod / ".info" / "assets",
                    mod / ".info" / "offline" / "assets",
                ):
                    f, b = _shallow_dir_stats(assets)
                    files += f
                    bytes_ += b
                scanned += 1
                if scanned >= 25:
                    return files, bytes_
        return files, bytes_

    def _backup_shallow() -> tuple[int, int]:
        if not backup.is_dir():
            return 0, 0
        buckets = 0
        try:
            for entry in backup.iterdir():
                if entry.is_dir():
                    buckets += 1
        except OSError:
            return 0, 0
        # Also sample first bucket offline/assets shallow size if present.
        sample_bytes = 0
        try:
            first = next(p for p in backup.iterdir() if p.is_dir())
            f, b = _shallow_dir_stats(first / "offline" / "assets")
            sample_bytes = b
            buckets = buckets  # noqa: PLW0127 — keep explicit
            return buckets, sample_bytes + f
        except StopIteration:
            return buckets, 0

    before_info = _info_assets_sample()
    before_backup = _backup_shallow()
    before_cache = _shallow_dir_stats(cache)
    before_prod_store = (
        _shallow_dir_stats(prod_store / "sha256")
        if (prod_store / "sha256").is_dir()
        else (0, 0)
    )
    prod_store_existed = prod_store.exists()

    local = AssetStore(root=tmp_path / "isolated_store")
    local.put_bytes(PAYLOAD_A)
    local.put_bytes(PAYLOAD_B)
    local.verify(sha256_bytes(PAYLOAD_A))

    after_info = _info_assets_sample()
    after_backup = _backup_shallow()
    after_cache = _shallow_dir_stats(cache)
    after_prod_store = (
        _shallow_dir_stats(prod_store / "sha256")
        if (prod_store / "sha256").is_dir()
        else (0, 0)
    )

    assert after_info == before_info
    assert after_backup == before_backup
    assert after_cache == before_cache
    assert after_prod_store == before_prod_store
    # Must not create/populate production asset_store from this test.
    if not prod_store_existed:
        assert not prod_store.exists() or not any(prod_store.rglob("*"))
    assert after_prod_store[0] == before_prod_store[0]


def test_asset_store_does_not_import_archive_cache() -> None:
    import services.asset_store as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert "from core.paths import asset_cache_dir" not in src
    assert "import services.archive" not in src
    assert "from services.archive" not in src
    # No callable coupling to the URL cache helper.
    assert "asset_cache_dir" not in src
    assert "prune_asset_cache" not in src
    assert "_find_cached_asset" not in src
    assert "_asset_cache_key" not in src
