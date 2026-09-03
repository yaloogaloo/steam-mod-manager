"""has_local_mod_payload — first-hit short-circuit (P0-4 Optimization A)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.local_file_index import _iter_loose_files, has_local_mod_payload


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "payload.db")
    yield manager
    DatabaseManager.reset_instance()


def _write_info_only(folder: Path, *, mid: str = "990001") -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                "published_file_id": mid,
                "title": folder.name,
                "workspace_id": f"ws-{mid}",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (info / "cover.png").write_bytes(b"\x89PNG" + b"x" * 16)
    offline = info / "offline"
    offline.mkdir()
    (offline / "index.html").write_text("<html></html>", encoding="utf-8")


def test_case_a_empty_mod_is_not_payload(db: DatabaseManager, tmp_path: Path) -> None:
    folder = tmp_path / "Game" / "EmptyMod"
    folder.mkdir(parents=True)
    _write_info_only(folder)
    assert has_local_mod_payload(folder, mod_id="990001", db=db) is False
    assert list(_iter_loose_files(folder)) == []


def test_case_b_stops_after_first_payload(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import services.local_file_index as lfi

    folder = tmp_path / "Game" / "ManyFiles"
    folder.mkdir(parents=True)
    _write_info_only(folder, mid="990002")
    for name in ("a.dat", "b.dat", "c.dat", "d.dat"):
        (folder / name).write_bytes(b"payload")
    nested = folder / "deep" / "tree"
    nested.mkdir(parents=True)
    (nested / "late.dat").write_bytes(b"late")

    yielded = {"n": 0}
    real = lfi._iter_loose_files

    def counting(managed: Path):
        for path in real(managed):
            yielded["n"] += 1
            yield path

    monkeypatch.setattr(lfi, "_iter_loose_files", counting)
    assert has_local_mod_payload(folder, mod_id="990002", db=db) is True
    assert yielded["n"] == 1


def test_case_c_info_metadata_cover_offline_are_not_payload(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "Game" / "MetaOnly"
    folder.mkdir(parents=True)
    _write_info_only(folder, mid="990003")
    assert has_local_mod_payload(folder, mod_id="990003", db=db) is False
    loose = list(_iter_loose_files(folder))
    assert loose == []
    assert not any(INFO_DIR_NAME in p.parts for p in loose)


def test_case_d_small_mod_with_pak_is_payload(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "GameX" / "ModA"
    folder.mkdir(parents=True)
    _write_info_only(folder, mid="960001")
    (folder / "content.pak").write_bytes(b"pak")
    assert has_local_mod_payload(folder, mod_id="960001", db=db) is True
    assert any(p.name == "content.pak" for p in _iter_loose_files(folder))


def test_root_archive_still_counts_as_payload(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder = tmp_path / "Game" / "ZipOnly"
    folder.mkdir(parents=True)
    _write_info_only(folder, mid="990004")
    (folder / "mod.zip").write_bytes(b"PK\x03\x04not-a-real-zip")
    assert list(_iter_loose_files(folder)) == []
    assert has_local_mod_payload(folder, mod_id="990004", db=db) is True
