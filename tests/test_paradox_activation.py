"""Paradox Launcher activation shared by Stellaris and CK3."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM, CK3_APP_IDS, STELLARIS_APP_IDS
from core.paths import load_order_dir
from services.deploy import ModDeployer
from services.deploy_rules import (
    DEPLOY_TYPE_PARADOX_LAUNCHER,
    DEPLOY_TYPE_WARHAMMER3,
    resolve_deploy_type,
)
from services.identity_service import create_mod_identity, identity_create_scope
from services.order_backend import get_order_backend
from services.paradox_activation import (
    CK3_APP_ID,
    CK3_ORDER_FILENAME,
    NO_ACTIVE_PLAYSET_SENTINEL,
    ORDER_MOVE_BOTTOM,
    PARADOX_INACTIVE_PLAYSET_MESSAGE,
    RESILIENCE_DEPLOYED_EFFECTIVE,
    RESILIENCE_DEPLOYED_NOT_EFFECTIVE,
    STELLARIS_APP_ID,
    apply_card_drop,
    apply_order_move,
    consider_paradox_playset_notice,
    get_paradox_effective_state,
    is_ck3_activation_app,
    is_paradox_activation_app,
    is_stellaris_activation_app,
    load_saved_order,
    persist_load_order,
    sync_paradox_launcher,
    workshop_content_root,
    workshop_launcher_id,
)
from tests.helpers.identity import write_info_sidecar

CK3 = 1158310
STELLARIS = 281990
SYNTHETIC_WORKSHOP_ID = "1234567890"
assert CK3 == CK3_APP_ID
assert CK3 in CK3_APP_IDS
assert STELLARIS == STELLARIS_APP_ID
assert STELLARIS in STELLARIS_APP_IDS

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "ck3_clausewitz"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "paradox_activation.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = _sha256(path)
    return out


def _configure_ck3(
    db: DatabaseManager,
    tmp_path: Path,
    *,
    write_ugc: bool = True,
    ugc_path: str | None = None,
) -> tuple[Path, Path, Path]:
    user_dir = tmp_path / "Paradox" / "Crusader Kings III"
    workshop = tmp_path / "workshop" / "content" / str(CK3)
    library = tmp_path / "mod"
    user_dir.mkdir(parents=True)
    (user_dir / "dlc_load.json").write_text(
        json.dumps({"enabled_mods": [], "disabled_dlcs": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    src_fixture = FIXTURE_ROOT / "workshop" / SYNTHETIC_WORKSHOP_ID
    dest = workshop / SYNTHETIC_WORKSHOP_ID
    dest.mkdir(parents=True)
    for child in src_fixture.rglob("*"):
        rel = child.relative_to(src_fixture)
        target = dest / rel
        if child.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(child.read_bytes())
    if write_ugc:
        mod_dir = user_dir / "mod"
        mod_dir.mkdir(parents=True, exist_ok=True)
        path_value = ugc_path if ugc_path is not None else dest.resolve().as_posix()
        (mod_dir / f"ugc_{SYNTHETIC_WORKSHOP_ID}.mod").write_text(
            "version=\"1.0\"\n"
            'tags={\n\t"Utilities"\n}\n'
            'name="SMM CK3 Synthetic Fixture"\n'
            f'path="{path_value}"\n'
            f'remote_file_id="{SYNTHETIC_WORKSHOP_ID}"\n',
            encoding="utf-8",
        )
    db.upsert_game(
        GameInfo(
            app_id=CK3,
            name="Crusader Kings III",
            folder_name="十字军之王Ⅲ",
        )
    )
    db.update_game_deploy_config(
        CK3,
        name="Crusader Kings III",
        install_path=str(tmp_path / "CK3Install"),
        mod_path=str(user_dir),
        workshop_path=str(workshop),
    )
    _seed_empty_active_playset(user_dir)
    return user_dir, workshop, library


def _seed_ck3_mod(
    library: Path,
    db: DatabaseManager,
    *,
    workshop_id: str = SYNTHETIC_WORKSHOP_ID,
    folder: str = "SyntheticCK3",
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
    return pk, entity, mod_dir


def _deploy_status(db: DatabaseManager, pk: str) -> str:
    info = db.get_mod_deploy_info(pk)
    return str(getattr(info, "deploy_status", "") or "")


def test_t6_app_id_routing() -> None:
    assert is_paradox_activation_app(STELLARIS) is True
    assert is_paradox_activation_app(CK3) is True
    assert is_paradox_activation_app("1158310") is True
    assert is_paradox_activation_app(0, "Crusader Kings III") is True
    assert is_paradox_activation_app(0, "十字军之王Ⅲ") is True
    assert is_ck3_activation_app(CK3) is True
    assert is_ck3_activation_app(STELLARIS) is False
    assert is_stellaris_activation_app(CK3) is False
    assert is_stellaris_activation_app(0, "Crusader Kings III") is False
    assert is_paradox_activation_app(1142710) is False
    assert is_paradox_activation_app(1623730, "Palworld") is False
    assert is_paradox_activation_app(292030, "The Witcher 3") is False
    assert resolve_deploy_type(STELLARIS, "folder_copy") == DEPLOY_TYPE_PARADOX_LAUNCHER
    assert resolve_deploy_type(CK3, "folder_copy") == DEPLOY_TYPE_PARADOX_LAUNCHER
    assert resolve_deploy_type(1142710, "folder_copy") == DEPLOY_TYPE_WARHAMMER3
    assert resolve_deploy_type(292030, "folder_copy") != DEPLOY_TYPE_PARADOX_LAUNCHER


def test_workshop_content_root_uses_app_id(tmp_path: Path) -> None:
    base = tmp_path / "workshop" / "content"
    ck3_root = workshop_content_root(base, app_id=CK3)
    st_root = workshop_content_root(base, app_id=STELLARIS)
    assert ck3_root == base / str(CK3)
    assert st_root == base / str(STELLARIS)
    assert ck3_root != st_root
    direct = workshop_content_root(base / str(CK3), app_id=CK3)
    assert direct == base / str(CK3)


def test_t1_ck3_first_deploy(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, managed = _seed_ck3_mod(library, db)
    persist_load_order([entity], db, app_id=CK3)

    result = ModDeployer(library_root=library, db=db).deploy_mod(entity)
    assert result.get("success") is True, result
    assert result.get("copied_files") == 0
    assert result.get("planned_files") == 0
    assert result.get("files") == []
    assert result.get("backed_up_files") == 0
    assert result.get("applied_files") == 0
    assert result.get("deploy_type") == DEPLOY_TYPE_PARADOX_LAUNCHER
    assert _deploy_status(db, pk) == DEPLOY_STATUS_DEPLOYED
    info = db.get_mod_deploy_info(pk)
    assert Path(str(info.deploy_path)) == managed

    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [workshop_launcher_id(SYNTHETIC_WORKSHOP_ID)]
    assert entity not in payload["enabled_mods"]
    assert pk not in payload["enabled_mods"]
    assert SYNTHETIC_WORKSHOP_ID not in payload["enabled_mods"]


def test_t2_missing_ugc_stays_unresolved(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _workshop, library = _configure_ck3(db, tmp_path, write_ugc=False)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    persist_load_order([entity], db, app_id=CK3)
    ugc = user_dir / "mod" / f"ugc_{SYNTHETIC_WORKSHOP_ID}.mod"
    assert not ugc.exists()

    result = ModDeployer(library_root=library, db=db).deploy_mod(entity)
    assert result.get("success") is True, result
    report = sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    assert report.unresolved
    assert not ugc.exists()
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert workshop_launcher_id(SYNTHETIC_WORKSHOP_ID) not in payload["enabled_mods"]
    assert list((user_dir / "mod").glob("ugc_*.mod")) == []


def test_t3_path_not_rewritten(tmp_path: Path, db: DatabaseManager) -> None:
    workshop_posix = (
        tmp_path / "workshop" / "content" / str(CK3) / SYNTHETIC_WORKSHOP_ID
    ).resolve().as_posix()
    user_dir, _workshop, library = _configure_ck3(
        db, tmp_path, ugc_path=workshop_posix
    )
    pk, entity, _managed = _seed_ck3_mod(library, db)
    persist_load_order([entity], db, app_id=CK3)
    ugc = user_dir / "mod" / f"ugc_{SYNTHETIC_WORKSHOP_ID}.mod"
    before = ugc.read_text(encoding="utf-8")
    assert f'path="{workshop_posix}"' in before

    result = ModDeployer(library_root=library, db=db).deploy_mod(entity)
    assert result.get("success") is True, result
    after = ugc.read_text(encoding="utf-8")
    assert after == before
    assert f'path="{workshop_posix}"' in after
    assert "十字军之王" not in after


def test_t4_workshop_source_immutable(tmp_path: Path, db: DatabaseManager) -> None:
    _user_dir, workshop, library = _configure_ck3(db, tmp_path)
    src = workshop / SYNTHETIC_WORKSHOP_ID
    before = _tree_hashes(src)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    persist_load_order([entity], db, app_id=CK3)
    result = ModDeployer(library_root=library, db=db).deploy_mod(entity)
    assert result.get("success") is True, result
    assert _tree_hashes(src) == before


def test_t5_ck3_order_uses_internal_identity(tmp_path: Path, db: DatabaseManager) -> None:
    _user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    persist_load_order([entity], db, app_id=CK3)
    result = ModDeployer(library_root=library, db=db).deploy_mod(entity)
    assert result.get("success") is True, result
    order_path = load_order_dir() / CK3_ORDER_FILENAME
    assert order_path.is_file()
    payload = json.loads(order_path.read_text(encoding="utf-8"))
    tokens = list(payload.get("order") or [])
    assert tokens
    assert SYNTHETIC_WORKSHOP_ID not in tokens
    assert workshop_launcher_id(SYNTHETIC_WORKSHOP_ID) not in tokens
    saved = load_saved_order(app_id=CK3)
    assert saved == tokens
    assert all(tok != SYNTHETIC_WORKSHOP_ID for tok in saved)
    assert entity in saved or pk in saved
    assert entity != SYNTHETIC_WORKSHOP_ID
    assert pk != SYNTHETIC_WORKSHOP_ID


def test_t7_t8_no_fileplan_apply_or_backup(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_calls: list[str] = []
    backup_calls: list[str] = []

    def _apply(*_a, **_k):
        apply_calls.append("apply")
        raise AssertionError("Paradox activation must not Apply FilePlan")

    class _BoomBackup:
        def __init__(self, *args, **kwargs):
            backup_calls.append("init")
            raise AssertionError("Paradox activation must not construct BackupManager")

    monkeypatch.setattr("services.deploy_apply.apply_file_plan", _apply)
    monkeypatch.setattr("services.deploy.BackupManager", _BoomBackup)
    _user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    persist_load_order([entity], db, app_id=CK3)
    result = ModDeployer(library_root=library, db=db).deploy_mod(entity)
    assert result.get("success") is True, result
    assert apply_calls == []
    assert backup_calls == []
    assert result.get("copied_files") == 0
    assert result.get("backed_up_files") == 0
    assert result.get("applied_files") == 0


def test_t9_repeat_deploy_idempotent(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    persist_load_order([entity], db, app_id=CK3)
    src_before = _tree_hashes(workshop / SYNTHETIC_WORKSHOP_ID)
    ugc = user_dir / "mod" / f"ugc_{SYNTHETIC_WORKSHOP_ID}.mod"
    ugc_before = ugc.read_bytes()
    deployer = ModDeployer(library_root=library, db=db)
    first = deployer.deploy_mod(entity)
    second = deployer.deploy_mod(entity)
    assert first.get("success") is True, first
    assert second.get("success") is True, second
    assert first.get("copied_files") == 0
    assert second.get("copied_files") == 0
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [workshop_launcher_id(SYNTHETIC_WORKSHOP_ID)]
    assert ugc.read_bytes() == ugc_before
    assert list((user_dir / "mod").glob("ugc_*.mod")) == [ugc]
    assert _tree_hashes(workshop / SYNTHETIC_WORKSHOP_ID) == src_before


def test_t10_identity_boundary(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    persist_load_order([entity], db, app_id=CK3)
    result = ModDeployer(library_root=library, db=db).deploy_mod(entity)
    assert result.get("success") is True, result
    launcher = workshop_launcher_id(SYNTHETIC_WORKSHOP_ID)
    assert launcher == f"mod/ugc_{SYNTHETIC_WORKSHOP_ID}.mod"
    assert launcher != entity
    assert launcher != pk
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [launcher]
    assert entity not in payload["enabled_mods"]
    saved = load_saved_order(app_id=CK3)
    assert launcher not in saved
    assert SYNTHETIC_WORKSHOP_ID not in saved


def test_generic_runtime_has_no_stellaris_app_id_branch() -> None:
    src = Path("services/paradox_activation.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            dump = ast.dump(node)
            assert "281990" not in dump
            assert "1158310" not in dump
    assert "PARADOX_LAUNCHER_GAMES" in src
    assert "CK3 first launch verification = CONFIRMED" in src


def test_no_ck3_strategy_module() -> None:
    root = Path("services")
    assert not (root / "deploy_rules" / "ck3.py").exists()
    assert not (root / "ck3_activation.py").exists()
    paradox = (root / "deploy_rules" / "paradox.py").read_text(encoding="utf-8")
    assert "class CK3Strategy" not in paradox
    assert "class ParadoxLauncherStrategy" in paradox


_PLAYSET_OWNERSHIP_SOURCES = (
    Path("services/paradox_activation.py"),
    Path("services/deploy.py"),
    Path("services/order_backend.py"),
    Path("services/stellaris_activation.py"),
    Path("services/deploy_rules/paradox.py"),
)

_SQL_UPDATE_TABLE = re.compile(r"\bUPDATE\s+([A-Za-z_][\w]*)\b", re.IGNORECASE)
_SQL_SET_IS_ACTIVE = re.compile(r"\bSET\b[^;]*\bisActive\b", re.IGNORECASE)


def _launcher_schema() -> str:
    """Same launcher tables the existing Paradox order fixture creates."""
    return """
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


