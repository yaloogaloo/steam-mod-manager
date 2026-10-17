"""Conservative orphan backup pairing — strong Frozen internal_id only."""

from __future__ import annotations

import json
from pathlib import Path

from core.paths import project_root
from services.backup_storage_cleanup import (
    classify_orphan_backups,
    delete_orphan_backups,
)


def _bucket(
    root: Path,
    name: str,
    *,
    internal_id: str,
    title: str = "Mod",
    workspace_id: str = "ws",
    cover: bytes | None = b"COVER",
    index: str | None = "<html>ok</html>",
    extra: dict | None = None,
) -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    payload = {
        "internal_id": internal_id,
        "title": title,
        "workspace_id": workspace_id,
        **(extra or {}),
    }
    (path / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
    if cover is not None:
        (path / "cover.jpg").write_bytes(cover)
    if index is not None:
        offline = path / "offline"
        offline.mkdir(exist_ok=True)
        (offline / "index.html").write_text(index, encoding="utf-8")
    return path


def test_orphan_without_strong_evidence_is_kept(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    _bucket(root, "10", internal_id="live-a", title="Shared")
    _bucket(root, "20", internal_id="orphan-b", title="Shared")
    classified = classify_orphan_backups(root, live_ids=["10"])
    assert classified["safe_delete"] == []
    assert classified["retain"][0]["id"] == "20"


def test_same_internal_id_and_complete_live_is_safe_delete(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    live = _bucket(root, "465", internal_id="same-uuid", title="Live")
    orphan = _bucket(root, "3681020076", internal_id="same-uuid", title="Live")
    classified = classify_orphan_backups(root, live_ids=["465"])
    assert {row["id"] for row in classified["safe_delete"]} == {"3681020076"}
    assert classified["safe_delete"][0]["live_id"] == "465"
    result = delete_orphan_backups(
        root,
        ["3681020076"],
        live_ids=["465"],
        live_pairs={"3681020076": "465"},
    )
    assert result["deleted"] == 1
    assert not orphan.exists()
    assert live.exists()
    assert (live / "metadata.json").is_file()
    assert (live / "cover.jpg").is_file()


def test_different_internal_id_is_kept(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    _bucket(root, "1", internal_id="uuid-a")
    _bucket(root, "2", internal_id="uuid-b")
    classified = classify_orphan_backups(root, live_ids=["1"])
    assert classified["safe_delete"] == []
    assert classified["retain"][0]["id"] == "2"


def test_same_title_workspace_cover_different_internal_id_is_kept(tmp_path: Path) -> None:
    """1336 ↔ 9000000000003299 class: identical display fields, different Frozen id."""
    root = tmp_path / "mod_backup"
    shared = {
        "title": "ancient-mega-pack-rel",
        "workspace_id": "17877709359211874",
        "cover": b"SAMECOVER",
        "index": "<html>same</html>",
    }
    _bucket(root, "1336", internal_id="8a2b1c86-bf67-4ee4-82ca-48cb0f052f05", **shared)
    orphan = _bucket(
        root,
        "9000000000003299",
        internal_id="3250b50e-73ae-49e9-965e-c205c2039fb2",
        **shared,
    )
    classified = classify_orphan_backups(root, live_ids=["1336"])
    assert classified["safe_delete"] == []
    assert orphan.exists()
    row = classified["retain"][0]
    assert row["id"] == "9000000000003299"
    assert row["internal_id"] != "8a2b1c86-bf67-4ee4-82ca-48cb0f052f05"


def test_no_live_counterpart_is_kept(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    _bucket(root, "9000000000000001", internal_id="only-orphan")
    classified = classify_orphan_backups(root, live_ids=["999"])
    assert classified["safe_delete"] == []
    assert classified["retain"][0]["id"] == "9000000000000001"
    assert classified["retain"][0]["reason"] == "no_live_internal_id_match"


def test_deletion_does_not_affect_live_backup(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    live = _bucket(root, "50", internal_id="dup")
    orphan = _bucket(root, "51", internal_id="dup")
    cover_before = (live / "cover.jpg").read_bytes()
    meta_before = (live / "metadata.json").read_text(encoding="utf-8")
    delete_orphan_backups(
        root, ["51"], live_ids=["50"], live_pairs={"51": "50"}
    )
    assert not orphan.exists()
    assert live.exists()
    assert (live / "cover.jpg").read_bytes() == cover_before
    assert (live / "metadata.json").read_text(encoding="utf-8") == meta_before


def test_cleanup_module_does_not_open_sqlite() -> None:
    source = project_root() / "services" / "backup_storage_cleanup.py"
    text = source.read_text(encoding="utf-8")
    assert "sqlite3" not in text
    assert "mod_manager.db" not in text


def test_dry_run_classify_does_not_modify_filesystem(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    live = _bucket(root, "7", internal_id="dup")
    orphan = _bucket(root, "8", internal_id="dup")
    before = {
        p: (p.stat().st_mtime_ns, p.stat().st_size)
        for p in (list(live.rglob("*")) + list(orphan.rglob("*")))
        if p.is_file()
    }
    classified = classify_orphan_backups(root, live_ids=["7"])
    assert classified["safe_delete"][0]["id"] == "8"
    assert live.exists() and orphan.exists()
    after = {
        p: (p.stat().st_mtime_ns, p.stat().st_size)
        for p in (list(live.rglob("*")) + list(orphan.rglob("*")))
        if p.is_file()
    }
    assert after == before


def test_second_delete_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    _bucket(root, "80", internal_id="dup")
    _bucket(root, "81", internal_id="dup")
    first = delete_orphan_backups(
        root, ["81"], live_ids=["80"], live_pairs={"81": "80"}
    )
    assert first["deleted"] == 1
    classified = classify_orphan_backups(root, live_ids=["80"])
    assert classified["safe_delete"] == []
    second = delete_orphan_backups(
        root, ["81"], live_ids=["80"], live_pairs={"81": "80"}
    )
    assert second["deleted"] == 0
    assert (root / "80").is_dir()


def test_incomplete_orphan_without_metadata_is_kept(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    _bucket(root, "90", internal_id="live")
    incomplete = root / "91"
    incomplete.mkdir(parents=True)
    (incomplete / "cover.jpg").write_bytes(b"only-cover")
    classified = classify_orphan_backups(root, live_ids=["90"])
    assert classified["safe_delete"] == []
    row = next(r for r in classified["retain"] if r["id"] == "91")
    assert row["category"] == "recovery_valuable"
    assert incomplete.exists()
    delete_orphan_backups(root, [], live_ids=["90"])
    assert incomplete.exists()


def test_refuses_delete_when_live_metadata_missing(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    live = root / "100"
    live.mkdir(parents=True)
    orphan = _bucket(root, "101", internal_id="x")
    result = delete_orphan_backups(
        root, ["101"], live_ids=["100"], live_pairs={"101": "100"}
    )
    assert result["deleted"] == 0
    assert result["skipped_incomplete"] == 1
    assert orphan.exists()


def test_refuses_to_delete_live_id(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    live = _bucket(root, "200", internal_id="x")
    result = delete_orphan_backups(root, ["200"], live_ids=["200"])
    assert result["deleted"] == 0
    assert result["skipped_live"] == 1
    assert live.exists()
