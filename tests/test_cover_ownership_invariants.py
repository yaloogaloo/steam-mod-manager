"""Cover ownership invariants — user cover, foreign pointer, official fill."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME
from services.info_sidecar import apply_sidecar_to_db
from services.metadata_cache import invalidate_metadata
from services.metadata_ownership import (
    FIELD_COVER,
    cover_reference_is_foreign,
    should_apply_official_field,
)
from tests.helpers.identity import create_steam_test_mod, patch_library_get_db, prove_managed_folder


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "cover_inv.db")
    yield manager
    DatabaseManager.reset_instance()


def _folder(tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    patch_library_get_db(monkeypatch, db)
    folder = tmp_path / "_smm_isolate_mod" / "Game" / "Owned"
    folder.mkdir(parents=True)
    (folder / "mod.pak").write_bytes(b"pak")
    created = create_steam_test_mod(db, external_id="77001", title="Owned")
    pk = prove_managed_folder(db, folder, handle=created.mod_id, title="Owned")
    return folder, pk


def _write_cover_ref(folder: Path, ref: str) -> None:
    meta = folder / INFO_DIR_NAME / METADATA_FILENAME
    data = json.loads(meta.read_text(encoding="utf-8")) if meta.is_file() else {}
    data["cover_path"] = ref
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps(data), encoding="utf-8")
    invalidate_metadata(folder)


def test_relative_cover_is_managed_and_other_tree_is_foreign(tmp_path: Path) -> None:
    folder = tmp_path / "mod"
    folder.mkdir()
    assert cover_reference_is_foreign(folder, f"{INFO_DIR_NAME}/cover.png") is False
    inside = folder / INFO_DIR_NAME / "cover.png"
    assert cover_reference_is_foreign(folder, str(inside)) is False
    foreign = tmp_path / "other-tree" / ".info" / "cover.jpg"
    assert cover_reference_is_foreign(folder, str(foreign)) is True


def test_user_override_blocks_stale_sidecar_pointer(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk = _folder(tmp_path, db, monkeypatch)
    db.update_mod_cover_path(pk, f"{INFO_DIR_NAME}/cover.png")
    db.set_user_override_field(pk, FIELD_COVER, overridden=True)
    foreign = tmp_path / "other-tree" / ".info" / "cover.jpg"
    _write_cover_ref(folder, str(foreign))
    apply_sidecar_to_db(folder, mod_id=pk, db=db, rescan_archives=False)
    row = db.get_mod_display_info(pk)
    assert row is not None
    assert str(row.cover_path or "").replace("\\", "/") == f"{INFO_DIR_NAME}/cover.png"
    assert db.get_user_override_fields(pk).get(FIELD_COVER) is True


def test_foreign_absolute_is_rejected_even_without_override(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk = _folder(tmp_path, db, monkeypatch)
    db.update_mod_cover_path(pk, f"{INFO_DIR_NAME}/cover.png")
    foreign = tmp_path / "another-tree" / ".info" / "cover.jpg"
    _write_cover_ref(folder, str(foreign))
    apply_sidecar_to_db(folder, mod_id=pk, db=db, rescan_archives=False)
    row = db.get_mod_display_info(pk)
    assert row is not None
    assert str(row.cover_path or "").replace("\\", "/") == f"{INFO_DIR_NAME}/cover.png"


def test_relative_official_cover_still_applies_without_override(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, pk = _folder(tmp_path, db, monkeypatch)
    db.update_mod_cover_path(pk, "")
    assert should_apply_official_field(FIELD_COVER, overrides={}, local_value="") is True
    _write_cover_ref(folder, f"{INFO_DIR_NAME}/cover.jpg")
    apply_sidecar_to_db(folder, mod_id=pk, db=db, rescan_archives=False)
    row = db.get_mod_display_info(pk)
    assert row is not None
    assert str(row.cover_path or "").replace("\\", "/") == f"{INFO_DIR_NAME}/cover.jpg"
    assert should_apply_official_field(
        FIELD_COVER, overrides={FIELD_COVER: True}, local_value=""
    ) is False
    assert should_apply_official_field(
        FIELD_COVER, overrides={}, local_value=f"{INFO_DIR_NAME}/cover.png"
    ) is False