def _seed_empty_active_playset(user_dir: Path) -> None:
    """Fixture so projection tests still have an active playset to follow."""
    path = user_dir / "launcher-v2.sqlite"
    if path.exists():
        return
    con = sqlite3.connect(str(path))
    try:
        con.executescript(_launcher_schema())
        con.execute(
            """
            INSERT INTO playsets(id, name, isActive, loadOrder, createdOn, isRemoved)
            VALUES ('playset-active', 'Active', 1, NULL, 1, 0)
            """
        )
        con.commit()
    finally:
        con.close()


def _seed_playset_db(
    user_dir: Path,
    playsets: list[tuple[str, str, int, int]],
    members: list[tuple[str, str, str, int, int]],
) -> None:
    """Fixture only. ``playsets`` rows are (id, name, isActive, createdOn)."""
    path = user_dir / "launcher-v2.sqlite"
    path.unlink(missing_ok=True)
    con = sqlite3.connect(str(path))
    try:
        con.executescript(_launcher_schema())
        con.executemany(
            """
            INSERT INTO playsets(id, name, isActive, loadOrder, createdOn, isRemoved)
            VALUES (?, ?, ?, NULL, ?, 0)
            """,
            playsets,
        )
        seen: set[str] = set()
        for playset_id, steam_id, registry, enabled, position in members:
            mod_pk = f"mod-{steam_id}"
            if mod_pk not in seen:
                con.execute(
                    """
                    INSERT INTO mods(id, steamId, gameRegistryId, displayName, status, source)
                    VALUES (?, ?, ?, ?, 'ready_to_play', 'steam')
                    """,
                    (mod_pk, steam_id, registry, steam_id),
                )
                seen.add(mod_pk)
            con.execute(
                """
                INSERT INTO playsets_mods(playsetId, modId, enabled, position)
                VALUES (?, ?, ?, ?)
                """,
                (playset_id, mod_pk, enabled, position),
            )
        con.commit()
    finally:
        con.close()


