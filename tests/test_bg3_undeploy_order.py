"""BG3 undeploy → explicit remove_uuids. Does not reopen Sort Mode."""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager
from core.game_info import GameInfo
from services.bg3_activation import (
    BG3_APP_ID,
    Bg3OrderError,
    apply_bg3_undeploy_order,
    apply_order_move,
    is_bg3_order_app,
    load_saved_order,
    persist_load_order,
    resolve_entity_bg3_uuids,
    save_saved_order,
)
from services.bg3_modsettings import GUSTAVX_UUID, parse_bg3_modsettings
from services.deploy import ModDeployer
from services.file_ops import INFO_DIR_NAME
from services.paradox_activation import (
    CK3_APP_ID,
    ORDER_MOVE_TOP,
    STELLARIS_APP_ID,
)
from services.wh3_activation import WH3_APP_ID
from tests.helpers.bg3_lspk import meta_lsx_bytes, write_lspk_v18
from tests.helpers.identity import create_other_test_mod, write_info_sidecar

ROOT = Path(__file__).resolve().parents[1]
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _uuids(path: Path) -> list[str]:
    _version, nodes = parse_bg3_modsettings(path.read_text(encoding="utf-8"))
    return [node.uuid for node in nodes]


def _unmanaged(path: Path, managed: set[str]) -> list[str]:
    return [uuid for uuid in _uuids(path)[1:] if uuid not in managed]


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "bg3_undeploy.db")
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


def _configure_game(db: DatabaseManager, tmp_path: Path) -> tuple[Path, Path]:
    library = tmp_path / "mod"
    game_mods = tmp_path / "GameMods"
    game_mods.mkdir(parents=True, exist_ok=True)
    (tmp_path / "BG3Install").mkdir(parents=True, exist_ok=True)
    db.upsert_game(
        GameInfo(app_id=BG3_APP_ID, name="Baldur's Gate 3", folder_name="博德之门Ⅲ")
    )
    db.update_game_deploy_config(
        BG3_APP_ID,
        name="Baldur's Gate 3",
        install_path=str(tmp_path / "BG3Install"),
        mod_path=str(game_mods),
    )
    return library, game_mods


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
    game_mods: Path,
    *,
    folder: str,
    uuid: str,
    name: str,
    deployed: bool = True,
    custom_deploy_path: str = "",
) -> tuple[str, str, Path, Path]:
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
    library_pak = mod_dir / f"{folder}.pak"
    write_lspk_v18(
        library_pak,
        {f"Mods/{folder}/meta.lsx": meta_lsx_bytes(uuid=uuid, name=name, folder=folder)},
    )
    deployed_pak = game_mods / f"{folder}.pak"
    if deployed:
        deployed_pak.write_bytes(library_pak.read_bytes())
        _write_manifest(mod_dir, pk=pk, iid=iid, paks=[deployed_pak])
        db.update_mod_deploy_status(
            pk, deploy_status=DEPLOY_STATUS_DEPLOYED, deploy_path=str(deployed_pak)
        )
    db.update_mod_identity_fields(pk, last_known_path=str(mod_dir), folder_present=True)
    return pk, iid, library_pak, mod_dir


def _base_lsx() -> str:
    return _lsx(
        _short(GUSTAVX_UUID, folder="GustavX", name="GustavX"),
        _short(UUID_A, folder="ModA", name="A"),
        _short(UUID_UNMANAGED, folder="CommunityLibrary", name="CommunityLibrary"),
        _short(UUID_UNMANAGED, folder="CommunityLibrary", name="CommunityLibrary"),
        _short(UUID_B, folder="ModB", name="B"),
        _short(UUID_C, folder="ModC", name="C"),
        _short(UUID_STALE, folder="Stale", name="Stale"),
    )


