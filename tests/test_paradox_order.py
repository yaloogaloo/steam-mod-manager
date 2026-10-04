"""Generic Paradox Launcher order — CK3 + Stellaris share one runtime."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from core.paths import load_order_dir
from services.identity_service import create_mod_identity, identity_create_scope
from services.paradox_activation import (
    CK3_APP_ID,
    CK3_ORDER_FILENAME,
    ORDER_MOVE_BOTTOM,
    ORDER_MOVE_DOWN,
    ORDER_MOVE_TOP,
    ORDER_MOVE_UP,
    STELLARIS_APP_ID,
    STELLARIS_ORDER_FILENAME,
    apply_order_move,
    is_paradox_activation_app,
    load_saved_order,
    persist_load_order,
    sync_paradox_launcher,
    workshop_launcher_id,
)
from tests.helpers.identity import write_info_sidecar

CK3 = 1158310
STELLARIS = 281990


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "paradox_order.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _configure_ck3(db: DatabaseManager, tmp_path: Path) -> tuple[Path, Path, Path]:
    user_dir = tmp_path / "Paradox" / "Crusader Kings III"
    workshop = tmp_path / "workshop" / "content" / str(CK3)
    library = tmp_path / "mod"
    user_dir.mkdir(parents=True)
    workshop.mkdir(parents=True)
    (user_dir / "dlc_load.json").write_text(
        json.dumps({"enabled_mods": [], "disabled_dlcs": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    db.upsert_game(
        GameInfo(app_id=CK3, name="Crusader Kings III", folder_name="十字军之王Ⅲ")
    )
    db.update_game_deploy_config(
        CK3,
        name="Crusader Kings III",
        install_path=str(tmp_path / "CK3Install"),
        mod_path=str(user_dir),
        workshop_path=str(workshop),
    )
    return user_dir, workshop, library


def _configure_stellaris(db: DatabaseManager, tmp_path: Path) -> tuple[Path, Path]:
    user_dir = tmp_path / "Paradox" / "Stellaris"
    workshop = tmp_path / "workshop" / "content" / str(STELLARIS)
    user_dir.mkdir(parents=True)
    workshop.mkdir(parents=True)
    (user_dir / "dlc_load.json").write_text(
        json.dumps({"enabled_mods": [], "disabled_dlcs": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    db.upsert_game(GameInfo(app_id=STELLARIS, name="Stellaris", folder_name="Stellaris"))
    db.update_game_deploy_config(
        STELLARIS,
        name="Stellaris",
        install_path=str(tmp_path / "StellarisInstall"),
        mod_path=str(user_dir),
        workshop_path=str(workshop),
    )
    return user_dir, workshop


def _write_ugc(user_dir: Path, workshop_id: str, workshop_path: Path) -> Path:
    mod_dir = user_dir / "mod"
    mod_dir.mkdir(parents=True, exist_ok=True)
    path = mod_dir / f"ugc_{workshop_id}.mod"
    path.write_text(
        f'version="1.0"\nname="Mod {workshop_id}"\n'
        f'path="{workshop_path.resolve().as_posix()}"\n'
        f'remote_file_id="{workshop_id}"\n',
        encoding="utf-8",
    )
    return path


def _seed_ck3_mod(
    library: Path,
    db: DatabaseManager,
    *,
    workshop_id: str,
    folder: str,
    deployed: bool = True,
) -> tuple[str, str, Path]:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(workshop_id),
            workshop_id=str(workshop_id),
            title=folder,
            app_id=CK3,
            game_name="Crusader Kings III",
            operation="import",
        )
    pk = str(created.mod_id)
    entity = str(created.internal_id or "")
    mod_dir = library / "十字军之王Ⅲ" / folder
    mod_dir.mkdir(parents=True)
    (mod_dir / "common").mkdir()
    (mod_dir / "common" / "foo.txt").write_text(folder, encoding="utf-8")
    (mod_dir / "descriptor.mod").write_text(
        f'name="{folder}"\nremote_file_id="{workshop_id}"\n',
        encoding="utf-8",
    )
    write_info_sidecar(
        mod_dir,
        internal_id=entity,
        title=folder,
        external_id=str(workshop_id),
        workspace_id=str(created.workspace_id or workshop_id),
        app_id=CK3,
        game_name="Crusader Kings III",
    )
    db.update_mod_identity_fields(
        pk,
        last_known_path=str(mod_dir),
        folder_present=True,
    )
    if deployed:
        db.update_mod_deploy_status(
            pk,
            deploy_status=DEPLOY_STATUS_DEPLOYED,
            deploy_path=str(mod_dir),
            app_id=CK3,
        )
    return pk, entity, mod_dir


def _seed_workshop(workshop: Path, workshop_id: str, payload: str) -> Path:
    root = workshop / workshop_id
    root.mkdir(parents=True)
    (root / "common").mkdir()
    (root / "common" / "foo.txt").write_text(payload, encoding="utf-8")
    return root


def _seed_launcher_sqlite(user_dir: Path, entries: list[tuple[str, str]]) -> None:
    path = user_dir / "launcher-v2.sqlite"
    con = sqlite3.connect(str(path))
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE mods (
            id char(36) not null,
            steamId varchar(255),
            gameRegistryId varchar(255),
            displayName varchar(255),
            status varchar(255),
            source varchar(255),
            primary key (id)
        );
        CREATE TABLE playsets (
            id char(36) not null,
            name varchar(255) not null,
            isActive boolean,
            loadOrder varchar(255),
            createdOn datetime not null,
            isRemoved boolean not null default 0,
            primary key (id)
        );
        CREATE TABLE playsets_mods (
            playsetId char(36) not null,
            modId char(36) not null,
            enabled boolean default '1',
            position integer
        );
        """
    )
    cur.execute(
        """
        INSERT INTO playsets(id, name, isActive, loadOrder, createdOn, isRemoved)
        VALUES (?, ?, 1, NULL, 1, 0)
        """,
        ("playset-1", "Initial playset"),
    )
    for position, (steam_id, registry) in enumerate(entries):
        mod_pk = f"mod-{steam_id}"
        cur.execute(
            """
            INSERT INTO mods(id, steamId, gameRegistryId, displayName, status, source)
            VALUES (?, ?, ?, ?, 'ready_to_play', 'steam')
            """,
            (mod_pk, steam_id, registry, steam_id),
        )
        cur.execute(
            "INSERT INTO playsets_mods(playsetId, modId, enabled, position) VALUES (?, ?, 1, ?)",
            ("playset-1", mod_pk, position),
        )
    con.commit()
    con.close()