def _playset_activation(user_dir: Path) -> list[tuple[str, int]]:
    con = sqlite3.connect(str(user_dir / "launcher-v2.sqlite"))
    try:
        return [
            (str(row[0]), int(row[1]))
            for row in con.execute(
                "SELECT id, isActive FROM playsets ORDER BY createdOn"
            )
        ]
    finally:
        con.close()


def _playset_members(user_dir: Path) -> list[tuple[str, int, int, int]]:
    con = sqlite3.connect(str(user_dir / "launcher-v2.sqlite"))
    try:
        return [
            (str(row[0]), int(row[1]), int(row[2]), int(row[3]))
            for row in con.execute(
                """
                SELECT p.id, p.isActive, pm.enabled, pm.position
                FROM playsets p
                JOIN playsets_mods pm ON pm.playsetId = p.id
                ORDER BY p.createdOn, pm.position
                """
            )
        ]
    finally:
        con.close()


def _write_extra_ugc(user_dir: Path, workshop_id: str) -> None:
    mod_dir = user_dir / "mod"
    mod_dir.mkdir(parents=True, exist_ok=True)
    (mod_dir / f"ugc_{workshop_id}.mod").write_text(
        f'version="1.0"\nname="Mod {workshop_id}"\n'
        f'path="C:/workshop/{workshop_id}"\n'
        f'remote_file_id="{workshop_id}"\n',
        encoding="utf-8",
    )


