"""BG3 Stage 2: membership, canonical internal_id order, resolver, projection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager
from core.game_info import GameInfo
from services.bg3_activation import (
    BG3_APP_ID,
    apply_card_drop,
    apply_order_move,
    inspect_bg3_membership,
    load_saved_order,
    persist_load_order,
    resolve_uuid_sequence,
    resolved_load_order,
)
from services.bg3_modsettings import GUSTAVX_UUID, parse_bg3_modsettings
from services.bg3_pak import Bg3PakError
from services.deploy_identity import is_frozen_internal_uuid
from services.file_ops import INFO_DIR_NAME
from services.paradox_activation import (
    ORDER_MOVE_BOTTOM,
    ORDER_MOVE_DOWN,
    ORDER_MOVE_TOP,
    ORDER_MOVE_UP,
)
from tests.helpers.bg3_lspk import meta_lsx_bytes, write_lspk_v18
from tests.helpers.identity import (
    create_other_test_mod,
    write_info_sidecar,
)

UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
UUID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"
UUID_C = "cccccccc-cccc-cccc-cccc-ccccccccccc3"
UUID_UNMANAGED = "396c5966-09b0-40a1-af3f-93a5e9ce71c0"
UUID_STALE = "dddddddd-dddd-dddd-dddd-ddddddddddd4"


def _short(uuid: str, *, folder: str, name: str) -> str:
    return (
        '                        <node id="ModuleShortDesc">\n'
        f'                            <attribute id="Folder" type="LSString" value="{folder}"/>\n'
        '                            <attribute id="MD5" type="LSString" value=""/>\n'
        f'                            <attribute id="Name" type="LSString" value="{name}"/>\n'
        '                            <attribute id="PublishHandle" type="uint64" value="0"/>\n'
        f'                            <attribute id="UUID" type="guid" value="{uuid}"/>\n'
        '                            <attribute id="Version64" type="int64" value="1"/>\n'
        "                        </node>"
    )


def _lsx(*nodes: str) -> str:
    body = "\n".join(nodes)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<save>\n"
        '    <version major="4" minor="8" revision="0" build="700"/>\n'
        '    <region id="ModuleSettings">\n'
        '        <node id="root">\n'
        "            <children>\n"
        '                <node id="Mods">\n'
        "                    <children>\n"
        f"{body}\n"
        "                    </children>\n"
        "                </node>\n"
        "            </children>\n"
        "        </node>\n"
        "    </region>\n"
        "</save>\n"
    )


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "bg3_stage2.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture()
def bg3_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    order = tmp_path / "bg3.json"
    lsx = tmp_path / "modsettings.lsx"
    monkeypatch.setenv("SMM_BG3_ORDER", str(order))
    monkeypatch.setenv("SMM_BG3_MODSETTINGS", str(lsx))
    return order, lsx


def _configure_game(db: DatabaseManager, tmp_path: Path) -> Path:
    library = tmp_path / "mod"
    db.upsert_game(
        GameInfo(app_id=BG3_APP_ID, name="Baldur's Gate 3", folder_name="博德之门Ⅲ")
    )
    db.update_game_deploy_config(
        BG3_APP_ID,
        name="Baldur's Gate 3",
        install_path=str(tmp_path / "BG3Install"),
        mod_path=str(tmp_path / "GameMods"),
    )
    return library


def _write_manifest(folder: Path, *, pk: str, iid: str, paks: list[Path]) -> None:
    info = folder / INFO_DIR_NAME
    info.mkdir(parents=True, exist_ok=True)
    payload = {
        "mod_id": pk,
        "internal_id": iid,
        "deploy_time": "2026-01-01T00:00:00",
        "deploy_type": "pak_mod_path",
        "files": [
            {"source": str(pak), "target": str(pak), "type": "pak"} for pak in paks
        ],
    }
    (info / "deploy_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _seed_bg3_mod(
    library: Path,
    db: DatabaseManager,
    tmp_path: Path,
    *,
    folder: str,
    uuid: str,
    name: str,
    deployed: bool = True,
    custom_deploy_path: str = "",
    pak_path: Path | None = None,
    write_pak: bool = True,
) -> tuple[str, str, Path]:
    created = create_other_test_mod(
        db,
        title=name,
        external_id=f"nexus-{folder}",
        app_id=BG3_APP_ID,
        game_name="Baldur's Gate 3",
    )
    pk = str(created.mod_id)
    iid = str(created.internal_id or "")
    mod_dir = library / "博德之门Ⅲ" / folder
    mod_dir.mkdir(parents=True, exist_ok=True)
    extra = {}
    if custom_deploy_path:
        extra["custom_deploy_path"] = custom_deploy_path
    write_info_sidecar(
        mod_dir,
        internal_id=iid,
        title=name,
        external_id=f"nexus-{folder}",
        workspace_id=str(created.workspace_id or ""),
        app_id=BG3_APP_ID,
        game_name="Baldur's Gate 3",
        extra=extra or None,
    )
    pak = pak_path or (tmp_path / "game_mods" / f"{folder}.pak")
    pak.parent.mkdir(parents=True, exist_ok=True)
    if write_pak:
        write_lspk_v18(
            pak,
            {f"Mods/{folder}/meta.lsx": meta_lsx_bytes(uuid=uuid, name=name, folder=folder)},
        )
    _write_manifest(mod_dir, pk=pk, iid=iid, paks=[pak])
    db.update_mod_identity_fields(pk, last_known_path=str(mod_dir), folder_present=True)
    if deployed:
        db.update_mod_deploy_status(
            pk, deploy_status=DEPLOY_STATUS_DEPLOYED, deploy_path=str(pak)
        )
    return pk, iid, pak


def _base_lsx() -> str:
    return _lsx(
        _short(GUSTAVX_UUID, folder="GustavX", name="GustavX"),
        _short(UUID_A, folder="ModA", name="A"),
        _short(UUID_UNMANAGED, folder="CommunityLibrary", name="CommunityLibrary"),
        _short(UUID_UNMANAGED, folder="CommunityLibrary", name="CommunityLibrary"),
        _short(UUID_B, folder="ModB", name="B"),
        _short(UUID_STALE, folder="Stale", name="Stale"),
    )


def test_canonical_json_created_from_modsettings(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    order_path, lsx_path = bg3_paths
    library = _configure_game(db, tmp_path)
    _pk_a, iid_a, _ = _seed_bg3_mod(
        library, db, tmp_path, folder="ModA", uuid=UUID_A, name="A"
    )
    _pk_b, iid_b, _ = _seed_bg3_mod(
        library, db, tmp_path, folder="ModB", uuid=UUID_B, name="B"
    )
    lsx_path.write_text(_base_lsx(), encoding="utf-8")
    assert not order_path.is_file()
    saved = persist_load_order(None, db)
    assert saved == [iid_a, iid_b]
    payload = json.loads(order_path.read_text(encoding="utf-8"))
    assert list(payload.keys()) == ["order"]
    assert payload["order"] == [iid_a, iid_b]
    assert _pk_a not in payload["order"]
    assert _pk_b not in payload["order"]
    blob = json.dumps(payload)
    assert UUID_A not in blob
    assert UUID_B not in blob
    assert "workspace_id" not in blob
    assert all(is_frozen_internal_uuid(item) for item in payload["order"])


def test_order_move_persists_internal_id(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    order_path, lsx_path = bg3_paths
    library = _configure_game(db, tmp_path)
    _, iid_a, _ = _seed_bg3_mod(library, db, tmp_path, folder="ModA", uuid=UUID_A, name="A")
    _, iid_b, _ = _seed_bg3_mod(library, db, tmp_path, folder="ModB", uuid=UUID_B, name="B")
    lsx_path.write_text(_base_lsx(), encoding="utf-8")
    persist_load_order(None, db)
    apply_order_move(iid_a, ORDER_MOVE_BOTTOM, db)
    assert load_saved_order() == [iid_b, iid_a]
    payload = json.loads(order_path.read_text(encoding="utf-8"))
    assert payload["order"] == [iid_b, iid_a]
    _, nodes = parse_bg3_modsettings(lsx_path.read_text(encoding="utf-8"))
    uuids = [n.uuid for n in nodes]
    assert uuids[0] == GUSTAVX_UUID
    assert uuids.index(UUID_B) < uuids.index(UUID_A)


def test_internal_id_resolves_via_manifest_pak(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    _order_path, lsx_path = bg3_paths
    library = _configure_game(db, tmp_path)
    _, iid_a, pak = _seed_bg3_mod(
        library, db, tmp_path, folder="ModA", uuid=UUID_A, name="A"
    )
    lsx_path.write_text(
        _lsx(_short(GUSTAVX_UUID, folder="GustavX", name="GustavX")),
        encoding="utf-8",
    )
    persist_load_order([iid_a], db)
    uuids, nodes = resolve_uuid_sequence([iid_a], db)
    assert uuids == [UUID_A]
    assert nodes[UUID_A].uuid == UUID_A
    assert Path(inspect_bg3_membership(db).sortable[0].pak_paths[0]) == pak


def test_missing_pak_fail_closed(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    _order, lsx_path = bg3_paths
    library = _configure_game(db, tmp_path)
    _, iid, _ = _seed_bg3_mod(
        library,
        db,
        tmp_path,
        folder="Gone",
        uuid=UUID_A,
        name="Gone",
        write_pak=False,
    )
    lsx_path.write_text(
        _lsx(_short(GUSTAVX_UUID, folder="GustavX", name="GustavX")),
        encoding="utf-8",
    )
    report = inspect_bg3_membership(db)
    assert iid not in [m.internal_id for m in report.sortable]
    assert any(item.internal_id == iid and item.code == "MissingPak" for item in report.unresolved)


def test_unsupported_compression_is_structured_unresolved(
    tmp_path: Path,
    db: DatabaseManager,
    bg3_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _order, lsx_path = bg3_paths
    library = _configure_game(db, tmp_path)
    _, iid, pak = _seed_bg3_mod(
        library, db, tmp_path, folder="ZstdMod", uuid=UUID_C, name="Zstd"
    )
    lsx_path.write_text(
        _lsx(_short(GUSTAVX_UUID, folder="GustavX", name="GustavX")),
        encoding="utf-8",
    )

    def _boom(path, **_kwargs):
        raise Bg3PakError(
            "UnsupportedCompression",
            "zstd is not available",
            method=3,
            path=str(path),
        )

    monkeypatch.setattr("services.bg3_activation.resolve_bg3_mod_metadata", _boom)
    report = inspect_bg3_membership(db)
    assert iid not in [m.internal_id for m in report.sortable]
    hits = [item for item in report.unresolved if item.internal_id == iid]
    assert hits and hits[0].code == "UnsupportedCompression"
    assert hits[0].fields.get("method") == 3
    del pak


def test_duplicate_uuid_fail_closed(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    _order, lsx_path = bg3_paths
    library = _configure_game(db, tmp_path)
    _, iid_a, _ = _seed_bg3_mod(
        library, db, tmp_path, folder="DupA", uuid=UUID_A, name="DupA"
    )
    _, iid_b, _ = _seed_bg3_mod(
        library, db, tmp_path, folder="DupB", uuid=UUID_A, name="DupB"
    )
    lsx_path.write_text(
        _lsx(_short(GUSTAVX_UUID, folder="GustavX", name="GustavX")),
        encoding="utf-8",
    )
    report = inspect_bg3_membership(db)
    ids = {m.internal_id for m in report.sortable}
    assert iid_a not in ids
    assert iid_b not in ids
    assert any(item.code == "DuplicateUuid" for item in report.unresolved)


def test_lsx_duplicate_managed_uuid_is_not_sortable(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    _order, lsx_path = bg3_paths
    library = _configure_game(db, tmp_path)
    _, iid_a, _ = _seed_bg3_mod(library, db, tmp_path, folder="ModA", uuid=UUID_A, name="A")
    _, iid_b, _ = _seed_bg3_mod(library, db, tmp_path, folder="ModB", uuid=UUID_B, name="B")
    lsx_path.write_text(
        _lsx(
            _short(GUSTAVX_UUID, folder="GustavX", name="GustavX"),
            _short(UUID_A, folder="ModA", name="A"),
            _short(UUID_A, folder="ModA", name="A"),
            _short(UUID_B, folder="ModB", name="B"),
        ),
        encoding="utf-8",
    )
    report = inspect_bg3_membership(db)
    ids = {m.internal_id for m in report.sortable}
    assert iid_a not in ids
    assert iid_b in ids
    persist_load_order(None, db)
    apply_order_move(iid_b, ORDER_MOVE_TOP, db)
    _, nodes = parse_bg3_modsettings(lsx_path.read_text(encoding="utf-8"))
    uuids = [n.uuid for n in nodes]
    assert uuids.count(UUID_A) == 2
    assert uuids[0] == GUSTAVX_UUID


def test_manifest_without_matching_internal_id_still_sortable(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    _order, lsx_path = bg3_paths
    library = _configure_game(db, tmp_path)
    pk, iid, pak = _seed_bg3_mod(
        library, db, tmp_path, folder="ModA", uuid=UUID_A, name="A"
    )
    mod_dir = library / "博德之门Ⅲ" / "ModA"
    _write_manifest(mod_dir, pk=pk, iid=pk, paks=[pak])
    lsx_path.write_text(
        _lsx(_short(GUSTAVX_UUID, folder="GustavX", name="GustavX")),
        encoding="utf-8",
    )
    report = inspect_bg3_membership(db)
    assert iid in [m.internal_id for m in report.sortable]


def test_custom_deploy_excluded(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    _order, lsx_path = bg3_paths
    library = _configure_game(db, tmp_path)
    _, iid, _ = _seed_bg3_mod(
        library,
        db,
        tmp_path,
        folder="ScriptExtender",
        uuid=UUID_C,
        name="BG3SE",
        custom_deploy_path=str(tmp_path / "custom"),
    )
    lsx_path.write_text(
        _lsx(_short(GUSTAVX_UUID, folder="GustavX", name="GustavX")),
        encoding="utf-8",
    )
    report = inspect_bg3_membership(db)
    assert iid in report.skipped_custom
    assert iid not in [m.internal_id for m in report.sortable]


def test_projection_preserves_gustavx_unmanaged_schema(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    order_path, lsx_path = bg3_paths
    library = _configure_game(db, tmp_path)
    _, iid_a, _ = _seed_bg3_mod(library, db, tmp_path, folder="ModA", uuid=UUID_A, name="A")
    _, iid_b, _ = _seed_bg3_mod(library, db, tmp_path, folder="ModB", uuid=UUID_B, name="B")
    lsx_path.write_text(_base_lsx(), encoding="utf-8")
    persist_load_order([iid_a, iid_b], db)
    apply_order_move(iid_b, ORDER_MOVE_TOP, db)
    text = lsx_path.read_text(encoding="utf-8")
    _, nodes = parse_bg3_modsettings(text)
    uuids = [n.uuid for n in nodes]
    assert uuids[0] == GUSTAVX_UUID
    assert uuids.count(UUID_UNMANAGED) == 2
    assert UUID_STALE in uuids
    assert "ModOrder" not in text
    assert 'major="4"' in text and 'minor="8"' in text
    assert load_saved_order() == [iid_b, iid_a]
    assert UUID_B in uuids and UUID_A in uuids
    assert uuids.index(UUID_B) < uuids.index(UUID_A)
    before = text
    apply_order_move(iid_b, ORDER_MOVE_TOP, db)
    assert lsx_path.read_text(encoding="utf-8") == before
    del order_path


def test_missing_managed_node_is_created(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    _order, lsx_path = bg3_paths
    library = _configure_game(db, tmp_path)
    _, iid_a, _ = _seed_bg3_mod(library, db, tmp_path, folder="ModA", uuid=UUID_A, name="A")
    _, iid_c, _ = _seed_bg3_mod(
        library, db, tmp_path, folder="ModC", uuid=UUID_C, name="C"
    )
    lsx_path.write_text(
        _lsx(
            _short(GUSTAVX_UUID, folder="GustavX", name="GustavX"),
            _short(UUID_A, folder="ModA", name="A"),
            _short(UUID_UNMANAGED, folder="CommunityLibrary", name="CommunityLibrary"),
        ),
        encoding="utf-8",
    )
    persist_load_order([iid_a, iid_c], db)
    apply_order_move(iid_c, ORDER_MOVE_BOTTOM, db)
    _, nodes = parse_bg3_modsettings(lsx_path.read_text(encoding="utf-8"))
    uuids = [n.uuid for n in nodes]
    assert UUID_C in uuids
    assert uuids[0] == GUSTAVX_UUID
    assert UUID_UNMANAGED in uuids


def test_top_up_down_bottom_and_drop(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    _order, lsx_path = bg3_paths
    library = _configure_game(db, tmp_path)
    _, iid_a, _ = _seed_bg3_mod(library, db, tmp_path, folder="ModA", uuid=UUID_A, name="A")
    _, iid_b, _ = _seed_bg3_mod(library, db, tmp_path, folder="ModB", uuid=UUID_B, name="B")
    _, iid_c, _ = _seed_bg3_mod(
        library, db, tmp_path, folder="ModC", uuid=UUID_C, name="C"
    )
    lsx_path.write_text(
        _lsx(
            _short(GUSTAVX_UUID, folder="GustavX", name="GustavX"),
            _short(UUID_A, folder="ModA", name="A"),
            _short(UUID_B, folder="ModB", name="B"),
            _short(UUID_C, folder="ModC", name="C"),
        ),
        encoding="utf-8",
    )
    persist_load_order([iid_a, iid_b, iid_c], db)
    apply_order_move(iid_c, ORDER_MOVE_TOP, db)
    assert resolved_load_order(db) == [iid_c, iid_a, iid_b]
    apply_order_move(iid_c, ORDER_MOVE_DOWN, db)
    assert resolved_load_order(db) == [iid_a, iid_c, iid_b]
    apply_order_move(iid_c, ORDER_MOVE_UP, db)
    assert resolved_load_order(db) == [iid_c, iid_a, iid_b]
    apply_order_move(iid_c, ORDER_MOVE_BOTTOM, db)
    assert resolved_load_order(db) == [iid_a, iid_b, iid_c]
    apply_card_drop(iid_c, iid_a, db)
    assert resolved_load_order(db) == [iid_c, iid_a, iid_b]
