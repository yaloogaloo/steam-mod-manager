""".info/internal_id filesystem Entity proof — same value as mods.internal_id."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from services.file_ops import INFO_DIR_NAME, METADATA_FILENAME, persist_unified_metadata_dict
from services.mod_identity import (
    INTERNAL_ID_KEY,
    LEGACY_ENTITY_KEY,
    ensure_mod_identity,
    normalize_info_identity_payload,
    read_internal_id,
    set_info_internal_id,
)
from tests.helpers.identity import create_steam_test_mod, write_info_sidecar


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "info_internal_id.db")
    yield manager
    DatabaseManager.reset_instance()


def test_set_info_internal_id_never_writes_entity_key() -> None:
    payload = set_info_internal_id(
        {"title": "T", LEGACY_ENTITY_KEY: "old"}, "uuid-1"
    )
    assert payload[INTERNAL_ID_KEY] == "uuid-1"
    assert LEGACY_ENTITY_KEY not in payload


def test_read_internal_id_prefers_canonical_over_legacy_entity_key() -> None:
    assert (
        read_internal_id(
            {INTERNAL_ID_KEY: "modern", LEGACY_ENTITY_KEY: "legacy"}
        )
        == "modern"
    )
    assert read_internal_id({LEGACY_ENTITY_KEY: "legacy"}) == "legacy"


def test_normalize_migrates_legacy_entity_key_without_regenerating() -> None:
    frozen = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    out, changed = normalize_info_identity_payload(
        {"title": "T", LEGACY_ENTITY_KEY: frozen, "workspace_id": "99"}
    )
    assert changed is True
    assert out[INTERNAL_ID_KEY] == frozen
    assert LEGACY_ENTITY_KEY not in out
    assert out["workspace_id"] == "99"


def test_write_info_sidecar_emits_internal_id_only(tmp_path: Path) -> None:
    folder = tmp_path / "Mod"
    write_info_sidecar(folder, internal_id="uuid-proof", title="T", external_id="1")
    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert data[INTERNAL_ID_KEY] == "uuid-proof"
    assert LEGACY_ENTITY_KEY not in data


def test_persist_strips_legacy_entity_key(tmp_path: Path) -> None:
    folder = tmp_path / "Mod"
    folder.mkdir()
    persist_unified_metadata_dict(
        folder,
        {"title": "T", LEGACY_ENTITY_KEY: "uuid-x", "workspace_id": "7"},
        sync_backup=False,
    )
    data = json.loads(
        (folder / INFO_DIR_NAME / METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert data[INTERNAL_ID_KEY] == "uuid-x"
    assert LEGACY_ENTITY_KEY not in data


def test_legacy_entity_key_migrates_on_bind(
    db: DatabaseManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)
    db.upsert_game(GameInfo(app_id=1, name="G", folder_name="G"))
    created = create_steam_test_mod(
        db, external_id="55001", title="LegacyKey", app_id=1, game_name="G"
    )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "")
    ws = str(created.workspace_id or "55001")
    folder = tmp_path / "G" / "LegacyKey"
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True)
    (info / METADATA_FILENAME).write_text(
        json.dumps(
            {
                LEGACY_ENTITY_KEY: frozen,
                "workspace_id": ws,
                "title": "LegacyKey",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    db.update_mod_identity_fields(pk, last_known_path=str(folder), folder_present=True)

    bound, _payload, changed = ensure_mod_identity(folder, db=db)
    assert bound == pk
    assert changed is True
    disk = json.loads((info / METADATA_FILENAME).read_text(encoding="utf-8"))
    assert disk[INTERNAL_ID_KEY] == frozen
    assert LEGACY_ENTITY_KEY not in disk
    row = db.get_mod_backup_row(pk) or {}
    assert str(row.get("internal_id") or "") == frozen
    assert str(row.get("workspace_id") or "") == ws
    assert str(pk).isdigit()  # mods.mod_id unchanged as DB PK
    assert frozen != pk