def _prepare_sorted_abc(
    db: DatabaseManager,
    tmp_path: Path,
    lsx_path: Path,
    *,
    deploy_via_core: bool = False,
) -> tuple[Path, dict[str, tuple[str, str, Path]]]:
    library, game_mods = _configure_game(db, tmp_path)
    seeds: dict[str, tuple[str, str, Path]] = {}
    for folder, uuid, name in (
        ("ModA", UUID_A, "A"),
        ("ModB", UUID_B, "B"),
        ("ModC", UUID_C, "C"),
    ):
        pk, iid, _pak, folder_path = _seed_bg3_mod(
            library,
            db,
            game_mods,
            folder=folder,
            uuid=uuid,
            name=name,
            deployed=not deploy_via_core,
        )
        seeds[name] = (pk, iid, folder_path)
    lsx_path.write_text(_base_lsx(), encoding="utf-8")
    if deploy_via_core:
        deployer = ModDeployer(library_root=library, db=db)
        for name in ("A", "B", "C"):
            result = deployer.deploy_mod(seeds[name][1])
            assert result["success"] is True, result
    persist_load_order(None, db, library_root=library)
    return library, seeds


def test_production_fixture_is_temp_copy(
    tmp_path: Path, bg3_paths: tuple[Path, Path]
) -> None:
    order_path, lsx_path = bg3_paths
    assert order_path.is_relative_to(tmp_path)
    assert lsx_path.is_relative_to(tmp_path)
    live = (
        Path.home()
        / "AppData"
        / "Local"
        / "Larian Studios"
        / "Baldur's Gate 3"
        / "PlayerProfiles"
        / "Public"
        / "modsettings.lsx"
    )
    assert lsx_path.resolve() != live.resolve()


