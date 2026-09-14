"""Stellaris activation, load order, and launcher sync (App ID 281990)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DEPLOY_STATUS_NOT_DEPLOYED, DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM, STELLARIS_APP_IDS
from core.paths import data_dir, load_order_dir
from services.deploy import ModDeployer
from services.identity_service import create_mod_identity, identity_create_scope
from services.stellaris_activation import (
    STELLARIS_APP_ID,
    STELLARIS_ORDER_FILENAME,
    apply_card_drop,
    default_stellaris_user_dir,
    deployed_load_order_tokens,
    discover_workshop_mods,
    enabled_load_order_tokens,
    is_stellaris_activation_app,
    list_installed_stellaris_mods,
    load_saved_order,
    map_to_launcher_id,
    merge_stellaris_enabled_mods,
    non_smm_enabled_entries,
    persist_load_order,
    resolved_load_order,
    set_stellaris_enabled,
    sync_stellaris_launcher,
    workshop_launcher_id,
)
from tests.helpers.identity import write_info_sidecar
from services.wh3_activation import is_wh3_activation_app

STELLARIS = 281990
assert STELLARIS == STELLARIS_APP_ID
assert STELLARIS in STELLARIS_APP_IDS


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "stellaris_activation.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _configure_stellaris(
    db: DatabaseManager,
    tmp_path: Path,
) -> tuple[Path, Path]:
    user_dir = tmp_path / "Paradox" / "Stellaris"
    workshop = tmp_path / "workshop" / "content" / str(STELLARIS)
    user_dir.mkdir(parents=True)
    workshop.mkdir(parents=True)
    (user_dir / "dlc_load.json").write_text(
        json.dumps({"enabled_mods": [], "disabled_dlcs": ["fake_dlc"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    db.upsert_game(
        GameInfo(app_id=STELLARIS, name="Stellaris", folder_name="Stellaris")
    )
    db.update_game_deploy_config(
        STELLARIS,
        name="Stellaris",
        install_path=str(tmp_path / "StellarisInstall"),
        mod_path=str(user_dir),
        workshop_path=str(workshop),
    )
    return user_dir, workshop


def _write_ugc_descriptor(user_dir: Path, workshop_id: str, *, name: str = "Mod") -> Path:
    mod_dir = user_dir / "mod"
    mod_dir.mkdir(parents=True, exist_ok=True)
    path = mod_dir / f"ugc_{workshop_id}.mod"
    path.write_text(
        f'name="{name}"\npath="C:/workshop/{workshop_id}"\nremote_file_id="{workshop_id}"\n',
        encoding="utf-8",
    )
    return path


def _seed_stellaris_mod(
    library: Path,
    db: DatabaseManager,
    *,
    folder: str,
    workshop_id: str,
    enabled: bool = True,
    deployed: bool = False,
) -> str:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(workshop_id),
            workshop_id=str(workshop_id),
            title=folder,
            app_id=STELLARIS,
            game_name="Stellaris",
            operation="import",
        )
    pk = str(created.mod_id)
    entity = str(created.internal_id or "")
    mod_dir = library / "Stellaris" / folder
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
        app_id=STELLARIS,
        game_name="Stellaris",
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
            app_id=STELLARIS,
        )
    if not enabled:
        db.disable_mod(pk)
    return pk


def _mark_deployed(db: DatabaseManager, pk: str, *, deployed: bool = True) -> None:
    db.update_mod_deploy_status(
        pk,
        deploy_status=DEPLOY_STATUS_DEPLOYED if deployed else DEPLOY_STATUS_NOT_DEPLOYED,
        deploy_path="",
        app_id=STELLARIS,
    )


def _entity_internal_id(db: DatabaseManager, pk: str) -> str:
    with db._lock:
        row = db._conn.execute(
            "SELECT internal_id FROM mods WHERE mod_id = ?",
            (int(pk),),
        ).fetchone()
    return str(row["internal_id"] or "") if row is not None else ""


def _workspace_id(db: DatabaseManager, pk: str) -> str:
    with db._lock:
        row = db._conn.execute(
            "SELECT workspace_id FROM mods WHERE mod_id = ?",
            (int(pk),),
        ).fetchone()
    return str(row["workspace_id"] or "") if row is not None else ""


def test_stellaris_app_id_enters_activation() -> None:
    assert is_stellaris_activation_app(281990) is True
    assert is_stellaris_activation_app("281990") is True
    assert is_stellaris_activation_app(0, "Stellaris") is True
    assert is_stellaris_activation_app(0, "群星") is True
    assert is_wh3_activation_app(281990) is False


def test_non_stellaris_games_do_not_enter_activation() -> None:
    assert is_stellaris_activation_app(1142710) is False
    assert is_stellaris_activation_app(1623730, "Palworld") is False
    assert is_stellaris_activation_app(292030, "The Witcher 3") is False


def test_workshop_mod_discovery(tmp_path: Path, db: DatabaseManager) -> None:
    _user_dir, workshop = _configure_stellaris(db, tmp_path)
    first = workshop / "1623423360"
    first.mkdir()
    (first / "descriptor.mod").write_text(
        'name="UI Overhaul Dynamic"\nremote_file_id="1623423360"\n',
        encoding="utf-8",
    )
    second = workshop / "1886496498"
    second.mkdir()
    hits = discover_workshop_mods(workshop)
    assert [hit.workspace_id for hit in hits] == ["1623423360", "1886496498"]
    assert hits[0].remote_file_id == "1623423360"
    assert hits[0].name == "UI Overhaul Dynamic"


def test_enable_disable_does_not_drive_launcher_membership(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_stellaris_mod(library, db, folder="A", workshop_id="1001", deployed=True)
    b = _seed_stellaris_mod(library, db, folder="B", workshop_id="1002")
    _write_ugc_descriptor(user_dir, "1001", name="A")
    _write_ugc_descriptor(user_dir, "1002", name="B")
    persist_load_order([a, b], db)

    assert set_stellaris_enabled(a, False, db) is True
    assert db.is_mod_enabled(a) is False
    assert set_stellaris_enabled(b, True, db) is True
    assert db.is_mod_enabled(b) is True

    report = sync_stellaris_launcher(db, user_dir=user_dir)
    assert report.written is True
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == ["mod/ugc_1001.mod"]
    assert "mod/ugc_1002.mod" not in payload["enabled_mods"]
    assert payload["disabled_dlcs"] == ["fake_dlc"]
    saved = json.loads((load_order_dir() / STELLARIS_ORDER_FILENAME).read_text(encoding="utf-8"))
    assert saved["order"] == [a, b]
    assert deployed_load_order_tokens(db) == [a]


def test_drag_reorder_save_and_reload(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_stellaris_mod(library, db, folder="A", workshop_id="2001")
    b = _seed_stellaris_mod(library, db, folder="B", workshop_id="2002")
    c = _seed_stellaris_mod(library, db, folder="C", workshop_id="2003")
    for wid in ("2001", "2002", "2003"):
        _write_ugc_descriptor(user_dir, wid)
    persist_load_order([a, b, c], db)
    for pk in (a, b, c):
        _mark_deployed(db, pk)
    apply_card_drop(c, a, db)
    assert load_saved_order() == [c, a, b]
    assert resolved_load_order(db) == [c, a, b]
    sync_stellaris_launcher(db, user_dir=user_dir)
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [
        "mod/ugc_2003.mod",
        "mod/ugc_2001.mod",
        "mod/ugc_2002.mod",
    ]
    assert load_saved_order() == [c, a, b]


def test_missing_mod_stays_in_file_until_persist(
    tmp_path: Path, db: DatabaseManager
) -> None:
    _configure_stellaris(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_stellaris_mod(library, db, folder="A", workshop_id="3001")
    b = _seed_stellaris_mod(library, db, folder="B", workshop_id="3002")
    from services.stellaris_activation import save_saved_order

    save_saved_order([a, "999999", b])
    assert load_saved_order() == [a, "999999", b]
    assert resolved_load_order(db) == [a, b]
    assert load_saved_order() == [a, "999999", b]


def test_new_mod_appends_without_disturbing_order(
    tmp_path: Path, db: DatabaseManager
) -> None:
    _configure_stellaris(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_stellaris_mod(library, db, folder="A", workshop_id="4001")
    b = _seed_stellaris_mod(library, db, folder="B", workshop_id="4002")
    persist_load_order([b, a], db)
    c = _seed_stellaris_mod(library, db, folder="C", workshop_id="4003")
    assert resolved_load_order(db) == [b, a, c]


def test_unresolved_mod_skipped_without_creating_launcher_entry(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_stellaris_mod(library, db, folder="A", workshop_id="5001")
    missing = _seed_stellaris_mod(library, db, folder="Missing", workshop_id="5002")
    _write_ugc_descriptor(user_dir, "5001")
    persist_load_order([a, missing], db)
    _mark_deployed(db, a)
    _mark_deployed(db, missing)
    before = list((user_dir / "mod").iterdir())
    report = sync_stellaris_launcher(db, user_dir=user_dir)
    assert missing in report.unresolved
    assert report.enabled_launcher_ids == ["mod/ugc_5001.mod"]
    assert not (user_dir / "mod" / "ugc_5002.mod").exists()
    assert list((user_dir / "mod").iterdir()) == before
    with db._lock:
        row = db._conn.execute(
            "SELECT 1 FROM mods WHERE mod_id = ?",
            (int(missing),),
        ).fetchone()
    assert row is not None


def test_identity_mapping_not_polluted(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_stellaris_mod(library, db, folder="A", workshop_id="1623423360")
    _write_ugc_descriptor(user_dir, "1623423360", name="UI Overhaul Dynamic")
    persist_load_order([a], db)
    _mark_deployed(db, a)
    entity = _entity_internal_id(db, a)
    workspace = _workspace_id(db, a)
    assert entity
    assert workspace == "1623423360"
    assert entity != workspace
    launcher = workshop_launcher_id(workspace)
    assert launcher == "mod/ugc_1623423360.mod"
    assert launcher != a
    assert launcher != entity
    assert launcher != workspace
    refs = {ref.token: ref for ref in list_installed_stellaris_mods(db)}
    mapping = map_to_launcher_id(refs[a], user_dir=user_dir)
    assert mapping.available is True
    assert mapping.launcher_id == launcher
    sync_stellaris_launcher(db, user_dir=user_dir)
    saved = json.loads((load_order_dir() / STELLARIS_ORDER_FILENAME).read_text(encoding="utf-8"))
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert saved["order"] == [a]
    assert entity not in payload["enabled_mods"]
    assert workspace not in payload["enabled_mods"]
    assert payload["enabled_mods"] == [launcher]


def _seed_launcher_sqlite(
    user_dir: Path,
    *,
    enabled: list[tuple[str, str]],
    disabled: list[tuple[str, str]] | None = None,
) -> None:
    path = user_dir / "launcher-v2.sqlite"
    con = sqlite3.connect(str(path))
    cur = con.cursor()
    cur.executescript(
        """
        CREATE TABLE dlc (
            id char(36) not null,
            name varchar(255) not null,
            dirPath varchar(255) not null,
            primary key (id)
        );
        CREATE TABLE key_value_pairs (
            name varchar(255) not null,
            value varchar(255) null,
            primary key (name)
        );
        CREATE TABLE mods (
            id char(36) not null,
            steamId varchar(255),
            gameRegistryId text,
            name varchar(255),
            displayName varchar(255),
            status text not null,
            source text not null,
            isMetadataApplied boolean not null default '0',
            metadataStatus text not null default 'not_applied',
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
        "INSERT INTO dlc(id, name, dirPath) VALUES (?, ?, ?)",
        ("dlc-keep", "Keep DLC", "dlc/keep"),
    )
    cur.execute(
        "INSERT INTO key_value_pairs(name, value) VALUES (?, ?)",
        ("lastModsCompatibilityVersion", "4.4"),
    )
    cur.execute(
        """
        INSERT INTO playsets(id, name, isActive, loadOrder, createdOn, isRemoved)
        VALUES (?, ?, 1, NULL, 1, 0)
        """,
        ("playset-1", "Initial playset"),
    )
    position = 0
    for steam_id, registry in enabled:
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
        position += 1
    for steam_id, registry in disabled or []:
        mod_pk = f"mod-{steam_id}"
        cur.execute(
            """
            INSERT INTO mods(id, steamId, gameRegistryId, displayName, status, source)
            VALUES (?, ?, ?, ?, 'ready_to_play', 'steam')
            """,
            (mod_pk, steam_id, registry, steam_id),
        )
        cur.execute(
            "INSERT INTO playsets_mods(playsetId, modId, enabled, position) VALUES (?, ?, 0, ?)",
            ("playset-1", mod_pk, position),
        )
        position += 1
    con.commit()
    con.close()


