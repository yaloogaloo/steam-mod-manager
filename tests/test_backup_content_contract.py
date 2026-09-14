"""Backup content contract — only metadata / cover / offline may be stored.

Forbidden in ``data/mod_backup/<mod_id>/``:
- Mod payload files (.pak, .zip, .rar, workshop dumps, game files)
- hash caches, deploy caches, temporary scan data
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.models import ModMetadata
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.metadata_backup import BACKUP_METADATA_NAME, backup_root
from services.metadata_backup_sync import drain_backup_queue, sync_after_metadata_change
from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

FORBIDDEN_SUFFIXES = (
    ".pak",
    ".zip",
    ".rar",
    ".7z",
    ".dll",
    ".exe",
    ".bin",
)
FORBIDDEN_NAME_FRAGMENTS = (
    "hash_cache",
    "deploy_cache",
    "workshop",
    "payload",
    "scan_tmp",
    "temp_scan",
)


def _assert_backup_tree_allowed(dest: Path) -> None:
    assert dest.is_dir()
    for path in dest.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(dest).as_posix().lower()
        name = path.name.lower()
        for frag in FORBIDDEN_NAME_FRAGMENTS:
            assert frag not in rel, f"forbidden backup path fragment {frag}: {rel}"
        for suf in FORBIDDEN_SUFFIXES:
            if rel.startswith("offline/") and suf == ".bin":
                continue
            assert not name.endswith(suf), f"forbidden backup suffix {suf}: {rel}"
        # Allowed roots: metadata.json, cover.*, offline/**
        if name == BACKUP_METADATA_NAME.lower():
            continue
        if name.startswith("cover."):
            continue
        if rel == "offline/index.html" or rel.startswith("offline/"):
            continue
        raise AssertionError(
            f"unexpected backup file (not metadata/cover/offline): {rel}"
        )


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DatabaseManager:
    DatabaseManager.reset_instance()
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    manager = DatabaseManager.instance(tmp_path / "backup_contract.db")
    yield manager
    DatabaseManager.reset_instance()


def test_backup_content_contract_allows_only_metadata_cover_offline(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)

    library = tmp_path / "mod"
    folder = library / "GameA" / "ContractMod"
    folder.mkdir(parents=True)
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / "cover.jpg").write_bytes(b"\xff\xd8\xff" + b"cover" * 20)
    offline = info / "offline"
    offline.mkdir()
    (offline / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    assets = offline / "assets"
    assets.mkdir()
    (assets / "all.css").write_text("body{}", encoding="utf-8")
    (assets / "site.woff").write_bytes(b"WOFF")
    (assets / "track.blink").write_bytes(b"blink")
    (assets / "huge.gif").write_bytes(b"GIF89a" + b"x" * 64)
    # Payload that must NEVER be copied into backup.
    (folder / "mod.pak").write_bytes(b"FAKEPAK")
    (folder / "archive.zip").write_bytes(b"FAKEZIP")
    (folder / "payload.bin").write_bytes(b"x" * 64)

    created = create_steam_test_mod(
        db, external_id="960001", title="Contract Mod", game_name="GameA"
    )
    pk = str(created.mod_id)
    prove_managed_folder(
        db,
        folder,
        handle=pk,
        title="Contract Mod",
        game_name="GameA",
        extra={"user_notes": "keep me"},
    )

    assert sync_after_metadata_change(pk, folder, "import")
    drain_backup_queue(timeout=5.0)

    dest = backup_root(pk)
    assert (dest / BACKUP_METADATA_NAME).is_file()
    assert list(dest.glob("cover.*"))
    assert (dest / "offline" / "index.html").is_file()
    assert not (dest / "offline" / "assets").exists()
    assert not (dest / "mod.pak").exists()
    assert not (dest / "archive.zip").exists()
    assert not (dest / "payload.bin").exists()
    _assert_backup_tree_allowed(dest)


def test_backup_content_contract_rejects_forbidden_if_planted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr("services.metadata_backup.data_dir", lambda: root)
    dest = backup_root("960099")
    dest.mkdir(parents=True)
    (dest / BACKUP_METADATA_NAME).write_text("{}", encoding="utf-8")
    (dest / "evil.pak").write_bytes(b"bad")
    with pytest.raises(AssertionError, match="forbidden backup suffix"):
        _assert_backup_tree_allowed(dest)
