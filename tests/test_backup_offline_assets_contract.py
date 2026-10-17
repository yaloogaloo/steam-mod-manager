"""Backup snapshot must not copy webpage assets; cleanup pairing is conservative."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.backup_storage_cleanup import (
    classify_import_cache_leftovers,
    classify_orphan_backups,
    delete_empty_quarantine_dirs,
    delete_import_cache_leftovers,
    delete_orphan_backups,
    strip_live_backup_offline_assets,
)
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.metadata_backup import BACKUP_METADATA_NAME, backup_root, snapshot_from_mod_folder
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "backup_assets.db")
    yield manager
    DatabaseManager.reset_instance()


@pytest.fixture()
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    return root


def _mod_folder(library: Path, *, title: str = "AssetMod") -> Path:
    folder = library / "GameA" / title
    folder.mkdir(parents=True)
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "cover.jpg").write_bytes(b"\xff\xd8\xff" + b"cover" * 12)
    offline = info / "offline"
    assets = offline / "assets"
    assets.mkdir(parents=True)
    (offline / "index.html").write_text("<html>offline-body</html>", encoding="utf-8")
    (assets / "all.css").write_text("body{color:red}", encoding="utf-8")
    (assets / "font.woff").write_bytes(b"WOFFDATA")
    (assets / "anim.gif").write_bytes(b"GIF89a" + b"g" * 80)
    (folder / "payload.bin").write_bytes(b"mod-payload")
    return folder


def _seed_asset_mod(db: DatabaseManager, library: Path) -> tuple[Path, str]:
    folder = _mod_folder(library)
    created = create_steam_test_mod(
        db, external_id="970001", title="AssetMod", game_name="GameA"
    )
    pk = str(created.mod_id)
    prove_managed_folder(
        db,
        folder,
        handle=pk,
        title="AssetMod",
        game_name="GameA",
        extra={
            "source_type": "steam",
            "source_url": "https://example.com/mod",
        },
    )
    return folder, pk


def test_snapshot_keeps_index_and_excludes_assets(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk = _seed_asset_mod(db, library)

    snap = snapshot_from_mod_folder(folder, owner_mod_id=pk)
    assert snap is not None
    dest = backup_root(pk)
    assert (dest / BACKUP_METADATA_NAME).is_file()
    assert list(dest.glob("cover.*"))
    assert (dest / "offline" / "index.html").is_file()
    assert "offline-body" in (dest / "offline" / "index.html").read_text(encoding="utf-8")
    assert not (dest / "offline" / "assets").exists()
    assert (folder / INFO_DIR_NAME / "offline" / "assets" / "all.css").is_file()


def test_second_snapshot_strips_leftover_assets(
    db: DatabaseManager, data_root: Path, tmp_path: Path
) -> None:
    library = tmp_path / "mod"
    folder, pk = _seed_asset_mod(db, library)
    dest = backup_root(pk)
    leftover = dest / "offline" / "assets"
    leftover.mkdir(parents=True)
    (leftover / "old.css").write_text("stale", encoding="utf-8")
    snapshot_from_mod_folder(folder, owner_mod_id=pk)
    assert (dest / "offline" / "index.html").is_file()
    assert not leftover.exists()


def test_orphan_duplicate_is_safe_to_delete(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    live = root / "1336"
    orphan = root / "9000000000003299"
    other = root / "999"
    for bucket in (live, orphan):
        bucket.mkdir(parents=True)
        (bucket / "metadata.json").write_text(
            json.dumps({"internal_id": "same-uuid", "title": "Dup"}),
            encoding="utf-8",
        )
        (bucket / "cover.jpg").write_bytes(b"COVERBYTES")
        offline = bucket / "offline"
        offline.mkdir()
        (offline / "index.html").write_text("<html>x</html>", encoding="utf-8")
        assets = offline / "assets"
        assets.mkdir()
        (assets / "all.css").write_text("body{}", encoding="utf-8")
    other.mkdir()
    (other / "metadata.json").write_text(
        json.dumps({"internal_id": "other-uuid", "title": "Unique"}),
        encoding="utf-8",
    )
    classified = classify_orphan_backups(root, live_ids=["1336"])
    safe_ids = {row["id"] for row in classified["safe_delete"]}
    retain_ids = {row["id"] for row in classified["retain"]}
    assert "9000000000003299" in safe_ids
    assert "999" in retain_ids
    result = delete_orphan_backups(
        root, ["9000000000003299"], live_ids=["1336"]
    )
    assert result["deleted"] == 1
    assert not orphan.exists()
    assert live.exists()
    assert other.exists()


def test_orphan_same_internal_id_is_safe_even_if_json_differs(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    live = root / "465"
    orphan = root / "3681020076"
    live.mkdir(parents=True)
    orphan.mkdir(parents=True)
    (live / "metadata.json").write_text(
        json.dumps({"internal_id": "same-uuid", "title": "Live", "path": "a"}),
        encoding="utf-8",
    )
    (orphan / "metadata.json").write_text(
        json.dumps({"internal_id": "same-uuid", "title": "Live", "path": "b"}),
        encoding="utf-8",
    )
    (live / "cover.jpg").write_bytes(b"COVERBYTES")
    (orphan / "cover.jpg").write_bytes(b"COVERBYTES")
    classified = classify_orphan_backups(root, live_ids=["465"])
    assert {row["id"] for row in classified["safe_delete"]} == {"3681020076"}


def test_orphan_without_live_match_is_retained(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    live = root / "10"
    orphan = root / "20"
    live.mkdir(parents=True)
    orphan.mkdir(parents=True)
    (live / "metadata.json").write_text('{"internal_id":"a","title":"A"}', encoding="utf-8")
    (orphan / "metadata.json").write_text('{"internal_id":"b","title":"B"}', encoding="utf-8")
    classified = classify_orphan_backups(root, live_ids=["10"])
    assert classified["safe_delete"] == []
    assert classified["retain"][0]["id"] == "20"


def test_orphan_unique_cover_is_retained(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    live = root / "11"
    orphan = root / "22"
    live.mkdir(parents=True)
    orphan.mkdir(parents=True)
    payload = json.dumps({"internal_id": "same", "title": "T"})
    (live / "metadata.json").write_text(payload, encoding="utf-8")
    (orphan / "metadata.json").write_text(payload, encoding="utf-8")
    (orphan / "cover.jpg").write_bytes(b"ONLY-ORPHAN-COVER")
    classified = classify_orphan_backups(root, live_ids=["11"])
    assert classified["safe_delete"] == []
    assert classified["retain"][0]["id"] == "22"


def test_strip_live_assets_leaves_index_and_keeps_referenced_css(tmp_path: Path) -> None:
    root = tmp_path / "mod_backup"
    live = root / "50"
    assets = live / "offline" / "assets"
    assets.mkdir(parents=True)
    (live / "offline" / "index.html").write_text(
        '<html><link href="./assets/keep.css" rel="stylesheet"></html>',
        encoding="utf-8",
    )
    (assets / "keep.css").write_text("body{}", encoding="utf-8")
    (assets / "all.css").write_text("x", encoding="utf-8")
    stats = strip_live_backup_offline_assets(root, ["50"])
    assert stats["files"] >= 1
    assert (live / "offline" / "index.html").is_file()
    assert (assets / "keep.css").is_file()
    assert not (assets / "all.css").exists()


def test_empty_quarantine_dirs_deleted_nonempty_kept(tmp_path: Path) -> None:
    q = tmp_path / "identity_repair_quarantine"
    empty = q / "2026-09-08T035010+0000"
    empty.mkdir(parents=True)
    keep = q / "2026-09-02T101049+0000"
    (keep / "invalid_1").mkdir(parents=True)
    (keep / "invalid_1" / "note.txt").write_text("keep", encoding="utf-8")
    result = delete_empty_quarantine_dirs(q)
    assert "2026-09-08T035010+0000" in result["deleted"]
    assert "2026-09-02T101049+0000" in result["retained"]
    assert not empty.exists()
    assert keep.exists()


def test_import_cache_leftover_classification(tmp_path: Path) -> None:
    cache = tmp_path / "import_cache"
    test_tree = cache / "aaa-uuid"
    (test_tree / "ModName" / "Optional").mkdir(parents=True)
    (test_tree / "ModName" / "test.pak").write_bytes(b"x" * 4)
    (test_tree / "ModName" / "Optional" / "hat.pak").write_bytes(b"y")
    trace = cache / "_trace_rar_test"
    trace.mkdir()
    (trace / "ex" / "ArmoryCHS").mkdir(parents=True)
    (trace / "ex" / "ArmoryCHS" / "ArmoryCHS.pak").write_bytes(b"pak")
    deploy = cache / "deploy_deadbeef"
    deploy.mkdir()
    (deploy / "content").mkdir()
    (deploy / "content" / "x.pak").write_bytes(b"z")
    stub = cache / "tiny-uuid"
    stub.mkdir()
    (stub / "x").write_bytes(b"ab")
    keep = cache / "_modio_live_verify"
    keep.mkdir()
    (keep / "real.bin").write_bytes(b"keepme" * 20)
    rar_a = cache / "rar-one"
    rar_b = cache / "rar-two"
    rar_a.mkdir()
    rar_b.mkdir()
    payload = b"RAR" + b"d" * 100
    (rar_a / "game.rar").write_bytes(payload)
    (rar_b / "game.rar").write_bytes(payload)
    classified = classify_import_cache_leftovers(cache)
    names = {row["name"] for row in classified["safe_delete"]}
    retain = {row["name"] for row in classified["retain"]}
    assert "aaa-uuid" in names
    assert "_trace_rar_test" in names
    assert "deploy_deadbeef" in names
    assert "tiny-uuid" in names
    assert "rar-one" in names
    assert "rar-two" in names
    assert "_modio_live_verify" in retain
    result = delete_import_cache_leftovers(cache, names)
    assert result["deleted"] >= 6
    assert keep.exists()
    assert not test_tree.exists()
    assert not trace.exists()
