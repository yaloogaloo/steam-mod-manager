"""Backup storage contract — data/mod_backup must not hold Mod payloads.

Full layout/extension assertions run on an isolated synthetic tree.
Production ``data/mod_backup`` gets a shallow, time-budgeted smoke sample
(never a multi-minute full-tree walk).
"""

from __future__ import annotations

import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP_ROOT = ROOT / "data" / "mod_backup"

FORBIDDEN_EXTENSIONS = frozenset(
    {
        ".pak",
        ".zip",
        ".rar",
        ".7z",
        ".dll",
        ".exe",
        ".lua",
        ".mesh",
        ".fbx",
        ".uasset",
    }
)

# Shallow production smoke — sample buckets/files only.
_PROD_SCAN_BUDGET_S = 2.0
_PROD_MAX_BUCKETS = 40
_PROD_MAX_FILES = 2000


def _bucket_relative_ok(rel_posix: str, name: str) -> bool:
    """True if path inside ``mod_backup/<id>/`` is metadata, cover, or offline."""
    lower_rel = rel_posix.lower()
    lower_name = name.lower()
    if lower_name == "metadata.json" and "/" not in lower_rel:
        return True
    if lower_name.startswith("cover.") and "/" not in lower_rel:
        return True
    if lower_rel.startswith("offline/"):
        return True
    return False


def _scan_bucket(mid_dir: Path) -> tuple[list[str], list[str]]:
    forbidden_hits: list[str] = []
    layout_hits: list[str] = []
    for path in mid_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(mid_dir).as_posix()
        if path.suffix.lower() in FORBIDDEN_EXTENSIONS:
            forbidden_hits.append(f"{mid_dir.name}/{rel}")
            continue
        if not _bucket_relative_ok(rel, path.name):
            layout_hits.append(f"{mid_dir.name}/{rel}")
    return forbidden_hits, layout_hits


def test_mod_backup_synthetic_tree_enforces_layout(tmp_path: Path) -> None:
    """Full layout contract on an isolated synthetic tree (no production IO)."""
    root = tmp_path / "mod_backup"
    good = root / "1001"
    good.mkdir(parents=True)
    (good / "metadata.json").write_text("{}", encoding="utf-8")
    (good / "cover.jpg").write_bytes(b"x")
    offline = good / "offline"
    offline.mkdir()
    (offline / "index.html").write_text("<html></html>", encoding="utf-8")
    (offline / "asset.css").write_text("a{}", encoding="utf-8")

    forbidden, layout = _scan_bucket(good)
    assert not forbidden
    assert not layout

    bad = root / "1002"
    bad.mkdir()
    (bad / "payload.pak").write_bytes(b"x")
    forbidden, _layout = _scan_bucket(bad)
    assert any(h.endswith("payload.pak") for h in forbidden)


def test_mod_backup_production_forbids_payload_extensions() -> None:
    """Shallow production smoke — forbidden extensions only (bounded)."""
    if not BACKUP_ROOT.is_dir():
        return

    deadline = time.monotonic() + _PROD_SCAN_BUDGET_S
    forbidden_hits: list[str] = []
    scanned_files = 0
    buckets = 0

    for mid_dir in BACKUP_ROOT.iterdir():
        if time.monotonic() >= deadline:
            break
        if buckets >= _PROD_MAX_BUCKETS:
            break
        if not mid_dir.is_dir():
            continue
        buckets += 1
        for path in mid_dir.rglob("*"):
            if time.monotonic() >= deadline:
                break
            if scanned_files >= _PROD_MAX_FILES:
                break
            if not path.is_file():
                continue
            scanned_files += 1
            if path.suffix.lower() in FORBIDDEN_EXTENSIONS:
                rel = path.relative_to(mid_dir).as_posix()
                forbidden_hits.append(f"{mid_dir.name}/{rel}")
                if len(forbidden_hits) >= 40:
                    break
        if len(forbidden_hits) >= 40 or scanned_files >= _PROD_MAX_FILES:
            break

    assert not forbidden_hits, (
        "data/mod_backup/ must not store Mod payload binaries "
        f"(metadata/cover/offline only). examples: {forbidden_hits}"
    )
    assert scanned_files >= 0
