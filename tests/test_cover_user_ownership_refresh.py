"""User-owned cover must survive refresh / sidecar rescan.

Refresh used to copy a stale metadata.json cover_path (often an absolute path
into another library tree) into mods.cover_path, so Library projection showed
a missing or tiny preview while the live .info/cover.* file stayed correct.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.file_ops import COVER_BASENAME, INFO_DIR_NAME, METADATA_FILENAME, read_info_metadata_dict
from services.importers.image_picker import apply_cover_to_mod
from services.info_sidecar import apply_sidecar_to_db, load_info_sidecar, rescan_mod_folder
from services.metadata_ownership import FIELD_COVER
from tests.helpers.identity import create_steam_test_mod, patch_library_get_db, prove_managed_folder


def _unique_png(path: Path, marker: int) -> bytes:
    """Minimal unique RGB PNG (not the 16×16 forensic blue)."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    rows = []
    for _y in range(32):
        row = bytearray(1 + 32 * 3)
        row[0] = 0
        for x in range(32):
            row[1 + x * 3] = marker & 0xFF
            row[2 + x * 3] = 40
            row[3 + x * 3] = 180
        rows.append(bytes(row))
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 32, 32, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "cover_own.db")
    yield manager
    DatabaseManager.reset_instance()


def _seed_folder(tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    patch_library_get_db(monkeypatch, db)
    lib = tmp_path / "_smm_isolate_mod"
    folder = lib / "Game" / "CoverOwner"
    folder.mkdir(parents=True)
    (folder / "mod.pak").write_bytes(b"pak")
    created = create_steam_test_mod(db, external_id="88001122", title="CoverOwner")
    pk = prove_managed_folder(db, folder, handle=created.mod_id, title="CoverOwner")
    return folder, pk


def test_apply_cover_writes_relative_cover_not_stale_absolute(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk = _seed_folder(tmp_path, db, monkeypatch)
    info = folder / INFO_DIR_NAME
    stale = tmp_path / "foreign_tree" / "cover.jpg"
    _unique_png(stale, marker=1)
    meta = info / METADATA_FILENAME
    raw = json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {}
    raw["cover_path"] = str(stale.resolve())
    info.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    from services.metadata_cache import invalidate_metadata

    invalidate_metadata(folder)
    db.update_mod_cover_path(pk, "")

    probe = tmp_path / "user_cover.png"
    expected = _unique_png(probe, marker=9)
    rel = apply_cover_to_mod(folder, probe, mod_id=pk, update_db=True, sync_backup=False)
    assert rel == f"{INFO_DIR_NAME}/{COVER_BASENAME}.png"
    live = folder / INFO_DIR_NAME / f"{COVER_BASENAME}.png"
    assert live.is_file()
    assert live.read_bytes() == expected
    data = read_info_metadata_dict(folder) or {}
    assert str(data.get("cover_path") or "").replace("\\", "/") == f"{INFO_DIR_NAME}/{COVER_BASENAME}.png"
    row = db.get_mod_display_info(pk)
    assert row is not None
    assert str(row.cover_path or "").replace("\\", "/") == f"{INFO_DIR_NAME}/{COVER_BASENAME}.png"
    assert db.get_user_override_fields(pk).get(FIELD_COVER) is True


def test_rescan_does_not_clobber_user_cover_with_stale_sidecar(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk = _seed_folder(tmp_path, db, monkeypatch)
    probe = tmp_path / "user_cover.png"
    expected = _unique_png(probe, marker=11)
    apply_cover_to_mod(folder, probe, mod_id=pk, update_db=True, sync_backup=False)
    sha_after_upload = _sha(folder / INFO_DIR_NAME / f"{COVER_BASENAME}.png")
    assert sha_after_upload == hashlib.sha256(expected).hexdigest()

    poison = tmp_path / "other_lib" / "mod" / ".info" / "cover.jpg"
    tiny = _unique_png(poison, marker=2)
    meta = folder / INFO_DIR_NAME / METADATA_FILENAME
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["cover_path"] = str(poison.resolve())
    meta.write_text(json.dumps(data, indent=2), encoding="utf-8")
    from services.metadata_cache import invalidate_metadata

    invalidate_metadata(folder)

    sidecar = load_info_sidecar(folder)
    assert sidecar is not None
    assert str(sidecar.cover_path) == str(poison.resolve())
    apply_sidecar_to_db(folder, mod_id=pk, db=db, rescan_archives=False)
    row = db.get_mod_display_info(pk)
    assert row is not None
    assert str(row.cover_path or "").replace("\\", "/") == f"{INFO_DIR_NAME}/{COVER_BASENAME}.png"
    live = folder / INFO_DIR_NAME / f"{COVER_BASENAME}.png"
    assert live.read_bytes() == expected
    assert hashlib.sha256(tiny).hexdigest() != sha_after_upload

    rescan_mod_folder(folder, mod_id=pk, db=db)
    live2 = folder / INFO_DIR_NAME / f"{COVER_BASENAME}.png"
    assert _sha(live2) == sha_after_upload
    row2 = db.get_mod_display_info(pk)
    assert row2 is not None
    assert str(row2.cover_path or "").replace("\\", "/") == f"{INFO_DIR_NAME}/{COVER_BASENAME}.png"


def test_refresh_does_not_download_over_user_cover(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk = _seed_folder(tmp_path, db, monkeypatch)
    probe = tmp_path / "user_cover.png"
    expected = _unique_png(probe, marker=13)
    apply_cover_to_mod(folder, probe, mod_id=pk, update_db=True, sync_backup=False)
    sha_before = _sha(folder / INFO_DIR_NAME / f"{COVER_BASENAME}.png")
    db.set_official_metadata_synced(pk, True)

    from services.mod_refresh import refresh_mod

    result = refresh_mod(pk, folder, platform="steam", library_root=folder.parents[1], db=db)
    assert result.success
    live = folder / INFO_DIR_NAME / f"{COVER_BASENAME}.png"
    assert _sha(live) == sha_before
    assert live.read_bytes() == expected
    row = db.get_mod_display_info(pk)
    assert row is not None
    cover_ref = str(row.cover_path or "").replace("\\", "/")
    assert cover_ref == f"{INFO_DIR_NAME}/{COVER_BASENAME}.png"