def _playset_positions(user_dir: Path) -> dict[str, int]:
    con = sqlite3.connect(str(user_dir / "launcher-v2.sqlite"))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT m.gameRegistryId, pm.position, pm.enabled
        FROM playsets_mods pm JOIN mods m ON m.id = pm.modId
        """
    ).fetchall()
    con.close()
    return {str(row["gameRegistryId"]): int(row["position"]) for row in rows}


def _setup_three(
    db: DatabaseManager,
    tmp_path: Path,
    *,
    write_c_ugc: bool = True,
) -> tuple[Path, list[str], list[str], list[str]]:
    user_dir, workshop, library = _configure_ck3(db, tmp_path)
    ids = ["9101", "9102", "9103"]
    folders = ["ModA", "ModB", "ModC"]
    entities: list[str] = []
    pks: list[str] = []
    for workshop_id, folder in zip(ids, folders, strict=True):
        src = _seed_workshop(workshop, workshop_id, folder)
        if workshop_id != "9103" or write_c_ugc:
            _write_ugc(user_dir, workshop_id, src)
        pk, entity, _managed = _seed_ck3_mod(
            library, db, workshop_id=workshop_id, folder=folder
        )
        pks.append(pk)
        entities.append(entity)
    _seed_launcher_sqlite(
        user_dir,
        [(wid, workshop_launcher_id(wid)) for wid in ids],
    )
    persist_load_order(entities, db, app_id=CK3)
    return user_dir, pks, entities, ids


def test_t1_ck3_order_file_internal_id(tmp_path: Path, db: DatabaseManager) -> None:
    _user_dir, pks, entities, ids = _setup_three(db, tmp_path)
    saved = load_saved_order(app_id=CK3)
    assert saved[:3] == entities
    payload = json.loads((load_order_dir() / CK3_ORDER_FILENAME).read_text(encoding="utf-8"))
    assert payload["order"][:3] == entities
    assert all(tok not in ids for tok in saved)
    assert all(tok not in pks for tok in saved)
    assert all("-" in tok for tok in saved[:3])


def test_t2_identity_resolution(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _pks, entities, ids = _setup_three(db, tmp_path)
    sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    report_ids = [
        workshop_launcher_id(wid) for wid in ids
    ]
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == report_ids
    assert entities[0] not in payload["enabled_mods"]
    assert ids[0] not in payload["enabled_mods"]


def test_t3_launcher_order_matches_smm(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _pks, entities, ids = _setup_three(db, tmp_path)
    persist_load_order(entities, db, app_id=CK3)
    sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    expected = [workshop_launcher_id(wid) for wid in ids]
    assert payload["enabled_mods"] == expected
    positions = _playset_positions(user_dir)
    assert positions[expected[0]] < positions[expected[1]] < positions[expected[2]]
    assert load_saved_order(app_id=CK3)[:3] == entities


def test_t4_move_up(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _pks, entities, ids = _setup_three(db, tmp_path)
    nxt = apply_order_move(entities[1], ORDER_MOVE_UP, db, app_id=CK3)
    assert nxt[:3] == [entities[1], entities[0], entities[2]]
    sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [
        workshop_launcher_id(ids[1]),
        workshop_launcher_id(ids[0]),
        workshop_launcher_id(ids[2]),
    ]


def test_t5_move_down(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _pks, entities, ids = _setup_three(db, tmp_path)
    nxt = apply_order_move(entities[1], ORDER_MOVE_DOWN, db, app_id=CK3)
    assert nxt[:3] == [entities[0], entities[2], entities[1]]
    sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [
        workshop_launcher_id(ids[0]),
        workshop_launcher_id(ids[2]),
        workshop_launcher_id(ids[1]),
    ]


def test_t6_move_top(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _pks, entities, ids = _setup_three(db, tmp_path)
    nxt = apply_order_move(entities[2], ORDER_MOVE_TOP, db, app_id=CK3)
    assert nxt[:3] == [entities[2], entities[0], entities[1]]
    sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [
        workshop_launcher_id(ids[2]),
        workshop_launcher_id(ids[0]),
        workshop_launcher_id(ids[1]),
    ]


def test_t7_move_bottom(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _pks, entities, ids = _setup_three(db, tmp_path)
    nxt = apply_order_move(entities[0], ORDER_MOVE_BOTTOM, db, app_id=CK3)
    assert nxt[:3] == [entities[1], entities[2], entities[0]]
    sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [
        workshop_launcher_id(ids[1]),
        workshop_launcher_id(ids[2]),
        workshop_launcher_id(ids[0]),
    ]


def test_t8_unresolved_stays_out_of_launcher_order(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _pks, entities, ids = _setup_three(db, tmp_path, write_c_ugc=False)
    ugc_c = user_dir / "mod" / f"ugc_{ids[2]}.mod"
    assert not ugc_c.exists()
    report = sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    assert entities[2] in report.unresolved
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert workshop_launcher_id(ids[2]) not in payload["enabled_mods"]
    assert payload["enabled_mods"] == [
        workshop_launcher_id(ids[0]),
        workshop_launcher_id(ids[1]),
    ]
    assert not ugc_c.exists()
    assert load_saved_order(app_id=CK3)[:3] == entities


def test_t9_repeat_persist_idempotent(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _pks, entities, ids = _setup_three(db, tmp_path)
    first = persist_load_order(entities, db, app_id=CK3)
    second = persist_load_order(entities, db, app_id=CK3)
    assert first == second
    assert first[:3] == entities
    sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [workshop_launcher_id(wid) for wid in ids]
    assert len(payload["enabled_mods"]) == len(set(payload["enabled_mods"]))


def test_t10_shared_runtime_separate_order_files(
    tmp_path: Path, db: DatabaseManager
) -> None:
    assert is_paradox_activation_app(CK3) is True
    assert is_paradox_activation_app(STELLARIS) is True
    ck3_user, workshop, library = _configure_ck3(db, tmp_path)
    _st_user, _st_ws = _configure_stellaris(db, tmp_path)
    _seed_workshop(workshop, "9101", "A")
    _write_ugc(ck3_user, "9101", workshop / "9101")
    _pk, ck3_entity, _managed = _seed_ck3_mod(
        library, db, workshop_id="9101", folder="SharedA"
    )
    with identity_create_scope():
        st_created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id="8101",
            workshop_id="8101",
            title="StA",
            app_id=STELLARIS,
            game_name="Stellaris",
            operation="import",
        )
    st_pk = str(st_created.mod_id)
    st_entity = str(st_created.internal_id or "")
    st_dir = library / "Stellaris" / "StA"
    st_dir.mkdir(parents=True)
    write_info_sidecar(
        st_dir,
        internal_id=st_entity,
        title="StA",
        external_id="8101",
        workspace_id="8101",
        app_id=STELLARIS,
        game_name="Stellaris",
    )
    db.update_mod_identity_fields(st_pk, last_known_path=str(st_dir), folder_present=True)
    db.update_mod_deploy_status(
        st_pk,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path=str(st_dir),
        app_id=STELLARIS,
    )
    persist_load_order([ck3_entity], db, app_id=CK3)
    persist_load_order([st_entity], db, app_id=STELLARIS)
    ck3_path = load_order_dir() / CK3_ORDER_FILENAME
    st_path = load_order_dir() / STELLARIS_ORDER_FILENAME
    assert ck3_path.is_file()
    assert st_path.is_file()
    assert ck3_path != st_path
    assert json.loads(ck3_path.read_text(encoding="utf-8"))["order"] == [ck3_entity]
    assert json.loads(st_path.read_text(encoding="utf-8"))["order"] == [st_entity]
    assert ck3_entity != st_entity
    src = Path("services/paradox_activation.py").read_text(encoding="utf-8")
    assert "def persist_ck3_load_order" not in src
    assert "def apply_ck3_order" not in src
    assert "apply_order_move" in src
    assert not (Path("services") / "ck3_order.py").exists()
    assert not (Path("services") / "deploy_rules" / "ck3.py").exists()


def test_order_does_not_copy_or_rewrite_payload(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, workshop, library = _configure_ck3(db, tmp_path)
    ids = ["9201", "9202"]
    entities = []
    hashes = {}
    for workshop_id, folder in zip(ids, ["Alpha", "Beta"], strict=True):
        src = _seed_workshop(workshop, workshop_id, folder)
        hashes[workshop_id] = (src / "common" / "foo.txt").read_bytes()
        _write_ugc(user_dir, workshop_id, src)
        _pk, entity, _managed = _seed_ck3_mod(
            library, db, workshop_id=workshop_id, folder=folder
        )
        entities.append(entity)
    _seed_launcher_sqlite(
        user_dir, [(wid, workshop_launcher_id(wid)) for wid in ids]
    )
    persist_load_order(entities, db, app_id=CK3)
    apply_order_move(entities[1], ORDER_MOVE_TOP, db, app_id=CK3)
    sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    for workshop_id, folder in zip(ids, ["Alpha", "Beta"], strict=True):
        assert (workshop / workshop_id / "common" / "foo.txt").read_bytes() == hashes[
            workshop_id
        ]
        ugc = (user_dir / "mod" / f"ugc_{workshop_id}.mod").read_text(encoding="utf-8")
        assert f"workshop/content/{CK3}/{workshop_id}" in ugc.replace("\\", "/")
        assert "十字军" not in ugc
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [
        workshop_launcher_id(ids[1]),
        workshop_launcher_id(ids[0]),
    ]
