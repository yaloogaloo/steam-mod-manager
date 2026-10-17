"""Tests for tools/audit_asset_store_architecture.py (readonly)."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.archive import (
    ASSET_CACHE_MAX_BYTES,
    ASSET_CACHE_TTL_SECONDS,
    _asset_cache_key,
    _is_asset_cache_filename,
)
from tools.audit_asset_store_architecture import (
    HashIndex,
    cache_key_is_url_hash_not_content,
    code_semantics,
    estimate_savings,
    intersect_stats,
    parse_cache_key_from_filename,
    sha256_file,
)


TINY = b"payload-aaa"
TINY2 = b"payload-bbb"


def test_asset_cache_key_is_url_sha256_not_content() -> None:
    url = "https://cdn.example/shared.css"
    key = _asset_cache_key(url)
    assert len(key) == 64
    content = hashlib.sha256(b"body{}").hexdigest()
    assert key != content
    assert cache_key_is_url_hash_not_content(url, content)


def test_same_content_different_url_different_cache_keys() -> None:
    a = _asset_cache_key("https://cdn.example/a.png")
    b = _asset_cache_key("https://cdn.example/b.png")
    assert a != b


def test_cache_filename_layout() -> None:
    key = _asset_cache_key("https://cdn.example/x.png")
    name = f"{key}.png"
    assert _is_asset_cache_filename(name)
    assert parse_cache_key_from_filename(name) == key


def test_cache_semantics_declare_eviction_and_cache_class() -> None:
    sem = code_semantics()
    assert sem["semantic_class"] == "cache"
    assert sem["durable_cas_direct"] is False
    assert sem["evolve_verdict"] == "CAN EVOLVE WITH REFACTOR"
    assert sem["eviction"]["ttl_seconds"] == ASSET_CACHE_TTL_SECONDS
    assert sem["eviction"]["max_bytes"] == ASSET_CACHE_MAX_BYTES
    assert ASSET_CACHE_TTL_SECONDS > 0
    assert ASSET_CACHE_MAX_BYTES > 0


def test_duplicate_detection_and_overlap_bytes() -> None:
    info = HashIndex()
    cache = HashIndex()
    backup = HashIndex()
    h1 = hashlib.sha256(TINY).hexdigest()
    h2 = hashlib.sha256(TINY2).hexdigest()
    info.add(h1, len(TINY), path=Path("i1"), type_name="png")
    info.add(h1, len(TINY), path=Path("i2"), type_name="png")
    info.add(h2, len(TINY2), path=Path("i3"), type_name="png")
    cache.add(h1, len(TINY), path=Path("c1"), type_name="png")
    backup.add(h1, len(TINY), path=Path("b1"), type_name="png")

    assert info.unique_bytes() == len(TINY) + len(TINY2)
    assert info.duplicate_instance_bytes() == len(TINY)
    inter = intersect_stats(info, cache, label="A and B")
    assert inter["unique_hashes"] == 1
    assert inter["unique_bytes"] == len(TINY)

    sav = estimate_savings(info, backup, cache)
    assert sav["current_physical_bytes"] == (
        info.bytes_total + backup.bytes_total + cache.bytes_total
    )
    assert sav["scenarios"]["global_cas_info_and_backup"]["estimated_bytes"] == (
        len(TINY) + len(TINY2)
    )


def test_info_backup_cache_overlap_helpers(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(TINY)
    b.write_bytes(TINY)
    assert sha256_file(a) == sha256_file(b)


def test_audit_readonly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tools import audit_asset_store_architecture as audit

    library = tmp_path / "mod" / "Game" / "ModA"
    info_assets = library / ".info" / "assets"
    info_assets.mkdir(parents=True)
    (info_assets / "aaaa.png").write_bytes(TINY)

    backup_assets = tmp_path / "data" / "mod_backup" / "1" / "offline" / "assets"
    backup_assets.mkdir(parents=True)
    (backup_assets / "aaaa.png").write_bytes(TINY)

    cache = tmp_path / "data" / "asset_cache"
    cache.mkdir(parents=True)
    key = _asset_cache_key("https://example.com/aaaa.png")
    (cache / f"{key}.png").write_bytes(TINY)

    before = {
        "info": (info_assets / "aaaa.png").read_bytes(),
        "backup": (backup_assets / "aaaa.png").read_bytes(),
        "cache": (cache / f"{key}.png").read_bytes(),
        "info_mtime": (info_assets / "aaaa.png").stat().st_mtime_ns,
        "cache_mtime": (cache / f"{key}.png").stat().st_mtime_ns,
    }

    out = tmp_path / "_tmp_out"
    out.mkdir()
    monkeypatch.setattr(audit, "OUT_DIR", out)
    monkeypatch.setattr(audit, "OUT_JSON", out / "a.json")
    monkeypatch.setattr(audit, "OUT_REPORT", out / "a.md")
    monkeypatch.setattr(audit, "OUT_CACHE_TOP", out / "top.json")
    monkeypatch.setattr(audit, "_REPO", tmp_path)

    payload = audit.run_audit(
        library_root=tmp_path / "mod",
        data_root=tmp_path / "data",
    )
    assert payload["status"] == "ARCHITECTURE AUDIT COMPLETE"
    assert payload["production_safety"]["production_asset_cache_modified"] == "NO"
    assert payload["overlap"]["info_cap_cache"]["unique_bytes"] == len(TINY)
    assert payload["conclusions"]["A_asset_cache"] == "CAN EVOLVE WITH REFACTOR"

    assert (info_assets / "aaaa.png").read_bytes() == before["info"]
    assert (backup_assets / "aaaa.png").read_bytes() == before["backup"]
    assert (cache / f"{key}.png").read_bytes() == before["cache"]
    assert (info_assets / "aaaa.png").stat().st_mtime_ns == before["info_mtime"]
    assert (cache / f"{key}.png").stat().st_mtime_ns == before["cache_mtime"]