def test_order_sync_preserves_unrelated_launcher_state(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_stellaris_mod(library, db, folder="A", workshop_id="6001")
    b = _seed_stellaris_mod(library, db, folder="B", workshop_id="6002")
    _write_ugc_descriptor(user_dir, "6001")
    _write_ugc_descriptor(user_dir, "6002")
    (user_dir / "dlc_load.json").write_text(
        json.dumps(
            {
                "enabled_mods": ["mod/ugc_6001.mod", "mod/unmanaged.mod"],
                "disabled_dlcs": ["fake_dlc"],
                "keep_me": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _seed_launcher_sqlite(
        user_dir,
        enabled=[
            ("6001", "mod/ugc_6001.mod"),
            ("keep", "mod/unmanaged.mod"),
        ],
        disabled=[("6002", "mod/ugc_6002.mod")],
    )
    persist_load_order([b, a], db)
    _mark_deployed(db, a)
    _mark_deployed(db, b)
    report = sync_stellaris_launcher(db, user_dir=user_dir)
    assert report.written is True
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    assert payload["enabled_mods"] == [
        "mod/ugc_6002.mod",
        "mod/ugc_6001.mod",
        "mod/unmanaged.mod",
    ]
    assert payload["disabled_dlcs"] == ["fake_dlc"]
    assert payload["keep_me"] is True
    con = sqlite3.connect(str(user_dir / "launcher-v2.sqlite"))
    con.row_factory = sqlite3.Row
    playset = con.execute("SELECT name, isActive FROM playsets").fetchone()
    assert playset["name"] == "Initial playset"
    assert int(playset["isActive"]) == 1
    dlc = con.execute("SELECT id, name FROM dlc").fetchone()
    assert dict(dlc) == {"id": "dlc-keep", "name": "Keep DLC"}
    version = con.execute(
        "SELECT value FROM key_value_pairs WHERE name = 'lastModsCompatibilityVersion'"
    ).fetchone()["value"]
    assert version == "4.4"
    enabled_rows = {
        row["gameRegistryId"]: (int(row["enabled"]), int(row["position"]))
        for row in con.execute(
            """
            SELECT m.gameRegistryId, pm.enabled, pm.position
            FROM playsets_mods pm JOIN mods m ON m.id = pm.modId
            """
        )
    }
    assert enabled_rows["mod/ugc_6002.mod"][0] == 1
    assert enabled_rows["mod/ugc_6001.mod"][0] == 1
    assert enabled_rows["mod/unmanaged.mod"][0] == 1
    assert enabled_rows["mod/ugc_6002.mod"][1] < enabled_rows["mod/ugc_6001.mod"][1]
    con.close()


def test_stellaris_order_file_lives_under_config(
    tmp_path: Path, db: DatabaseManager
) -> None:
    _configure_stellaris(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_stellaris_mod(library, db, folder="A", workshop_id="7001")
    persist_load_order([a], db)
    path = load_order_dir() / STELLARIS_ORDER_FILENAME
    assert path.is_file()
    assert not (data_dir() / "stellaris").exists()
    assert not (data_dir() / "load_order").exists()
    assert not (data_dir() / "281990").exists()


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_stellaris_sort_mode_visible(qapp, tmp_path: Path, db: DatabaseManager) -> None:
    from ui.library_view import ModLibraryView

    _configure_stellaris(db, tmp_path)
    view = ModLibraryView()
    view._set_current_game_context("Stellaris", game_id=STELLARIS)
    assert view.btn_wh3_sort_mode.isHidden() is False
    assert not hasattr(view, "btn_stellaris_auto_sort")
    assert not hasattr(view, "_on_stellaris_auto_sort")
    button_texts = [
        str(btn.text() or "")
        for btn in view.findChildren(type(view.btn_wh3_sort_mode))
    ]
    assert all("自动排序" not in text for text in button_texts)
    view.deleteLater()


def test_stellaris_auto_sort_removed() -> None:
    import importlib

    root = Path(__file__).resolve().parents[1]
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("services.stellaris_auto_sort")
    assert not (root / "services" / "stellaris_auto_sort.py").exists()
    assert not (root / "tests" / "test_stellaris_auto_sort.py").exists()
    view_src = (root / "ui" / "library_view.py").read_text(encoding="utf-8")
    assert "stellaris_auto_sort" not in view_src
    assert "btn_stellaris_auto_sort" not in view_src
    assert "_on_stellaris_auto_sort" not in view_src
    assert "preview_auto_sort" not in view_src
    assert "apply_auto_sort" not in view_src
    act_src = (root / "services" / "stellaris_activation.py").read_text(encoding="utf-8")
    assert "parse_descriptor_dependencies" not in act_src
    assert "descriptor_has_replace_path" not in act_src


def _filtered_mod_ids(view) -> list[str]:
    return [str(index.mod_id) for index, _payload in view._filtered_row_entries]


def _visible_card_ids(view) -> list[str]:
    return [str(card._mod_id()) for card in view._cards]


def _host_visible_card_ids(view) -> list[str]:
    from ui.mod_card import ModCardWidget

    host = view._cards_host if view._cards_host is not None else view.library_host
    ids: list[str] = []
    for widget in host.findChildren(ModCardWidget):
        if widget.isHidden():
            continue
        mid = str(widget._mod_id() or "").strip()
        if mid:
            ids.append(mid)
    return ids


def _write_enabled_mods(user_dir: Path, workshop_ids: list[str]) -> None:
    path = user_dir / 'dlc_load.json'
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['enabled_mods'] = [workshop_launcher_id(wid) for wid in workshop_ids]
    path.write_text(json.dumps(payload, ensure_ascii=False) + '\n', encoding='utf-8')


def _deploy_status_of(db: DatabaseManager, pk: str) -> str:
    info = db.get_mod_deploy_info(pk)
    return str(getattr(info, "deploy_status", "") or "")


def test_launcher_enabled_is_not_smm_membership(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / 'mod'
    ids: list[str] = []
    for i in range(10):
        ids.append(
            _seed_stellaris_mod(
                library,
                db,
                folder=f'Lib{i:02d}',
                workshop_id=str(8300 + i),
                enabled=True,
            )
        )
    _write_enabled_mods(user_dir, [str(8300 + i) for i in range(3)])
    db.disable_mod(ids[0])
    assert enabled_load_order_tokens(db, user_dir=user_dir) == ids[:3]
    assert deployed_load_order_tokens(db) == []
    for pk in ids:
        assert _deploy_status_of(db, pk) != DEPLOY_STATUS_DEPLOYED


def test_stellaris_sort_mode_click_shows_deployed_not_launcher(
    qapp, tmp_path: Path, db: DatabaseManager
) -> None:
    from PySide6.QtWidgets import QApplication
    from ui.library_query import FILTER_ALL
    from ui.library_view import ModLibraryView

    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / 'mod'
    all_ids: list[str] = []
    for i in range(10):
        all_ids.append(
            _seed_stellaris_mod(
                library,
                db,
                folder=f'Lib{i:02d}',
                workshop_id=str(8400 + i),
                enabled=True,
                deployed=i < 3,
            )
        )
        _write_ugc_descriptor(user_dir, str(8400 + i), name=f'Lib{i:02d}')
    deployed_ids = all_ids[:3]
    inactive_ids = all_ids[3:]
    _write_enabled_mods(user_dir, [str(8400 + i) for i in range(10)])
    persist_load_order(all_ids, db)
    identity_before = {
        pk: (_entity_internal_id(db, pk), _workspace_id(db, pk)) for pk in all_ids
    }

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.set_preferred_filter('Stellaris')
    view.refresh()
    library_ids = _filtered_mod_ids(view)
    assert len(library_ids) == 10
    assert set(library_ids) == set(all_ids)
    assert all(bool(index.enabled) for index, _payload in view._game_row_entries)
    shown_deployed = {
        str(index.mod_id)
        for index, _payload in view._game_row_entries
        if bool(index.deployed)
    }
    assert shown_deployed == set(deployed_ids)
    assert set(enabled_load_order_tokens(db, user_dir=user_dir)) == set(all_ids)

    view.btn_wh3_sort_mode.click()
    QApplication.processEvents()
    assert view._wh3_sort_mode is True
    filtered = _filtered_mod_ids(view)
    visible = _visible_card_ids(view)
    host_visible = _host_visible_card_ids(view)
    print('SORT_MODE =', filtered)
    print('VISIBLE_CARDS =', visible)
    print('ACTUAL_WIDGET_IDS =', host_visible)
    print('count_label =', view.count_label.text())
    assert set(filtered) == set(deployed_ids)
    assert set(visible) == set(deployed_ids)
    assert set(host_visible) == set(deployed_ids)
    assert view.count_label.text().startswith('3 Mods')
    assert all(mid not in filtered for mid in inactive_ids)
    assert all(mid not in host_visible for mid in inactive_ids)
    numbers = [int(card.load_order_badge.text()) for card in view._cards]
    assert numbers == list(range(1, len(numbers) + 1))

    view.search_box.setText('Lib09')
    view._last_filter_sig = None
    view._apply_view_filter()
    QApplication.processEvents()
    assert _filtered_mod_ids(view) == []
    assert _visible_card_ids(view) == []

    view.search_box.setText('Lib00')
    view._last_filter_sig = None
    view._apply_view_filter()
    QApplication.processEvents()
    assert _filtered_mod_ids(view) == [deployed_ids[0]]
    assert _visible_card_ids(view) == [deployed_ids[0]]

    view.search_box.clear()
    view._last_filter_sig = None
    view._apply_view_filter()
    QApplication.processEvents()
    assert set(_filtered_mod_ids(view)) == set(deployed_ids)

    view.resize(1400, 900)
    view._sync_viewport_cards(scroll_y=0)
    QApplication.processEvents()
    assert set(_visible_card_ids(view)) == set(deployed_ids)
    assert set(_host_visible_card_ids(view)) == set(deployed_ids)

    view.refresh()
    QApplication.processEvents()
    assert view._wh3_sort_mode is True
    assert set(_filtered_mod_ids(view)) == set(deployed_ids)
    assert set(_visible_card_ids(view)) == set(deployed_ids)
    assert set(_host_visible_card_ids(view)) == set(deployed_ids)

    view._on_wh3_sort_drop(deployed_ids[-1], deployed_ids[0])
    QApplication.processEvents()
    reordered = _filtered_mod_ids(view)
    assert reordered[0] == deployed_ids[-1]
    assert set(reordered) == set(deployed_ids)
    saved = json.loads(
        (load_order_dir() / STELLARIS_ORDER_FILENAME).read_text(encoding='utf-8')
    )
    assert set(saved.keys()) == {'order'}
    assert set(saved['order']) == set(all_ids)
    after_sync = json.loads((user_dir / 'dlc_load.json').read_text(encoding='utf-8'))
    assert after_sync['enabled_mods'] == [
        workshop_launcher_id(str(8400 + i)) for i in (2, 0, 1)
    ]
    identity_after = {
        pk: (_entity_internal_id(db, pk), _workspace_id(db, pk)) for pk in all_ids
    }
    assert identity_after == identity_before

    _write_enabled_mods(user_dir, [str(8400 + i) for i in range(2)])
    view.refresh()
    QApplication.processEvents()
    assert set(_filtered_mod_ids(view)) == set(deployed_ids)
    assert set(_visible_card_ids(view)) == set(deployed_ids)
    assert set(_host_visible_card_ids(view)) == set(deployed_ids)

    view.btn_wh3_sort_mode.click()
    QApplication.processEvents()
    assert view._wh3_sort_mode is False
    restored = _filtered_mod_ids(view)
    assert len(restored) == 10
    assert set(restored) == set(all_ids)
    assert set(_host_visible_card_ids(view)) == set(all_ids)
    assert view._status_filter == FILTER_ALL
    QApplication.processEvents()
    view.deleteLater()
    QApplication.processEvents()

    view2 = ModLibraryView()
    view2.set_target_root(str(library))
    view2.set_preferred_filter('Stellaris')
    view2.refresh()
    QApplication.processEvents()
    assert len(_filtered_mod_ids(view2)) == 10
    view2.btn_wh3_sort_mode.click()
    QApplication.processEvents()
    restarted = _filtered_mod_ids(view2)
    visible2 = _visible_card_ids(view2)
    expected = deployed_load_order_tokens(db)
    print('AFTER_RESTART_SORT_MODE =', restarted)
    print('AFTER_RESTART_VISIBLE =', visible2)
    print('AFTER_RESTART_DEPLOYED =', expected)
    assert set(restarted) == set(expected) == set(deployed_ids)
    assert set(visible2) == set(expected)
    assert set(_host_visible_card_ids(view2)) == set(expected)
    assert view2.count_label.text().startswith('3 Mods')
    QApplication.processEvents()
    view2.deleteLater()
    QApplication.processEvents()


def test_stellaris_sort_mode_zero_deployed_ignores_launcher_enabled(
    qapp, tmp_path: Path, db: DatabaseManager
) -> None:
    from PySide6.QtWidgets import QApplication
    from ui.library_view import ModLibraryView

    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / 'mod'
    ids = [
        _seed_stellaris_mod(
            library,
            db,
            folder=f'Lib{i}',
            workshop_id=str(8500 + i),
            enabled=True,
        )
        for i in range(4)
    ]
    _write_enabled_mods(user_dir, [str(8500 + i) for i in range(4)])
    persist_load_order(ids, db)
    view = ModLibraryView()
    view.set_target_root(str(library))
    view.set_preferred_filter('Stellaris')
    view.refresh()
    QApplication.processEvents()
    assert len(view._game_row_entries) == 4
    assert all(bool(index.enabled) for index, _payload in view._game_row_entries)
    assert all(not bool(index.deployed) for index, _payload in view._game_row_entries)
    view.btn_wh3_sort_mode.click()
    QApplication.processEvents()
    assert _filtered_mod_ids(view) == []
    assert _visible_card_ids(view) == []
    assert _host_visible_card_ids(view) == []
    assert not view.empty_overlay.isHidden()
    view.btn_wh3_sort_mode.click()
    QApplication.processEvents()
    restored = _filtered_mod_ids(view)
    assert len(restored) == 4
    assert set(restored) == set(ids)
    QApplication.processEvents()
    view.deleteLater()
    QApplication.processEvents()


def test_stellaris_sort_mode_deployed_with_empty_launcher(
    qapp, tmp_path: Path, db: DatabaseManager
) -> None:
    from PySide6.QtWidgets import QApplication
    from ui.library_view import ModLibraryView

    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / 'mod'
    ids = [
        _seed_stellaris_mod(
            library,
            db,
            folder=f'Lib{i}',
            workshop_id=str(8600 + i),
            enabled=True,
            deployed=i < 3,
        )
        for i in range(6)
    ]
    _write_enabled_mods(user_dir, [])
    persist_load_order(ids, db)
    view = ModLibraryView()
    view.set_target_root(str(library))
    view.set_preferred_filter('Stellaris')
    view.refresh()
    QApplication.processEvents()
    view.btn_wh3_sort_mode.click()
    QApplication.processEvents()
    expected = ids[:3]
    assert set(_filtered_mod_ids(view)) == set(expected)
    assert set(_host_visible_card_ids(view)) == set(expected)
    assert view.count_label.text().startswith('3 Mods')
    view.deleteLater()
    QApplication.processEvents()


def test_stellaris_deploy_writes_b_then_syncs_a(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / 'mod'
    ids = [
        _seed_stellaris_mod(
            library,
            db,
            folder=f'Dep{i}',
            workshop_id=str(8700 + i),
        )
        for i in range(3)
    ]
    extra = _seed_stellaris_mod(library, db, folder='Extra', workshop_id='8799')
    for i in range(3):
        _write_ugc_descriptor(user_dir, str(8700 + i))
    _write_ugc_descriptor(user_dir, '8799')
    _write_enabled_mods(user_dir, ['8799', '8700', '8701', '8702'])
    persist_load_order([ids[2], ids[0], ids[1], extra], db)

    deployer = ModDeployer(library_root=library, db=db)
    for pk in ids:
        result = deployer.deploy_mod(pk)
        assert result.get('success') is True, result
        assert _deploy_status_of(db, pk) == DEPLOY_STATUS_DEPLOYED
    assert _deploy_status_of(db, extra) != DEPLOY_STATUS_DEPLOYED
    assert deployed_load_order_tokens(db) == [ids[2], ids[0], ids[1]]

    payload = json.loads((user_dir / 'dlc_load.json').read_text(encoding='utf-8'))
    assert payload['enabled_mods'] == [
        workshop_launcher_id('8702'),
        workshop_launcher_id('8700'),
        workshop_launcher_id('8701'),
    ]
    saved = json.loads((load_order_dir() / STELLARIS_ORDER_FILENAME).read_text(encoding='utf-8'))
    assert saved['order'] == [ids[2], ids[0], ids[1], extra]


def test_stellaris_undeploy_removes_from_launcher(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / 'mod'
    ids = [
        _seed_stellaris_mod(
            library,
            db,
            folder=f'Und{i}',
            workshop_id=str(8800 + i),
        )
        for i in range(3)
    ]
    for i in range(3):
        _write_ugc_descriptor(user_dir, str(8800 + i))
    persist_load_order(ids, db)
    deployer = ModDeployer(library_root=library, db=db)
    for pk in ids:
        assert deployer.deploy_mod(pk).get('success') is True
    removed = deployer.undeploy_mod(ids[1])
    assert removed.get('success') is True, removed
    assert _deploy_status_of(db, ids[1]) != DEPLOY_STATUS_DEPLOYED
    assert deployed_load_order_tokens(db) == [ids[0], ids[2]]
    payload = json.loads((user_dir / 'dlc_load.json').read_text(encoding='utf-8'))
    assert payload['enabled_mods'] == [
        workshop_launcher_id('8800'),
        workshop_launcher_id('8802'),
    ]
    saved = json.loads((load_order_dir() / STELLARIS_ORDER_FILENAME).read_text(encoding='utf-8'))
    assert saved['order'] == ids


def test_sync_corrects_launcher_without_adopting_into_smm(
    tmp_path: Path, db: DatabaseManager
) -> None:
    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / 'mod'
    ids = [
        _seed_stellaris_mod(
            library,
            db,
            folder=f'Sync{i}',
            workshop_id=str(8900 + i),
            deployed=i < 3,
        )
        for i in range(5)
    ]
    for i in range(5):
        _write_ugc_descriptor(user_dir, str(8900 + i))
    persist_load_order(ids, db)
    _write_enabled_mods(user_dir, [str(8900 + i) for i in range(5)])
    before = [_deploy_status_of(db, pk) for pk in ids]
    assert before == [
        DEPLOY_STATUS_DEPLOYED,
        DEPLOY_STATUS_DEPLOYED,
        DEPLOY_STATUS_DEPLOYED,
        DEPLOY_STATUS_NOT_DEPLOYED,
        DEPLOY_STATUS_NOT_DEPLOYED,
    ]
    assert enabled_load_order_tokens(db, user_dir=user_dir) == ids
    assert deployed_load_order_tokens(db) == ids[:3]

    report = sync_stellaris_launcher(db, user_dir=user_dir)
    assert report.written is True
    after = [_deploy_status_of(db, pk) for pk in ids]
    assert after == before
    payload = json.loads((user_dir / 'dlc_load.json').read_text(encoding='utf-8'))
    assert payload['enabled_mods'] == [
        workshop_launcher_id('8900'),
        workshop_launcher_id('8901'),
        workshop_launcher_id('8902'),
    ]
    _write_enabled_mods(user_dir, [])
    sync_stellaris_launcher(db, user_dir=user_dir)
    payload = json.loads((user_dir / 'dlc_load.json').read_text(encoding='utf-8'))
    assert payload['enabled_mods'] == [
        workshop_launcher_id('8900'),
        workshop_launcher_id('8901'),
        workshop_launcher_id('8902'),
    ]
    assert [_deploy_status_of(db, pk) for pk in ids] == before


DLC_A = "dlc008_ancientrelics"
DLC_B = "dlc010_utopia"
DLC_C = "dlc021_overlord"
UNKNOWN_MOD = "mod/unknown_xyz.mod"


def _assert_non_smm_preserved(before: list[object], after: list[object], managed_ids: set[str]) -> None:
    left = non_smm_enabled_entries(before, managed_ids=managed_ids)
    right = non_smm_enabled_entries(after, managed_ids=managed_ids)
    assert left == right
    assert len(left) == len(right)


def test_merge_preserves_dlc_and_reorders_only_smm_mods() -> None:
    mod_a = workshop_launcher_id("111")
    mod_b = workshop_launcher_id("222")
    original = [DLC_A, mod_a, DLC_B, mod_b, DLC_C]
    managed = {mod_a, mod_b}
    after = merge_stellaris_enabled_mods(
        original,
        managed_ids=managed,
        enabled_ordered=[mod_b, mod_a],
    )
    assert after == [DLC_A, mod_b, mod_a, DLC_B, DLC_C]
    _assert_non_smm_preserved(original, after, managed)


def test_merge_preserves_unknown_mod_and_dlc() -> None:
    known = workshop_launcher_id("111")
    original = [DLC_A, known, UNKNOWN_MOD, DLC_B]
    after = merge_stellaris_enabled_mods(
        original,
        managed_ids={known},
        enabled_ordered=[known],
    )
    assert UNKNOWN_MOD in after
    assert after == [DLC_A, known, UNKNOWN_MOD, DLC_B]
    _assert_non_smm_preserved(original, after, {known})


def test_merge_empty_deployed_removes_only_smm_mods() -> None:
    mod_a = workshop_launcher_id("111")
    mod_b = workshop_launcher_id("222")
    original = [DLC_A, mod_a, UNKNOWN_MOD, mod_b, DLC_B]
    managed = {mod_a, mod_b}
    after = merge_stellaris_enabled_mods(
        original,
        managed_ids=managed,
        enabled_ordered=[],
    )
    assert after == [DLC_A, UNKNOWN_MOD, DLC_B]
    _assert_non_smm_preserved(original, after, managed)


def test_sync_preserves_mixed_dlc_load_json(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_stellaris_mod(library, db, folder="A", workshop_id="9101", deployed=True)
    b = _seed_stellaris_mod(library, db, folder="B", workshop_id="9102", deployed=True)
    _write_ugc_descriptor(user_dir, "9101")
    _write_ugc_descriptor(user_dir, "9102")
    persist_load_order([b, a], db)
    original = [
        DLC_A,
        workshop_launcher_id("9101"),
        DLC_B,
        workshop_launcher_id("9102"),
        DLC_C,
        UNKNOWN_MOD,
    ]
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    payload["enabled_mods"] = list(original)
    payload["disabled_dlcs"] = ["fake_dlc"]
    (user_dir / "dlc_load.json").write_text(
        json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    managed = {workshop_launcher_id("9101"), workshop_launcher_id("9102")}
    report = sync_stellaris_launcher(db, user_dir=user_dir)
    assert report.written is True
    after_payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    after = after_payload["enabled_mods"]
    assert after == [
        DLC_A,
        workshop_launcher_id("9102"),
        workshop_launcher_id("9101"),
        DLC_B,
        DLC_C,
        UNKNOWN_MOD,
    ]
    _assert_non_smm_preserved(original, after, managed)
    assert after_payload["disabled_dlcs"] == ["fake_dlc"]


def test_undeploy_does_not_remove_dlc(tmp_path: Path, db: DatabaseManager) -> None:
    user_dir, _workshop = _configure_stellaris(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_stellaris_mod(library, db, folder="A", workshop_id="9201")
    b = _seed_stellaris_mod(library, db, folder="B", workshop_id="9202")
    _write_ugc_descriptor(user_dir, "9201")
    _write_ugc_descriptor(user_dir, "9202")
    persist_load_order([a, b], db)
    original = [DLC_A, workshop_launcher_id("9201"), DLC_B, workshop_launcher_id("9202"), DLC_C]
    payload = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))
    payload["enabled_mods"] = list(original)
    (user_dir / "dlc_load.json").write_text(
        json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    deployer = ModDeployer(library_root=library, db=db)
    assert deployer.deploy_mod(a).get("success") is True
    assert deployer.deploy_mod(b).get("success") is True
    after_deploy = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))[
        "enabled_mods"
    ]
    managed = {workshop_launcher_id("9201"), workshop_launcher_id("9202")}
    _assert_non_smm_preserved(original, after_deploy, managed)
    assert deployer.undeploy_mod(b).get("success") is True
    after_undeploy = json.loads((user_dir / "dlc_load.json").read_text(encoding="utf-8"))[
        "enabled_mods"
    ]
    assert workshop_launcher_id("9202") not in after_undeploy
    assert workshop_launcher_id("9201") in after_undeploy
    _assert_non_smm_preserved(original, after_undeploy, managed)
    assert after_undeploy == [DLC_A, workshop_launcher_id("9201"), DLC_B, DLC_C]


def test_simulated_sync_against_real_dlc_load_format_does_not_write_production() -> None:
    prod = default_stellaris_user_dir() / "dlc_load.json"
    if prod.is_file():
        real = json.loads(prod.read_text(encoding="utf-8"))
        assert isinstance(real, dict)
        real_enabled = list(real.get("enabled_mods") or [])
        before_text = prod.read_text(encoding="utf-8")
    else:
        real_enabled = ["mod/ugc_2598240743.mod"]
        before_text = None
    mixed = [DLC_A, *real_enabled, DLC_B, UNKNOWN_MOD]
    smm_owned = workshop_launcher_id("2598240743")
    managed = {smm_owned}
    after = merge_stellaris_enabled_mods(
        mixed,
        managed_ids=managed,
        enabled_ordered=[smm_owned],
    )
    _assert_non_smm_preserved(mixed, after, managed)
    assert DLC_A in after and DLC_B in after
    assert UNKNOWN_MOD in after
    if before_text is not None:
        assert prod.read_text(encoding="utf-8") == before_text