def test_deploy_sort_undeploy_removes_b_only(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    order_path, lsx_path = bg3_paths
    library, seeds = _prepare_sorted_abc(
        db, tmp_path, lsx_path, deploy_via_core=True
    )
    _pk_a, iid_a, _folder_a = seeds["A"]
    _pk_b, iid_b, folder_b = seeds["B"]
    _pk_c, iid_c, _folder_c = seeds["C"]
    assert load_saved_order() == [iid_a, iid_b, iid_c]

    apply_order_move(iid_b, ORDER_MOVE_TOP, db, library_root=library)
    apply_order_move(iid_a, ORDER_MOVE_TOP, db, library_root=library)
    assert load_saved_order() == [iid_a, iid_b, iid_c]

    unmanaged_before = _unmanaged(lsx_path, {UUID_A, UUID_B, UUID_C})
    deployer = ModDeployer(library_root=library, db=db)
    result = deployer.undeploy_mod(iid_b)
    assert result["success"] is True, result

    assert load_saved_order() == [iid_a, iid_c]
    payload = json.loads(order_path.read_text(encoding="utf-8"))
    assert payload["order"] == [iid_a, iid_c]
    assert iid_b not in payload["order"]
    live = _uuids(lsx_path)
    assert live[0] == GUSTAVX_UUID
    assert UUID_B not in live
    assert live == [
        GUSTAVX_UUID,
        UUID_A,
        UUID_UNMANAGED,
        UUID_UNMANAGED,
        UUID_C,
        UUID_STALE,
    ]
    assert _unmanaged(lsx_path, {UUID_A, UUID_C}) == unmanaged_before
    assert not (folder_b / INFO_DIR_NAME / "deploy_manifest.json").exists()
    assert not (tmp_path / "GameMods" / "ModB.pak").exists()


def test_unmanaged_mod_is_not_removed(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    _order_path, lsx_path = bg3_paths
    library, _seeds = _prepare_sorted_abc(db, tmp_path, lsx_path)
    _pk_u, iid_u, _pak, folder_u = _seed_bg3_mod(
        library,
        db,
        tmp_path / "GameMods",
        folder="CommunityLibrary",
        uuid=UUID_UNMANAGED,
        name="CommunityLibrary",
    )
    before = lsx_path.read_text(encoding="utf-8")
    json_before = load_saved_order()
    out = apply_bg3_undeploy_order(
        iid_u, managed_path=folder_u, db=db, library_root=library
    )
    assert out["skipped"] is True
    assert out["reason"] == "not_in_canonical_order"
    assert load_saved_order() == json_before
    assert lsx_path.read_text(encoding="utf-8") == before
    assert _uuids(lsx_path).count(UUID_UNMANAGED) == 2

    deployer = ModDeployer(library_root=library, db=db)
    result = deployer.undeploy_mod(iid_u)
    assert result["success"] is True, result
    assert _uuids(lsx_path).count(UUID_UNMANAGED) == 2
    assert load_saved_order() == json_before


def test_gustavx_undeploy_is_refused(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    _order_path, lsx_path = bg3_paths
    library, _seeds = _prepare_sorted_abc(db, tmp_path, lsx_path)
    _pk_g, iid_g, _pak, folder_g = _seed_bg3_mod(
        library,
        db,
        tmp_path / "GameMods",
        folder="GustavX",
        uuid=GUSTAVX_UUID,
        name="GustavX",
    )
    before = lsx_path.read_text(encoding="utf-8")
    json_before = list(load_saved_order())
    with pytest.raises(Bg3OrderError) as exc:
        apply_bg3_undeploy_order(
            iid_g, managed_path=folder_g, db=db, library_root=library
        )
    assert exc.value.code == "GustavXProtected"
    assert load_saved_order() == json_before
    assert _uuids(lsx_path)[0] == GUSTAVX_UUID
    assert lsx_path.read_text(encoding="utf-8") == before

    deployer = ModDeployer(library_root=library, db=db)
    result = deployer.undeploy_mod(iid_g)
    assert result["success"] is False
    assert result.get("error_code") == "GustavXProtected"
    assert (tmp_path / "GameMods" / "GustavX.pak").is_file()
    assert _uuids(lsx_path)[0] == GUSTAVX_UUID


def test_duplicate_managed_uuid_fail_closed(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    order_path, lsx_path = bg3_paths
    library, seeds = _prepare_sorted_abc(db, tmp_path, lsx_path)
    _pk_b, iid_b, folder_b = seeds["B"]
    lsx_path.write_text(
        _lsx(
            _short(GUSTAVX_UUID, folder="GustavX", name="GustavX"),
            _short(UUID_A, folder="ModA", name="A"),
            _short(UUID_UNMANAGED, folder="CommunityLibrary", name="CommunityLibrary"),
            _short(UUID_B, folder="ModB", name="B"),
            _short(UUID_B, folder="ModB", name="B"),
            _short(UUID_C, folder="ModC", name="C"),
        ),
        encoding="utf-8",
    )
    save_saved_order([seeds["A"][1], iid_b, seeds["C"][1]])
    before = lsx_path.read_text(encoding="utf-8")
    json_before = list(load_saved_order())
    with pytest.raises(Bg3OrderError) as exc:
        apply_bg3_undeploy_order(
            iid_b, managed_path=folder_b, db=db, library_root=library
        )
    assert exc.value.code == "DuplicateManagedUUID"
    assert exc.value.fields.get("uuid") == UUID_B
    assert load_saved_order() == json_before
    assert lsx_path.read_text(encoding="utf-8") == before
    assert _uuids(lsx_path).count(UUID_B) == 2
    assert order_path.read_text(encoding="utf-8")


def test_missing_uuid_fail_closed(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    _order_path, lsx_path = bg3_paths
    library, seeds = _prepare_sorted_abc(db, tmp_path, lsx_path)
    _pk_b, iid_b, folder_b = seeds["B"]
    (tmp_path / "GameMods" / "ModB.pak").unlink()
    before = lsx_path.read_text(encoding="utf-8")
    json_before = list(load_saved_order())
    with pytest.raises(Bg3OrderError) as exc:
        apply_bg3_undeploy_order(
            iid_b, managed_path=folder_b, db=db, library_root=library
        )
    assert exc.value.code == "MissingPak"
    assert load_saved_order() == json_before
    assert UUID_B in _uuids(lsx_path)
    assert lsx_path.read_text(encoding="utf-8") == before

    deployer = ModDeployer(library_root=library, db=db)
    result = deployer.undeploy_mod(iid_b)
    assert result["success"] is False
    assert result.get("error_code") == "MissingPak"
    assert load_saved_order() == json_before
    assert UUID_B in _uuids(lsx_path)


def test_repeated_undeploy_is_idempotent(
    tmp_path: Path, db: DatabaseManager, bg3_paths: tuple[Path, Path]
) -> None:
    order_path, lsx_path = bg3_paths
    library, seeds = _prepare_sorted_abc(db, tmp_path, lsx_path)
    _pk_b, iid_b, folder_b = seeds["B"]
    first = apply_bg3_undeploy_order(
        iid_b, managed_path=folder_b, db=db, library_root=library
    )
    assert first["skipped"] is False
    json_sha = _sha256(order_path)
    lsx_sha = _sha256(lsx_path)
    second = apply_bg3_undeploy_order(
        iid_b, managed_path=folder_b, db=db, library_root=library
    )
    assert second["skipped"] is True
    assert _sha256(order_path) == json_sha
    assert _sha256(lsx_path) == lsx_sha
    third = apply_bg3_undeploy_order(
        iid_b, managed_path=folder_b, db=db, library_root=library
    )
    assert third["skipped"] is True
    assert _sha256(lsx_path) == lsx_sha


def test_lsx_failure_rolls_back_json(
    tmp_path: Path,
    db: DatabaseManager,
    bg3_paths: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order_path, lsx_path = bg3_paths
    library, seeds = _prepare_sorted_abc(db, tmp_path, lsx_path)
    _pk_b, iid_b, folder_b = seeds["B"]
    json_before = order_path.read_bytes()
    lsx_before = lsx_path.read_bytes()
    real_replace = __import__("os").replace

    def _boom(src: str, dst: str) -> None:
        if str(dst).endswith("modsettings.lsx"):
            raise OSError("replace failed")
        real_replace(src, dst)

    monkeypatch.setattr("os.replace", _boom)
    with pytest.raises(OSError, match="replace failed"):
        apply_bg3_undeploy_order(
            iid_b, managed_path=folder_b, db=db, library_root=library
        )
    assert order_path.read_bytes() == json_before
    assert lsx_path.read_bytes() == lsx_before
    assert load_saved_order() == [seeds["A"][1], iid_b, seeds["C"][1]]


def test_uuid_resolution_does_not_use_workspace_id() -> None:
    tree = ast.parse(
        inspect.getsource(resolve_entity_bg3_uuids)
        + "\n"
        + inspect.getsource(apply_bg3_undeploy_order)
    )
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert "workspace_id" not in names
    assert "workspace_id" not in attrs
    assert "remove_uuids" in inspect.getsource(apply_bg3_undeploy_order)


def test_ck3_stellaris_wh3_lifecycle_untouched() -> None:
    deploy_src = (ROOT / "services" / "deploy.py").read_text(encoding="utf-8")
    hook = deploy_src.index("apply_bg3_undeploy_order")
    assert deploy_src.index("_undeploy_wh3_activation") < hook
    assert deploy_src.index("_undeploy_paradox_activation") < hook
    for name in ("paradox_activation.py", "stellaris_activation.py", "wh3_activation.py"):
        text = (ROOT / "services" / name).read_text(encoding="utf-8")
        assert "apply_bg3_undeploy_order" not in text
    assert not is_bg3_order_app(CK3_APP_ID)
    assert not is_bg3_order_app(STELLARIS_APP_ID)
    assert not is_bg3_order_app(WH3_APP_ID)
