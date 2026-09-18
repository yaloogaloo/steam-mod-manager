"""Fast leftover .info/assets GC — no hash, no OPEN, no Store/Backup writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.cas_runtime import reset_cas_runtime_cache
from services.asset_manifest import MANIFEST_FILENAME, AssetManifest, AssetReference
from services.asset_store import AssetStore, sha256_bytes
from tools.archive.legacy_asset_tools.fast_info_asset_purge import (
    ModCandidate,
    PurgeClass,
    audit,
    classify_mod,
    execute,
    load_checkpoint,
)
from services.info_asset_migration import migrate_info_assets
from services.info_asset_runtime import ensure_live_offline_openable


@pytest.fixture(autouse=True)
def _cas_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMM_CAS_ONLY_INFO_ASSET_RUNTIME", "1")
    reset_cas_runtime_cache()
    yield
    reset_cas_runtime_cache()


PAYLOAD_A = b"fast-purge-aaa"
PAYLOAD_B = b"fast-purge-bbb"
PAYLOAD_U = b"fast-purge-unknown"


def _write_manifest(info: Path, assets: dict[str, bytes]) -> None:
    refs = [
        AssetReference(
            path=f"assets/{name}",
            sha256=sha256_bytes(data),
            size=len(data),
        )
        for name, data in assets.items()
    ]
    if not refs:
        return
    (info / MANIFEST_FILENAME).write_text(
        AssetManifest(assets=refs).to_json(), encoding="utf-8"
    )


def _mod(
    folder: Path,
    assets: dict[str, bytes],
    *,
    manifest_assets: dict[str, bytes] | None = None,
    write_manifest: bool = True,
) -> Path:
    info = folder / ".info"
    ad = info / "assets"
    ad.mkdir(parents=True)
    for name, data in assets.items():
        (ad / name).write_bytes(data)
    names = list(assets)
    imgs = "\n".join(f'<img src="./assets/{n}">' for n in names)
    (info / "index.html").write_text(
        f"<html><body>{imgs}</body></html>", encoding="utf-8"
    )
    (info / "metadata.json").write_text("{}", encoding="utf-8")
    (info / "internal_id").write_text("ent-1", encoding="utf-8")
    if write_manifest:
        _write_manifest(info, manifest_assets if manifest_assets is not None else assets)
    return folder


def _cand(
    folder: Path,
    mod_id: str = "1",
    *,
    platform: str = "steam",
    source_url: str = "https://steamcommunity.com/sharedfiles/filedetails/?id=1",
    workspace_id: str = "1",
) -> ModCandidate:
    return ModCandidate(
        mod_id=mod_id,
        managed_path=folder,
        workspace_id=workspace_id,
        source_url=source_url,
        platform=platform,
    )


def test_safe_delete_unlinks_assets_keeps_sidecar(tmp_path: Path) -> None:
    mod = _mod(tmp_path / "M", {"a.png": PAYLOAD_A, "b.png": PAYLOAD_B})
    result = execute(mods=[_cand(mod)], dry_run=False, checkpoint_path=tmp_path / "cp.json")
    assert result.deleted_files == 2
    assert result.after_files == 0
    assert not (mod / ".info" / "assets").exists()
    assert (mod / ".info" / MANIFEST_FILENAME).is_file()
    assert (mod / ".info" / "index.html").is_file()
    assert (mod / ".info" / "metadata.json").is_file()
    assert (mod / ".info" / "internal_id").read_text(encoding="utf-8") == "ent-1"


def test_unknown_file_kept(tmp_path: Path) -> None:
    mod = _mod(
        tmp_path / "M",
        {"a.png": PAYLOAD_A, "orphan.bin": PAYLOAD_U},
        manifest_assets={"a.png": PAYLOAD_A},
    )
    result = execute(mods=[_cand(mod)], dry_run=False, checkpoint_path=tmp_path / "cp.json")
    assert result.deleted_files == 1
    assert result.after_files == 1
    assert (mod / ".info" / "assets" / "orphan.bin").is_file()
    assert not (mod / ".info" / "assets" / "a.png").exists()


def test_low_risk_no_manifest_steam(tmp_path: Path) -> None:
    mod = _mod(tmp_path / "M", {"a.png": PAYLOAD_A}, write_manifest=False)
    plan = classify_mod(_cand(mod, platform="steam"))
    assert plan.classification == PurgeClass.LOW_RISK_DELETE.value
    result = execute(mods=[_cand(mod)], dry_run=False, checkpoint_path=tmp_path / "cp.json")
    assert result.deleted_files == 1
    assert not (mod / ".info" / "assets").exists()
    assert (mod / ".info" / "index.html").is_file()


def test_keep_no_source(tmp_path: Path) -> None:
    mod = _mod(tmp_path / "M", {"a.png": PAYLOAD_A}, write_manifest=False)
    cand = _cand(mod, platform="other", source_url="", workspace_id="ws-local")
    plan = classify_mod(cand, store_has_objects=False)
    assert plan.classification == PurgeClass.KEEP.value
    result = execute(mods=[cand], dry_run=False, checkpoint_path=tmp_path / "cp.json")
    assert result.deleted_files == 0
    assert (mod / ".info" / "assets" / "a.png").is_file()


def test_dry_run_does_not_delete(tmp_path: Path) -> None:
    mod = _mod(tmp_path / "M", {"a.png": PAYLOAD_A})
    result = execute(mods=[_cand(mod)], dry_run=True, checkpoint_path=tmp_path / "cp.json")
    assert result.deleted_files == 1
    assert (mod / ".info" / "assets" / "a.png").is_file()
    assert not (tmp_path / "cp.json").exists()


def test_never_hashes_or_materializes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _mod(tmp_path / "M", {"a.png": PAYLOAD_A})

    def boom(*_a, **_k):
        raise AssertionError("hash / materialize / store write forbidden")

    monkeypatch.setattr("services.asset_store.sha256_file", boom)
    monkeypatch.setattr("services.info_asset_runtime.ensure_live_offline_openable", boom)
    monkeypatch.setattr("services.asset_store.AssetStore.put_file", boom)
    monkeypatch.setattr("services.asset_store.AssetStore.put_bytes", boom)
    execute(mods=[_cand(mod)], dry_run=False, checkpoint_path=tmp_path / "cp.json")
    assert not (mod / ".info" / "assets").exists()


def test_store_and_backup_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_root = tmp_path / "store"
    backup = tmp_path / "mod_backup" / "1" / "offline"
    backup.mkdir(parents=True)
    marker = backup / "index.html"
    marker.write_text("backup-keep", encoding="utf-8")
    store = AssetStore(root=store_root)
    store.put_bytes(PAYLOAD_A)
    before_objs = {p.resolve() for p in store_root.rglob("*") if p.is_file()}
    before_backup = marker.read_text(encoding="utf-8")
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store_root)
    mod = _mod(tmp_path / "M", {"a.png": PAYLOAD_A})
    execute(
        mods=[_cand(mod)],
        dry_run=False,
        store_root=store_root,
        checkpoint_path=tmp_path / "cp.json",
    )
    after_objs = {p.resolve() for p in store_root.rglob("*") if p.is_file()}
    assert after_objs == before_objs
    assert marker.read_text(encoding="utf-8") == before_backup


def test_open_after_purge_may_rematerialize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssetStore(root=tmp_path / "store")
    monkeypatch.setattr("core.paths.asset_store_dir", lambda: store.root)
    monkeypatch.setattr("core.paths.offline_view_cache_dir", lambda: tmp_path / "ov")
    mod = _mod(tmp_path / "M", {"a.png": PAYLOAD_A})
    assert migrate_info_assets(mod, store=store, mod_id="9").ok
    execute(
        mods=[_cand(mod, mod_id="9")],
        dry_run=False,
        checkpoint_path=tmp_path / "cp.json",
    )
    assert not (mod / ".info" / "assets").exists()
    opened = ensure_live_offline_openable(mod, store=store)
    assert opened is not None
    assert opened.is_file()


def test_cache_dir_remains_deletable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "offline_view").mkdir()
    (cache / "offline_view" / "x").write_text("ephemeral", encoding="utf-8")
    monkeypatch.setattr("core.paths.get_cache_dir", lambda: cache)
    monkeypatch.setattr(
        "core.paths.offline_view_cache_dir", lambda: cache / "offline_view"
    )
    mod = _mod(tmp_path / "M", {"a.png": PAYLOAD_A})
    execute(mods=[_cand(mod)], dry_run=False, checkpoint_path=tmp_path / "cp.json")
    import shutil

    shutil.rmtree(cache)
    assert not cache.exists()


def test_resume_skips_cleaned(tmp_path: Path) -> None:
    a = _mod(tmp_path / "A", {"a.png": PAYLOAD_A})
    b = _mod(tmp_path / "B", {"b.png": PAYLOAD_B})
    cp = tmp_path / "cp.json"
    first = execute(
        mods=[_cand(a, "10"), _cand(b, "11")],
        dry_run=False,
        batch_size=1,
        checkpoint_path=cp,
    )
    assert first.cleaned_mods == 2
    state = load_checkpoint(cp)
    assert "10" in state["cleaned_mod_ids"]
    (a / ".info" / "assets").mkdir()
    (a / ".info" / "assets" / "again.png").write_bytes(PAYLOAD_A)
    second = execute(
        mods=[_cand(a, "10"), _cand(b, "11")],
        dry_run=False,
        resume=True,
        checkpoint_path=cp,
    )
    assert second.skipped_mods >= 1
    assert (a / ".info" / "assets" / "again.png").is_file()


def test_audit_counts_without_delete(tmp_path: Path) -> None:
    mod = _mod(tmp_path / "M", {"a.png": PAYLOAD_A, "b.png": PAYLOAD_B})
    result = audit(mods=[_cand(mod)])
    assert result.files == 2
    assert result.delete_files == 2
    assert result.safe_mods == 1
    assert (mod / ".info" / "assets" / "a.png").is_file()


def test_empty_manifest_is_not_safe(tmp_path: Path) -> None:
    mod = _mod(tmp_path / "M", {"a.png": PAYLOAD_A}, write_manifest=False)
    (mod / ".info" / MANIFEST_FILENAME).write_text(
        '{"schema_version": 1, "assets": []}\n', encoding="utf-8"
    )
    plan = classify_mod(_cand(mod, platform="other", source_url=""))
    assert plan.classification == PurgeClass.KEEP.value