def _launcher_content_load_export(con: sqlite3.Connection) -> list[str]:
    """Launcher content-load rule. Not an SMM success condition.

    An active playset exports its enabled registry ids. No active playset
    exports an empty list, even when ``playsets_mods.enabled`` is 1.
    """
    active = con.execute(
        "SELECT id FROM playsets WHERE isActive = 1 ORDER BY createdOn DESC LIMIT 1"
    ).fetchone()
    if active is None:
        return []
    rows = con.execute(
        """
        SELECT m.gameRegistryId
        FROM playsets_mods pm
        JOIN mods m ON m.id = pm.modId
        WHERE pm.playsetId = ? AND pm.enabled = 1
        ORDER BY pm.position
        """,
        (active[0],),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _memory_playset(*, active: int, enabled: int, registry: str) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(_launcher_schema())
    con.execute(
        """
        INSERT INTO playsets(id, name, isActive, loadOrder, createdOn, isRemoved)
        VALUES ('p1', 'Initial playset', ?, NULL, 1, 0)
        """,
        (active,),
    )
    con.execute(
        """
        INSERT INTO mods(id, steamId, gameRegistryId, displayName, status, source)
        VALUES ('m1', '2618143990', ?, 'mod', 'ready_to_play', 'steam')
        """,
        (registry,),
    )
    con.execute(
        """
        INSERT INTO playsets_mods(playsetId, modId, enabled, position)
        VALUES ('p1', 'm1', ?, 15)
        """,
        (enabled,),
    )
    con.commit()
    return con


def _sql_fragments(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                else:
                    parts.append(" ")
            found.append("".join(parts))
    return found


def _normalize_sql(sql: str) -> str:
    without_line = re.sub(r"--.*?$", " ", sql, flags=re.MULTILINE)
    without_block = re.sub(r"/\*.*?\*/", " ", without_line, flags=re.DOTALL)
    return re.sub(r"\s+", " ", without_block).strip()


def _playset_activation_writes(tree: ast.AST) -> list[str]:
    """Write operations against ``playsets`` / ``isActive``. Reads stay allowed."""
    hits: list[str] = []
    for sql in _sql_fragments(tree):
        compact = _normalize_sql(sql)
        if not compact:
            continue
        for match in _SQL_UPDATE_TABLE.finditer(compact):
            if match.group(1).lower() == "playsets":
                hits.append(f"UPDATE playsets: {compact[:180]}")
        if _SQL_SET_IS_ACTIVE.search(compact):
            hits.append(f"SQL assigns isActive: {compact[:180]}")
    for node in ast.walk(tree):
        targets: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.target is not None:
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "isActive":
                hits.append(f"assign name isActive line {node.lineno}")
            elif isinstance(target, ast.Attribute) and target.attr == "isActive":
                hits.append(f"assign attribute isActive line {node.lineno}")
            elif isinstance(target, ast.Subscript):
                sl = target.slice
                if isinstance(sl, ast.Constant) and sl.value == "isActive":
                    hits.append(f"assign subscript isActive line {node.lineno}")
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "isActive":
                    hits.append(f"call keyword isActive line {node.lineno}")
    return hits


def test_sync_paradox_launcher_does_not_activate_inactive_playset(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    db.update_mod_deploy_status(
        pk,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path="",
        app_id=CK3,
    )
    persist_load_order([entity], db, app_id=CK3)
    registry = workshop_launcher_id(SYNTHETIC_WORKSHOP_ID)
    _seed_playset_db(
        user_dir,
        [("playset-initial", "Initial playset", 0, 1)],
        [("playset-initial", SYNTHETIC_WORKSHOP_ID, registry, 1, 15)],
    )

    sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)

    assert _playset_activation(user_dir) == [("playset-initial", 0)]


def test_launcher_export_requires_active_playset(
    tmp_path: Path, db: DatabaseManager
) -> None:
    registry = workshop_launcher_id("2618143990")
    active = _memory_playset(active=1, enabled=1, registry=registry)
    inactive = _memory_playset(active=0, enabled=1, registry=registry)
    try:
        assert _launcher_content_load_export(active) == [registry]
        assert _launcher_content_load_export(inactive) == []
    finally:
        active.close()
        inactive.close()

    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    db.update_mod_deploy_status(
        pk,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path="",
        app_id=CK3,
    )
    persist_load_order([entity], db, app_id=CK3)
    smm_registry = workshop_launcher_id(SYNTHETIC_WORKSHOP_ID)
    _seed_playset_db(
        user_dir,
        [("playset-initial", "Initial playset", 0, 1)],
        [("playset-initial", SYNTHETIC_WORKSHOP_ID, smm_registry, 1, 15)],
    )

    before_dlc = (user_dir / "dlc_load.json").read_bytes()
    before_members = _playset_members(user_dir)
    report = sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    con = sqlite3.connect(str(user_dir / "launcher-v2.sqlite"))
    try:
        launcher_export = _launcher_content_load_export(con)
    finally:
        con.close()

    assert report.playset_written is False
    assert report.direct_dlc_load_written is True
    assert report.is_effective is False
    assert report.resilience_status == RESILIENCE_DEPLOYED_NOT_EFFECTIVE
    assert _playset_activation(user_dir) == [("playset-initial", 0)]
    assert launcher_export == []
    smm_projection = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert smm_projection["enabled_mods"] == [smm_registry]
    assert smm_projection["enabled_mods"] != launcher_export
    assert (user_dir / "dlc_load.json").read_bytes() != before_dlc
    assert _playset_members(user_dir) == before_members


def test_paradox_deploy_does_not_mutate_playset_activation(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    assert _deploy_status(db, pk) != DEPLOY_STATUS_DEPLOYED
    persist_load_order([entity], db, app_id=CK3)
    registry = workshop_launcher_id(SYNTHETIC_WORKSHOP_ID)
    _seed_playset_db(
        user_dir,
        [("playset-initial", "Initial playset", 0, 1)],
        [("playset-initial", SYNTHETIC_WORKSHOP_ID, registry, 1, 15)],
    )

    result = ModDeployer(library_root=library, db=db).deploy_mod(entity)

    assert result.get("success") is True, result
    assert _deploy_status(db, pk) == DEPLOY_STATUS_DEPLOYED
    assert _playset_activation(user_dir) == [("playset-initial", 0)]


def test_paradox_sort_does_not_mutate_playset_activation(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    second_id = "2222222222"
    _write_extra_ugc(user_dir, second_id)
    pk_a, entity_a, _a = _seed_ck3_mod(library, db, folder="A")
    pk_b, entity_b, _b = _seed_ck3_mod(
        library, db, workshop_id=second_id, folder="B"
    )
    for pk in (pk_a, pk_b):
        db.update_mod_deploy_status(
            pk,
            deploy_status=DEPLOY_STATUS_DEPLOYED,
            deploy_path="",
            app_id=CK3,
        )
    persist_load_order([entity_a, entity_b], db, app_id=CK3)
    _seed_playset_db(
        user_dir,
        [("playset-initial", "Initial playset", 0, 1)],
        [
            (
                "playset-initial",
                SYNTHETIC_WORKSHOP_ID,
                workshop_launcher_id(SYNTHETIC_WORKSHOP_ID),
                1,
                0,
            ),
            (
                "playset-initial",
                second_id,
                workshop_launcher_id(second_id),
                1,
                1,
            ),
        ],
    )
    backend = get_order_backend(CK3, "Crusader Kings III")
    assert backend is not None

    moved = apply_order_move(entity_a, ORDER_MOVE_BOTTOM, db, app_id=CK3)
    assert moved[0] == entity_b
    assert _playset_activation(user_dir) == [("playset-initial", 0)]

    dropped = apply_card_drop(entity_a, entity_b, db, app_id=CK3)
    assert set(dropped) == {entity_a, entity_b}
    assert _playset_activation(user_dir) == [("playset-initial", 0)]

    backend.sync_projection(db=db)
    assert _playset_activation(user_dir) == [("playset-initial", 0)]
    assert load_saved_order(app_id=CK3) == dropped


def test_sync_without_active_playset_does_not_project_fallback(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    db.update_mod_deploy_status(
        pk,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path="",
        app_id=CK3,
    )
    persist_load_order([entity], db, app_id=CK3)
    registry = workshop_launcher_id(SYNTHETIC_WORKSHOP_ID)
    _seed_playset_db(
        user_dir,
        [
            ("playset-older", "Older", 0, 10),
            ("playset-newer", "Newer", 0, 20),
        ],
        [
            ("playset-older", SYNTHETIC_WORKSHOP_ID, registry, 0, 7),
            ("playset-newer", SYNTHETIC_WORKSHOP_ID, registry, 0, 3),
        ],
    )
    before_dlc = (user_dir / "dlc_load.json").read_bytes()

    report = sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)

    assert report.playset_written is False
    assert report.direct_dlc_load_written is True
    assert report.is_effective is False
    assert report.active_playset_id is None
    assert report.effective_launcher_ids == []
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [registry]
    assert (user_dir / "dlc_load.json").read_bytes() != before_dlc
    assert _playset_members(user_dir) == [
        ("playset-older", 0, 0, 7),
        ("playset-newer", 0, 0, 3),
    ]


def test_paradox_activation_has_no_playset_activation_write() -> None:
    hits: list[str] = []
    for path in _PLAYSET_OWNERSHIP_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for hit in _playset_activation_writes(tree):
            hits.append(f"{path}: {hit}")
    assert hits == []


def _launcher_set_active(user_dir: Path, playset_id: str | None) -> None:
    """Fixture stands in for the Launcher. Production code must not do this."""
    con = sqlite3.connect(str(user_dir / "launcher-v2.sqlite"))
    try:
        con.execute("UPDATE playsets SET isActive = 0")
        if playset_id:
            con.execute(
                "UPDATE playsets SET isActive = 1 WHERE id = ?",
                (playset_id,),
            )
        con.commit()
    finally:
        con.close()


def _mark_deployed(db: DatabaseManager, pk: str, entity: str) -> None:
    db.update_mod_deploy_status(
        pk,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        deploy_path="",
        app_id=CK3,
    )
    persist_load_order([entity], db, app_id=CK3)


def test_sync_follows_active_playset_and_reports_effective(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    _mark_deployed(db, pk, entity)
    registry = workshop_launcher_id(SYNTHETIC_WORKSHOP_ID)
    _seed_playset_db(
        user_dir,
        [("playset-a", "A", 1, 1)],
        [("playset-a", SYNTHETIC_WORKSHOP_ID, registry, 0, 9)],
    )

    report = sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)

    assert report.playset_written is True
    assert report.direct_dlc_load_written is True
    assert report.is_effective is True
    assert report.resilience_status == RESILIENCE_DEPLOYED_EFFECTIVE
    assert report.active_playset_id == "playset-a"
    assert report.effective_launcher_ids == [registry]
    assert report.enabled_launcher_ids == [registry]
    assert _playset_activation(user_dir) == [("playset-a", 1)]
    assert _playset_members(user_dir) == [("playset-a", 1, 1, 0)]


def test_inactive_enabled_member_is_not_effective(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    _mark_deployed(db, pk, entity)
    registry = workshop_launcher_id(SYNTHETIC_WORKSHOP_ID)
    _seed_playset_db(
        user_dir,
        [("playset-initial", "Initial playset", 0, 1)],
        [("playset-initial", SYNTHETIC_WORKSHOP_ID, registry, 1, 15)],
    )

    state = get_paradox_effective_state(db, app_id=CK3, user_dir=user_dir)

    assert state.active_playset_id is None
    assert state.effective_launcher_ids == []
    assert state.read_error == ""


def test_sync_follows_whichever_playset_is_active(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    _mark_deployed(db, pk, entity)
    registry = workshop_launcher_id(SYNTHETIC_WORKSHOP_ID)
    _seed_playset_db(
        user_dir,
        [
            ("playset-a", "A", 1, 10),
            ("playset-b", "B", 0, 20),
        ],
        [
            ("playset-a", SYNTHETIC_WORKSHOP_ID, registry, 0, 4),
            ("playset-b", SYNTHETIC_WORKSHOP_ID, registry, 0, 8),
        ],
    )

    first = sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    assert first.active_playset_id == "playset-a"
    assert first.playset_written is True
    assert first.direct_dlc_load_written is True
    assert first.is_effective is True
    assert json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))[
        "enabled_mods"
    ] == [registry]
    assert _playset_members(user_dir) == [
        ("playset-a", 1, 1, 0),
        ("playset-b", 0, 0, 8),
    ]

    _launcher_set_active(user_dir, "playset-b")
    second = sync_paradox_launcher(db, app_id=CK3, user_dir=user_dir)
    assert second.active_playset_id == "playset-b"
    assert second.playset_written is True
    assert second.direct_dlc_load_written is True
    assert second.is_effective is True
    assert _playset_activation(user_dir) == [("playset-a", 0), ("playset-b", 1)]
    assert _playset_members(user_dir) == [
        ("playset-a", 0, 1, 0),
        ("playset-b", 1, 1, 0),
    ]


def test_paradox_deploy_inactive_keeps_success_and_is_not_effective(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    persist_load_order([entity], db, app_id=CK3)
    registry = workshop_launcher_id(SYNTHETIC_WORKSHOP_ID)
    _seed_playset_db(
        user_dir,
        [("playset-initial", "Initial playset", 0, 1)],
        [("playset-initial", SYNTHETIC_WORKSHOP_ID, registry, 1, 15)],
    )
    before_dlc = (user_dir / "dlc_load.json").read_bytes()
    before_members = _playset_members(user_dir)

    result = ModDeployer(library_root=library, db=db).deploy_mod(entity)

    assert result.get("success") is True, result
    assert result.get("deployment_status") == "deployed"
    assert result.get("launcher_effective") is False
    assert result.get("playset_written") is False
    assert result.get("direct_dlc_load_written") is True
    assert _deploy_status(db, pk) == DEPLOY_STATUS_DEPLOYED
    assert _playset_activation(user_dir) == [("playset-initial", 0)]
    assert _playset_members(user_dir) == before_members
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [registry]
    assert (user_dir / "dlc_load.json").read_bytes() != before_dlc
    state = get_paradox_effective_state(db, app_id=CK3, user_dir=user_dir)
    assert state.effective_launcher_ids == []


def test_paradox_sort_inactive_updates_order_and_is_not_effective(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    second_id = "2222222222"
    _write_extra_ugc(user_dir, second_id)
    pk_a, entity_a, _a = _seed_ck3_mod(library, db, folder="A")
    pk_b, entity_b, _b = _seed_ck3_mod(
        library, db, workshop_id=second_id, folder="B"
    )
    for pk in (pk_a, pk_b):
        db.update_mod_deploy_status(
            pk,
            deploy_status=DEPLOY_STATUS_DEPLOYED,
            deploy_path="",
            app_id=CK3,
        )
    persist_load_order([entity_a, entity_b], db, app_id=CK3)
    _seed_playset_db(
        user_dir,
        [("playset-initial", "Initial playset", 0, 1)],
        [
            (
                "playset-initial",
                SYNTHETIC_WORKSHOP_ID,
                workshop_launcher_id(SYNTHETIC_WORKSHOP_ID),
                1,
                0,
            ),
            (
                "playset-initial",
                second_id,
                workshop_launcher_id(second_id),
                1,
                1,
            ),
        ],
    )
    before_members = _playset_members(user_dir)
    backend = get_order_backend(CK3, "Crusader Kings III")
    assert backend is not None
    notices: list[str] = []

    def _forbid_notice(*_a, **_k):
        notices.append("prompt")
        raise AssertionError("Sort must not prompt for an inactive playset")

    monkeypatch.setattr(
        "services.paradox_activation.consider_paradox_playset_notice",
        _forbid_notice,
    )

    moved = backend.apply_order_move(entity_a, ORDER_MOVE_BOTTOM, db=db)
    report = backend.sync_projection(db=db)
    assert notices == []

    assert moved[0] == entity_b
    assert load_saved_order(app_id=CK3) == moved
    assert _playset_activation(user_dir) == [("playset-initial", 0)]
    assert _playset_members(user_dir) == before_members
    assert report.is_effective is False
    assert report.playset_written is False
    assert report.direct_dlc_load_written is True
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [
        workshop_launcher_id(second_id),
        workshop_launcher_id(SYNTHETIC_WORKSHOP_ID),
    ]
    assert report.resilience_status == RESILIENCE_DEPLOYED_NOT_EFFECTIVE


def test_paradox_inactive_notice_is_deduped(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    notice_path = tmp_path / "notice.json"
    monkeypatch.setenv("SMM_PARADOX_PLAYSET_NOTICE", str(notice_path))
    user_dir, _workshop, library = _configure_ck3(db, tmp_path)
    pk, entity, _managed = _seed_ck3_mod(library, db)
    persist_load_order([entity], db, app_id=CK3)
    registry = workshop_launcher_id(SYNTHETIC_WORKSHOP_ID)
    _seed_playset_db(
        user_dir,
        [("playset-initial", "Initial playset", 0, 1)],
        [("playset-initial", SYNTHETIC_WORKSHOP_ID, registry, 1, 4)],
    )
    marker = f"{CK3}:{NO_ACTIVE_PLAYSET_SENTINEL}"
    deployer = ModDeployer(library_root=library, db=db)

    first = deployer.deploy_mod(entity)
    assert first.get("success") is True
    assert first.get("playset_notice") == PARADOX_INACTIVE_PLAYSET_MESSAGE
    assert "部署失败" not in first["playset_notice"]
    assert json.loads(notice_path.read_text(encoding="utf-8"))[marker] is True

    backend = get_order_backend(CK3, "Crusader Kings III")
    assert backend is not None
    backend.apply_order_move(entity, ORDER_MOVE_BOTTOM, db=db)
    backend.sync_projection(db=db)
    assert json.loads(notice_path.read_text(encoding="utf-8"))[marker] is True
    assert _playset_activation(user_dir) == [("playset-initial", 0)]

    startup = consider_paradox_playset_notice(
        db, app_id=CK3, user_dir=user_dir, notice_path=notice_path
    )
    assert startup.should_prompt is False
    second = deployer.deploy_mod(entity)
    assert second.get("success") is True
    assert "playset_notice" not in second

    _launcher_set_active(user_dir, "playset-initial")
    cleared = consider_paradox_playset_notice(
        db, app_id=CK3, user_dir=user_dir, notice_path=notice_path
    )
    assert cleared.should_prompt is False
    assert marker not in json.loads(notice_path.read_text(encoding="utf-8"))

    _launcher_set_active(user_dir, None)
    again = deployer.deploy_mod(entity)
    assert again.get("success") is True
    assert again.get("playset_notice") == PARADOX_INACTIVE_PLAYSET_MESSAGE
    assert _playset_activation(user_dir) == [("playset-initial", 0)]


def test_real_stellaris_effective_state_matches_launcher_db() -> None:
    user_dir = Path.home() / "Documents" / "Paradox Interactive" / "Stellaris"
    db_path = user_dir / "launcher-v2.sqlite"
    dlc_path = user_dir / "dlc_load.json"
    assert db_path.is_file(), db_path
    before_db = (db_path.stat().st_mtime_ns, db_path.stat().st_size)
    before_dlc = (
        (dlc_path.stat().st_mtime_ns, dlc_path.stat().st_size)
        if dlc_path.is_file()
        else None
    )
    uri = db_path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        active = con.execute(
            "SELECT id FROM playsets WHERE isActive = 1 ORDER BY createdOn DESC LIMIT 1"
        ).fetchone()
        if active is None:
            expected_id = None
            expected_ids: list[str] = []
        else:
            expected_id = str(active[0])
            expected_ids = [
                str(row[0])
                for row in con.execute(
                    """
                    SELECT m.gameRegistryId
                    FROM playsets_mods pm
                    JOIN mods m ON m.id = pm.modId
                    WHERE pm.playsetId = ? AND CAST(pm.enabled AS INTEGER) = 1
                    ORDER BY pm.position
                    """,
                    (expected_id,),
                )
                if str(row[0] or "").strip()
            ]
        snapshot = con.execute(
            "SELECT id, isActive FROM playsets ORDER BY id"
        ).fetchall()
    finally:
        con.close()

    state = get_paradox_effective_state(app_id=STELLARIS, user_dir=user_dir)

    assert state.read_error == ""
    assert state.active_playset_id == expected_id
    assert state.effective_launcher_ids == expected_ids
    con = sqlite3.connect(uri, uri=True)
    try:
        after = con.execute(
            "SELECT id, isActive FROM playsets ORDER BY id"
        ).fetchall()
    finally:
        con.close()
    assert after == snapshot
    assert (db_path.stat().st_mtime_ns, db_path.stat().st_size) == before_db
    if before_dlc is not None:
        assert (dlc_path.stat().st_mtime_ns, dlc_path.stat().st_size) == before_dlc
