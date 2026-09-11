"""WH3 activation, load order, and used_mods.txt (App ID 1142710)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from core.db_manager import DEPLOY_STATUS_DEPLOYED, DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM, WARHAMMER3_APP_IDS
from services.identity_service import create_mod_identity, identity_create_scope
from services.wh3_activation import (
    WH3_APP_ID,
    WH3_LAUNCH_ARG,
    apply_card_drop,
    collect_enabled_pack_lines,
    display_numbers,
    is_wh3_activation_app,
    launch_wh3,
    load_saved_order,
    move_in_order,
    persist_load_order,
    render_used_mods_text,
    resolve_pack_lines,
    resolved_load_order,
    set_wh3_enabled,
    sync_used_mods_txt,
)

WH3 = 1142710
assert WH3 == WH3_APP_ID
assert WH3 in WARHAMMER3_APP_IDS


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "wh3_activation.db")
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


def _configure_wh3(
    db: DatabaseManager,
    tmp_path: Path,
    *,
    workshop: Path | None = None,
) -> tuple[Path, Path, Path]:
    install = tmp_path / "WH3Install"
    data = tmp_path / "WH3Data"
    workshop_path = workshop if workshop is not None else tmp_path / "workshop" / "content" / str(WH3)
    install.mkdir(parents=True)
    data.mkdir(parents=True)
    workshop_path.mkdir(parents=True)
    (install / "Warhammer3.exe").write_bytes(b"MZ")
    db.upsert_game(
        GameInfo(
            app_id=WH3,
            name="Total War: WARHAMMER III",
            folder_name="Warhammer3",
        )
    )
    db.update_game_deploy_config(
        WH3,
        name="Total War: WARHAMMER III",
        install_path=str(install),
        mod_path=str(data),
        workshop_path=str(workshop_path),
    )
    return install, data, workshop_path


def _seed_wh3_mod(
    library: Path,
    db: DatabaseManager,
    *,
    folder: str,
    workshop_id: str,
    pack_name: str,
    pack_bytes: bytes = b"PACK",
    enabled: bool = True,
    deployed: bool = True,
) -> str:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=str(workshop_id),
            workshop_id=str(workshop_id),
            title=folder,
            app_id=WH3,
            game_name="Total War: WARHAMMER III",
            operation="import",
        )
    pk = str(created.mod_id)
    mod_dir = library / "Warhammer3" / folder
    mod_dir.mkdir(parents=True)
    (mod_dir / pack_name).write_bytes(pack_bytes)
    db.update_mod_identity_fields(
        pk,
        last_known_path=str(mod_dir),
        folder_present=True,
    )
    cfg = db.get_game_deploy_config(WH3)
    workshop_root = str(cfg.workshop_path or "").strip() if cfg is not None else ""
    if workshop_root:
        wdir = Path(workshop_root) / str(workshop_id)
        wdir.mkdir(parents=True, exist_ok=True)
        (wdir / pack_name).write_bytes(pack_bytes)
    if deployed:
        db.update_mod_deploy_status(
            pk,
            deploy_status=DEPLOY_STATUS_DEPLOYED,
            deploy_path=str(mod_dir),
            app_id=WH3,
        )
    if not enabled:
        db.disable_mod(pk)
    return pk


def test_wh3_app_id_enters_activation() -> None:
    assert is_wh3_activation_app(1142710) is True
    assert is_wh3_activation_app("1142710") is True
    assert is_wh3_activation_app(0, "Total War: WARHAMMER III") is True


def test_non_wh3_games_do_not_enter_activation() -> None:
    assert is_wh3_activation_app(289070) is False
    assert is_wh3_activation_app(1623730, "Palworld") is False
    assert is_wh3_activation_app(1086940, "Baldur's Gate 3") is False
    assert is_wh3_activation_app(292030, "The Witcher 3") is False
    assert is_wh3_activation_app(413150, "Stardew Valley") is False


def test_enable_disable_persist_and_used_mods(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    copies: list[str] = []

    def _fail_copy(*_a, **_k):
        copies.append("copy")
        raise AssertionError("WH3 activation must not copy packs")

    monkeypatch.setattr(shutil, "copy2", _fail_copy)
    monkeypatch.setattr(shutil, "copyfile", _fail_copy)
    monkeypatch.setattr(shutil, "copytree", _fail_copy)

    install, _data, workshop = _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_wh3_mod(library, db, folder="A", workshop_id="1001", pack_name="a.pack")
    b = _seed_wh3_mod(library, db, folder="B", workshop_id="1002", pack_name="b.pack")
    persist_load_order([a, b], db, library_root=library)

    assert set_wh3_enabled(a, True, db) is True
    assert db.is_mod_enabled(a) is True
    assert set_wh3_enabled(b, False, db) is True
    assert db.is_mod_enabled(b) is False

    path, wrote = sync_used_mods_txt(db, library_root=library, install_path=install)
    assert path is not None
    assert wrote is True
    text = path.read_text(encoding="utf-8")
    mod_lines = [line for line in text.splitlines() if line.startswith("mod ")]
    assert 'mod "a.pack";' in text
    assert "b.pack" not in text
    assert all(line.endswith('.pack";') for line in mod_lines)
    assert str(workshop / "1001") in text
    assert str(library / "Warhammer3" / "A") not in text
    assert f'mod "1001";' not in text
    assert f'mod "{a}";' not in text
    assert copies == []


def test_initial_load_order_and_display_numbers(
    tmp_path: Path, db: DatabaseManager
) -> None:
    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    ids = [
        _seed_wh3_mod(
            library, db, folder=name, workshop_id=wid, pack_name=f"{name}.pack"
        )
        for name, wid in (("A", "11"), ("B", "12"), ("C", "13"), ("D", "14"))
    ]
    persist_load_order(ids, db, library_root=library)
    order = resolved_load_order(db, library_root=library)
    assert order == ids
    numbers = display_numbers(order)
    assert numbers[ids[0]] == 1
    assert numbers[ids[1]] == 2
    assert sorted(numbers.values()) == [1, 2, 3, 4]


def test_drag_third_onto_second_renumbers(tmp_path: Path, db: DatabaseManager) -> None:
    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    a, b, c, d, e = [
        _seed_wh3_mod(
            library, db, folder=name, workshop_id=wid, pack_name=f"{name}.pack"
        )
        for name, wid in (
            ("A", "21"),
            ("B", "22"),
            ("C", "23"),
            ("D", "24"),
            ("E", "25"),
        )
    ]
    persist_load_order([a, b, c, d, e], db, library_root=library)
    next_order = apply_card_drop(c, b, db, library_root=library)
    assert next_order == [a, c, b, d, e]
    numbers = display_numbers(next_order)
    assert list(numbers.values()) == [1, 2, 3, 4, 5]
    assert numbers[c] == 2
    assert numbers[b] == 3


def test_drag_last_to_first_renumbers(tmp_path: Path, db: DatabaseManager) -> None:
    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    a, b, c, d, e = [
        _seed_wh3_mod(
            library, db, folder=name, workshop_id=wid, pack_name=f"{name}.pack"
        )
        for name, wid in (
            ("A", "31"),
            ("B", "32"),
            ("C", "33"),
            ("D", "34"),
            ("E", "35"),
        )
    ]
    persist_load_order([a, b, c, d, e], db, library_root=library)
    next_order = move_in_order([a, b, c, d, e], e, a)
    assert next_order == [e, a, b, c, d]
    assert display_numbers(next_order)[e] == 1
    assert list(display_numbers(next_order).values()) == [1, 2, 3, 4, 5]


def test_no_duplicate_numbers_and_offscreen_kept() -> None:
    order = ["1", "2", "3", "4", "5", "6", "7"]
    moved = move_in_order(order, "7", "1")
    nums = list(display_numbers(moved).values())
    assert nums == list(range(1, 8))
    assert len(set(nums)) == 7
    assert set(moved) == set(order)


def test_load_order_survives_reload(tmp_path: Path, db: DatabaseManager) -> None:
    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    ids = [
        _seed_wh3_mod(
            library, db, folder=name, workshop_id=wid, pack_name=f"{name}.pack"
        )
        for name, wid in (("A", "41"), ("B", "42"), ("C", "43"))
    ]
    persist_load_order([ids[2], ids[0], ids[1]], db, library_root=library)
    assert load_saved_order() == [ids[2], ids[0], ids[1]]
    assert resolved_load_order(db, library_root=library) == [ids[2], ids[0], ids[1]]


def test_used_mods_enabled_order_no_duplicates(
    tmp_path: Path, db: DatabaseManager
) -> None:
    install, _data, workshop = _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_wh3_mod(library, db, folder="A", workshop_id="51", pack_name="a.pack")
    _seed_wh3_mod(
        library, db, folder="B", workshop_id="52", pack_name="b.pack", enabled=False
    )
    c = _seed_wh3_mod(library, db, folder="C", workshop_id="53", pack_name="c.pack")
    persist_load_order([c, a, "unused"], db, library_root=library)
    persist_load_order([c, a], db, library_root=library)
    lines = collect_enabled_pack_lines(db, library_root=library)
    text = render_used_mods_text(lines)
    assert text.splitlines()[0].startswith("add_working_directory ")
    names = [line for line in text.splitlines() if line.startswith("mod ")]
    assert names == ['mod "c.pack";', 'mod "a.pack";']
    assert "b.pack" not in text
    assert all(line.endswith('.pack";') for line in names)
    assert str(workshop / "53") in text
    assert str(library / "Warhammer3" / "C") not in text
    assert f'mod "51";' not in text
    assert f'mod "{a}";' not in text
    _ = install


def test_used_mods_uses_workshop_not_library(tmp_path: Path, db: DatabaseManager) -> None:
    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    mid = _seed_wh3_mod(
        library, db, folder="A", workshop_id="61", pack_name="old.pack"
    )
    workshop = tmp_path / "workshop" / "content" / str(WH3)
    wdir = workshop / "61"
    persist_load_order([mid], db, library_root=library)
    first = render_used_mods_text(collect_enabled_pack_lines(db, library_root=library))
    assert "old.pack" in first
    assert str(wdir) in first
    assert str(library / "Warhammer3" / "A") not in first
    (wdir / "old.pack").unlink()
    (wdir / "new.pack").write_bytes(b"NEW")
    second = render_used_mods_text(collect_enabled_pack_lines(db, library_root=library))
    assert "new.pack" in second
    assert "old.pack" not in second
    assert str(wdir) in second
    assert f'mod "61";' not in second
    assert f'mod "{mid}";' not in second


def test_launch_wh3_writes_used_mods(tmp_path: Path, db: DatabaseManager) -> None:
    install, _data, workshop = _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_wh3_mod(library, db, folder="A", workshop_id="71", pack_name="a.pack")
    persist_load_order([a], db, library_root=library)
    captured: dict = {}

    def _spawn(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs

    ok, message = launch_wh3(db, library_root=library, spawn=_spawn)
    assert ok is True
    assert message == ""
    used = install / "used_mods.txt"
    assert used.is_file()
    text = used.read_text(encoding="utf-8")
    assert 'mod "a.pack";' in text
    assert str(workshop / "71") in text
    assert str(library / "Warhammer3" / "A") not in text
    assert captured["cmd"][0].endswith("Warhammer3.exe")
    assert captured["cmd"][1] == WH3_LAUNCH_ARG
    assert Path(captured["kwargs"]["cwd"]) == install


def test_sync_skips_rewrite_when_unchanged(tmp_path: Path, db: DatabaseManager) -> None:
    install, _data, workshop = _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_wh3_mod(library, db, folder="A", workshop_id="81", pack_name="a.pack")
    persist_load_order([a], db, library_root=library)
    _, first = sync_used_mods_txt(db, library_root=library, install_path=install)
    _, second = sync_used_mods_txt(db, library_root=library, install_path=install)
    assert first is True
    assert second is False
    _ = workshop


def test_multiple_packs_one_working_directory(tmp_path: Path, db: DatabaseManager) -> None:
    from services.wh3_activation import Wh3ModRef

    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    mid = _seed_wh3_mod(
        library, db, folder="MultiPack", workshop_id="91", pack_name="a.pack"
    )
    mod_dir = library / "Warhammer3" / "MultiPack"
    (mod_dir / "b.pack").write_bytes(b"B")
    (mod_dir / "notes.txt").write_text("no", encoding="utf-8")
    cfg = db.get_game_deploy_config(WH3)
    wdir = Path(str(cfg.workshop_path)) / "91"
    (wdir / "b.pack").write_bytes(b"B")
    persist_load_order([mid], db, library_root=library)
    text = render_used_mods_text(collect_enabled_pack_lines(db, library_root=library))
    assert text.count("add_working_directory") == 1
    assert str(wdir) in text
    assert str(mod_dir) not in text
    assert 'mod "a.pack";' in text
    assert 'mod "b.pack";' in text
    assert "notes.txt" not in text
    assert f'mod "91";' not in text
    assert f'mod "{mid}";' not in text
    ref = Wh3ModRef(
        internal_id=mid,
        workspace_id="91",
        enabled=True,
        last_known_path=str(mod_dir),
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        title="MultiPack",
    )
    lines = resolve_pack_lines(ref, workshop_path=str(tmp_path / "workshop" / "content" / str(WH3)))
    assert {line.pack_name for line in lines} == {"a.pack", "b.pack"}
    assert all(line.directory == str(wdir) for line in lines)


def test_used_mods_skips_chinese_library_path(tmp_path: Path, db: DatabaseManager) -> None:
    install, _data, workshop = _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    mid = _seed_wh3_mod(
        library,
        db,
        folder="中文模组名称",
        workshop_id="2811310125",
        pack_name="skipintro_3.pack",
    )
    persist_load_order([mid], db, library_root=library)
    text = render_used_mods_text(collect_enabled_pack_lines(db, library_root=library))
    chinese_dir = library / "Warhammer3" / "中文模组名称"
    assert str(chinese_dir) not in text
    assert str(workshop / "2811310125") in text
    assert 'mod "skipintro_3.pack";' in text
    assert f'mod "{mid}";' not in text
    _ = install


def test_archive_extracts_to_english_unzip(
    tmp_path: Path, db: DatabaseManager
) -> None:
    import zipfile

    from services.wh3_activation import prepare_wh3_workshop_packs, Wh3ModRef

    install, data, workshop = _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    chinese = library / "Warhammer3" / "更好的镜头"
    chinese.mkdir(parents=True)
    zip_path = chinese / "BetterCamera.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("folder/cam.pack", b"PACK")
        zf.writestr("readme.txt", b"docs")
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id="99001",
            workshop_id="99001",
            title="Better Camera",
            app_id=WH3,
            game_name="Total War: WARHAMMER III",
            operation="import",
        )
    pk = str(created.mod_id)
    db.update_mod_identity_fields(pk, last_known_path=str(chinese), folder_present=True)
    db.update_mod_deploy_status(
        pk, deploy_status=DEPLOY_STATUS_DEPLOYED, deploy_path=str(chinese), app_id=WH3
    )
    ref = Wh3ModRef(
        internal_id=pk,
        workspace_id="99001",
        enabled=True,
        last_known_path=str(chinese),
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        title="Better Camera",
    )
    lines, err = prepare_wh3_workshop_packs(
        ref, workshop_path=str(workshop), extract=True
    )
    assert err == ""
    unzip = workshop / "Better Camera_unzip"
    assert unzip.is_dir()
    assert any(p.name.lower() == "cam.pack" for p in unzip.rglob("*.pack"))
    text = render_used_mods_text(lines)
    assert str(unzip) in text or str(unzip / "folder") in text
    assert str(chinese) not in text
    assert 'mod "cam.pack";' in text
    assert not any(line.startswith("mod ") and "\\" in line for line in text.splitlines())
    assert _data_pack_names_missing(data)
    _ = install


def _data_empty(data: Path) -> bool:
    return not any(
        p.is_file() and p.suffix.lower() == ".pack" for p in data.iterdir()
    ) if data.exists() else True


def _data_pack_names_missing(data: Path) -> bool:
    return _data_empty(data)


def test_archive_without_english_name_uses_zip_stem(
    tmp_path: Path, db: DatabaseManager
) -> None:
    import zipfile

    from services.wh3_activation import prepare_wh3_workshop_packs, Wh3ModRef

    _install, _data, workshop = _configure_wh3(db, tmp_path)
    library = tmp_path / "mod" / "Warhammer3" / "中文"
    library.mkdir(parents=True)
    zip_path = library / "123456.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("only.pack", b"P")
    ref = Wh3ModRef(
        internal_id="12",
        workspace_id="99002",
        enabled=True,
        last_known_path=str(library),
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        title="中文模组",
    )
    lines, err = prepare_wh3_workshop_packs(
        ref, workshop_path=str(workshop), extract=True
    )
    assert err == ""
    unzip = workshop / "123456_unzip"
    assert unzip.is_dir()
    text = render_used_mods_text(lines)
    assert str(unzip) in text
    assert 'mod "only.pack";' in text
    assert str(library) not in text


def test_workshop_root_missing_is_explicit_error(
    tmp_path: Path, db: DatabaseManager
) -> None:
    from services.wh3_activation import (
        WH3_WORKSHOP_MISSING,
        prepare_wh3_workshop_packs,
        Wh3ModRef,
    )

    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    _seed_wh3_mod(library, db, folder="A", workshop_id="77", pack_name="a.pack")
    ref = Wh3ModRef(
        internal_id="1",
        workspace_id="77",
        enabled=True,
        last_known_path=str(library / "Warhammer3" / "A"),
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        title="A",
    )
    lines, err = prepare_wh3_workshop_packs(
        ref, workshop_path=str(tmp_path / "missing_workshop"), extract=True
    )
    assert lines == []
    assert err == WH3_WORKSHOP_MISSING
    used = render_used_mods_text(
        resolve_pack_lines(ref, workshop_path=str(tmp_path / "missing_workshop"))
    )
    assert str(library / "Warhammer3" / "A") not in used


def test_archive_without_pack_fails_prepare(tmp_path: Path) -> None:
    import zipfile

    from services.wh3_activation import WH3_MISSING_PACK, prepare_wh3_workshop_packs, Wh3ModRef

    workshop = tmp_path / "workshop" / "content" / str(WH3)
    workshop.mkdir(parents=True)
    library = tmp_path / "mod" / "ZipEmpty"
    library.mkdir(parents=True)
    with zipfile.ZipFile(library / "docs.zip", "w") as zf:
        zf.writestr("readme.txt", b"hi")
    ref = Wh3ModRef(
        internal_id="8",
        workspace_id="11408",
        enabled=True,
        last_known_path=str(library),
        deploy_status="",
        title="Docs Only",
    )
    lines, err = prepare_wh3_workshop_packs(
        ref, workshop_path=str(workshop), extract=True
    )
    assert lines == []
    assert err == WH3_MISSING_PACK


def test_enable_sort_do_not_reextract(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services import deploy_apply
    from services.wh3_activation import Wh3ModRef, prepare_wh3_workshop_packs

    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_wh3_mod(library, db, folder="A", workshop_id="41", pack_name="a.pack")
    extracted: list[str] = []

    def _no_extract(archive, dest):
        extracted.append(str(archive))
        raise AssertionError("enable/sort must not extract")

    monkeypatch.setattr(deploy_apply, "extract_archive_via_core", _no_extract)
    persist_load_order([a], db, library_root=library)
    apply_card_drop(a, a, db, library_root=library)
    sync_used_mods_txt(db, library_root=library)
    set_wh3_enabled(a, False, db)
    set_wh3_enabled(a, True, db)
    sync_used_mods_txt(db, library_root=library)
    assert extracted == []
    ref = Wh3ModRef(
        internal_id=a,
        workspace_id="41",
        enabled=True,
        last_known_path=str(library / "Warhammer3" / "A"),
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        title="A",
    )
    lines, err = prepare_wh3_workshop_packs(
        ref,
        workshop_path=str(tmp_path / "workshop" / "content" / str(WH3)),
        extract=False,
    )
    assert err == ""
    assert lines


def test_data_folder_packs_skip_working_directory(tmp_path: Path) -> None:
    from services.wh3_activation import Wh3PackLine

    text = render_used_mods_text(
        [
            Wh3PackLine(
                pack_name="data_mod.pack",
                directory=str(tmp_path / "data"),
                in_data=True,
                internal_id="9",
            )
        ]
    )
    assert text == 'mod "data_mod.pack";'
    assert "add_working_directory" not in text


@pytest.mark.parametrize("app_id", [289070, 1623730, 1086940, 413150, 292030])
def test_used_mods_not_used_for_other_games(app_id: int) -> None:
    assert is_wh3_activation_app(app_id) is False


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _card_data(folder: Path, *, deps: int = 0, conflicts: int = 0):
    from core.db_manager import DEPLOY_STATUS_DEPLOYED
    from services.mod_library_cache import ModCardData

    return ModCardData(
        id="12",
        title="A",
        platform=PLATFORM_STEAM,
        cover="",
        description="",
        tags="",
        size=0,
        updated_time=0.0,
        managed_path=str(folder),
        game_folder="Warhammer3",
        deployed=True,
        deploy_status=DEPLOY_STATUS_DEPLOYED,
        relation_deps=deps,
        relation_conflicts=conflicts,
    )


def _rects_overlap(a, b) -> bool:
    return a.geometry().intersects(b.geometry())


def test_normal_mode_keeps_relation_hides_load_order(qapp, tmp_path: Path) -> None:
    from ui.mod_card import COVER_HEIGHT, ModCardWidget

    folder = tmp_path / "mod"
    folder.mkdir()
    card = ModCardWidget(folder, card_data=_card_data(folder, deps=3, conflicts=1))
    card.refresh_display()
    assert card.load_order_badge.isHidden()
    assert not card.relation_badge.isHidden()
    assert "↑ 3" in card.relation_badge.text()
    assert card.relation_badge.x() <= 4
    assert card.relation_badge.y() >= COVER_HEIGHT - card.relation_badge.height() - 8
    card.deleteLater()


def test_sort_mode_centers_load_order_keeps_relation_and_deploy(
    qapp, tmp_path: Path
) -> None:
    from ui.mod_card import COVER_HEIGHT, COVER_WIDTH, ModCardWidget

    folder = tmp_path / "mod"
    folder.mkdir()
    card = ModCardWidget(folder, card_data=_card_data(folder, deps=2, conflicts=1))
    card.set_wh3_sort_mode(True, number=1)
    assert not card.load_order_badge.isHidden()
    assert card.load_order_badge.text() == "1"
    assert not card.relation_badge.isHidden()
    assert "↑ 2" in card.relation_badge.text()
    assert not card.deploy_dot.isHidden()

    cover_cx = COVER_WIDTH // 2
    cover_cy = COVER_HEIGHT // 2
    order_cx = card.load_order_badge.x() + card.load_order_badge.width() // 2
    order_cy = card.load_order_badge.y() + card.load_order_badge.height() // 2
    assert abs(order_cx - cover_cx) <= 8
    assert abs(order_cy - cover_cy) <= 8
    assert card.relation_badge.x() <= 4
    assert card.deploy_dot.x() > card.load_order_badge.x() + card.load_order_badge.width() // 2
    assert not _rects_overlap(card.load_order_badge, card.relation_badge)
    assert not _rects_overlap(card.load_order_badge, card.deploy_dot)
    assert not _rects_overlap(card.relation_badge, card.deploy_dot)
    font = card.load_order_badge.font()
    assert font.bold() is True
    assert font.pixelSize() >= 18
    card.deleteLater()


def test_load_order_digits_remain_readable(qapp, tmp_path: Path) -> None:
    from ui.mod_card import COVER_WIDTH, ModCardWidget

    folder = tmp_path / "mod"
    folder.mkdir()
    card = ModCardWidget(folder, card_data=_card_data(folder, deps=1))
    for number in (1, 10, 100):
        card.set_wh3_sort_mode(True, number=number)
        assert card.load_order_badge.text() == str(number)
        assert card.load_order_badge.width() >= 28
        assert card.load_order_badge.height() >= 24
        assert card.load_order_badge.x() >= 0
        assert card.load_order_badge.x() + card.load_order_badge.width() <= COVER_WIDTH
        assert not card.relation_badge.isHidden()
        assert not _rects_overlap(card.load_order_badge, card.relation_badge)
    card.deleteLater()


def _visible_button_texts(view) -> list[str]:
    from PySide6.QtWidgets import QAbstractButton

    return [str(btn.text() or "") for btn in view.findChildren(QAbstractButton)]


def test_sort_mode_library_order_and_viewport(
    qapp, tmp_path: Path, db: DatabaseManager
) -> None:
    from PySide6.QtWidgets import QLabel, QWidget
    from ui.library_query import FILTER_ALL, FILTER_FAVORITE, STATUS_FILTER_LABELS
    from ui.library_view import LIBRARY_ACTION_BTN_H, LIBRARY_ACTION_BTN_W, ModLibraryView

    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    ids = [
        _seed_wh3_mod(
            library,
            db,
            folder=f"Mod{i:02d}",
            workshop_id=str(9000 + i),
            pack_name=f"m{i:02d}.pack",
        )
        for i in range(8)
    ]
    undeployed = _seed_wh3_mod(
        library,
        db,
        folder="Undeployed",
        workshop_id="9099",
        pack_name="skip.pack",
        deployed=False,
    )
    persist_load_order(list(reversed(ids)), db, library_root=library)

    view = ModLibraryView()
    view.set_target_root(str(library))
    view.set_preferred_filter("Warhammer3")
    view.refresh()
    assert view._is_wh3_current_game() is True
    assert not view.btn_wh3_sort_mode.isHidden()
    assert view.btn_wh3_sort_mode.parent() is view._record_actions
    assert view.btn_deployment_record.parent() is view._record_actions
    assert view.btn_wh3_sort_mode.parent() is not view._status_chips
    col = view._record_actions.layout()
    assert col.indexOf(view.btn_deployment_record) == 0
    assert col.indexOf(view.btn_wh3_sort_mode) == 1
    toolbar_layout = view._library_toolbar.layout()
    assert toolbar_layout.indexOf(view._filter_column) == 0
    assert toolbar_layout.indexOf(view._record_actions) == 1
    left_layout = view._filter_column.layout()
    assert left_layout.indexOf(view._status_chips) == 0
    assert left_layout.indexOf(view._meta_bar) == 1
    assert view._record_actions.parent() is view._library_toolbar
    assert view._status_chips.parent() is view._filter_column
    assert view._meta_bar.parent() is view._filter_column
    assert {k for k, _label in STATUS_FILTER_LABELS} <= set(view._filter_buttons)
    assert view.btn_wh3_sort_mode not in view._filter_buttons.values()
    assert "排序模式" not in [btn.text() for btn in view._filter_buttons.values()]
    chip_texts = [btn.text() for btn in view._filter_buttons.values()]
    assert "启动游戏" not in chip_texts
    assert not hasattr(view, "btn_wh3_launch")
    assert not hasattr(view, "_wh3_bar")
    button_texts = _visible_button_texts(view)
    assert all("启动游戏" not in text for text in button_texts)
    assert all("Launch Game" not in text for text in button_texts)
    assert not hasattr(view, "_source_row")
    assert not hasattr(view, "_platform_bar")
    assert not hasattr(view, "_platform_buttons")
    center = view.findChild(QWidget, "libraryCenter")
    assert center is not None
    assert "来源" not in [
        str(w.text() or "").strip() for w in center.findChildren(QLabel)
    ]
    assert view.btn_deployment_record.width() == view.btn_wh3_sort_mode.width()
    assert view.btn_deployment_record.height() == view.btn_wh3_sort_mode.height()
    assert view.btn_deployment_record.width() >= LIBRARY_ACTION_BTN_W
    assert view.btn_deployment_record.height() >= LIBRARY_ACTION_BTN_H

    view.search_box.setText("Mod00")
    view._set_library_status_filter(FILTER_FAVORITE)
    assert view._status_filter == FILTER_FAVORITE
    view.btn_wh3_sort_mode.setChecked(True)
    assert view._wh3_sort_mode is True
    assert view._status_filter == FILTER_FAVORITE
    assert view.search_box.text() == "Mod00"
    filtered_ids = [
        str(index.mod_id) for index, _payload in view._filtered_row_entries
    ]
    assert undeployed not in filtered_ids
    assert filtered_ids == list(reversed(ids))
    numbers = [int(card.load_order_badge.text()) for card in view._cards]
    assert numbers == list(range(1, len(numbers) + 1))
    assert view._cards[0].load_order_badge.text() == "1"
    assert view._cards[1].load_order_badge.text() == "2"

    view._on_wh3_sort_drop(ids[0], ids[-1])
    filtered_ids = [
        str(index.mod_id) for index, _payload in view._filtered_row_entries
    ]
    assert filtered_ids[0] == ids[0]
    assert set(filtered_ids) == set(ids)
    shown = [int(card.load_order_badge.text()) for card in view._cards]
    assert shown == list(range(1, len(shown) + 1))
    assert len(set(shown)) == len(shown)

    view._last_filter_sig = None
    view._apply_view_filter()
    assert filtered_ids == [
        str(index.mod_id) for index, _payload in view._filtered_row_entries
    ]
    assert view._status_filter == FILTER_FAVORITE
    assert view.search_box.text() == "Mod00"

    view.btn_wh3_sort_mode.setChecked(False)
    assert view._wh3_sort_mode is False
    assert view._status_filter == FILTER_FAVORITE
    assert view.search_box.text() == "Mod00"
    restored_ids = [
        str(index.mod_id) for index, _payload in view._filtered_row_entries
    ]
    assert restored_ids != filtered_ids

    view.btn_wh3_sort_mode.setChecked(True)
    view._filter_buttons[FILTER_ALL].click()
    assert view._wh3_sort_mode is False
    assert view._status_filter == FILTER_ALL
    view.deleteLater()


def test_new_mod_appended_missing_dropped(tmp_path: Path, db: DatabaseManager) -> None:
    from core.db_manager import DEPLOY_STATUS_NOT_DEPLOYED

    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_wh3_mod(library, db, folder="A", workshop_id="31", pack_name="a.pack")
    b = _seed_wh3_mod(library, db, folder="B", workshop_id="32", pack_name="b.pack")
    persist_load_order([a, b], db, library_root=library)
    c = _seed_wh3_mod(library, db, folder="C", workshop_id="33", pack_name="c.pack")
    order = persist_load_order(load_saved_order(), db, library_root=library)
    assert order == [a, b, c]
    db.update_mod_deploy_status(b, deploy_status=DEPLOY_STATUS_NOT_DEPLOYED, app_id=WH3)
    order = persist_load_order(load_saved_order(), db, library_root=library)
    assert order == [a, c]
    assert b not in order


def test_reorder_does_not_copy_or_hash_packs(
    tmp_path: Path, db: DatabaseManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    copies: list[str] = []

    def _fail_copy(*_a, **_k):
        copies.append("copy")
        raise AssertionError("WH3 sort must not copy packs")

    monkeypatch.setattr(shutil, "copy2", _fail_copy)
    monkeypatch.setattr(shutil, "copyfile", _fail_copy)
    monkeypatch.setattr(shutil, "copytree", _fail_copy)

    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_wh3_mod(library, db, folder="A", workshop_id="41", pack_name="a.pack")
    b = _seed_wh3_mod(library, db, folder="B", workshop_id="42", pack_name="b.pack")
    persist_load_order([a, b], db, library_root=library)
    apply_card_drop(b, a, db, library_root=library)
    sync_used_mods_txt(db, library_root=library)
    assert copies == []
    assert load_saved_order() == [b, a]


def test_load_order_writes_wh3_dir_not_data_root(
    tmp_path: Path, db: DatabaseManager
) -> None:
    from core.paths import data_dir
    from services.wh3_activation import (
        WH3_LEGACY_ORDER_FILENAME,
        WH3_ORDER_FILENAME,
        WH3_STATE_DIRNAME,
    )

    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_wh3_mod(library, db, folder="A", workshop_id="71", pack_name="a.pack")
    persist_load_order([a], db, library_root=library)
    new_path = data_dir() / WH3_STATE_DIRNAME / WH3_ORDER_FILENAME
    legacy = data_dir() / WH3_LEGACY_ORDER_FILENAME
    assert new_path.is_file()
    assert not legacy.exists()
    assert load_saved_order() == [a]


def test_legacy_wh3_load_order_json_migrates_once(
    tmp_path: Path, db: DatabaseManager
) -> None:
    import json

    from core.paths import data_dir
    from services.wh3_activation import (
        WH3_LEGACY_ORDER_FILENAME,
        WH3_ORDER_FILENAME,
        WH3_STATE_DIRNAME,
    )

    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_wh3_mod(library, db, folder="A", workshop_id="81", pack_name="a.pack")
    b = _seed_wh3_mod(library, db, folder="B", workshop_id="82", pack_name="b.pack")
    legacy = data_dir() / WH3_LEGACY_ORDER_FILENAME
    new_path = data_dir() / WH3_STATE_DIRNAME / WH3_ORDER_FILENAME
    legacy.write_text(
        json.dumps({"order": [b, a]}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert load_saved_order() == [b, a]
    assert new_path.is_file()
    assert not legacy.exists()
    assert load_saved_order() == [b, a]
    assert resolved_load_order(db, library_root=library) == [b, a]


def test_new_wh3_order_file_wins_over_legacy(
    tmp_path: Path, db: DatabaseManager
) -> None:
    import json

    from core.paths import data_dir
    from services.wh3_activation import (
        WH3_LEGACY_ORDER_FILENAME,
        WH3_ORDER_FILENAME,
        WH3_STATE_DIRNAME,
        save_saved_order,
    )

    _configure_wh3(db, tmp_path)
    library = tmp_path / "mod"
    a = _seed_wh3_mod(library, db, folder="A", workshop_id="91", pack_name="a.pack")
    b = _seed_wh3_mod(library, db, folder="B", workshop_id="92", pack_name="b.pack")
    save_saved_order([a, b])
    legacy = data_dir() / WH3_LEGACY_ORDER_FILENAME
    new_path = data_dir() / WH3_STATE_DIRNAME / WH3_ORDER_FILENAME
    legacy.write_text(
        json.dumps({"order": [b, a]}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert new_path.is_file()
    assert load_saved_order() == [a, b]
    assert not legacy.exists()


def test_wh3_sort_mode_hidden_for_other_games(
    qapp, tmp_path: Path, db: DatabaseManager
) -> None:
    from core.game_info import GameInfo
    from ui.library_view import ModLibraryView

    db.upsert_game(GameInfo(app_id=1623730, name="Palworld", folder_name="Palworld"))
    view = ModLibraryView()
    view._set_current_game_context("Palworld", game_id=1623730)
    assert view.btn_wh3_sort_mode.isHidden()
    assert not hasattr(view, "btn_wh3_launch")
    view.deleteLater()


def test_filter_row_does_not_keep_phantom_height(qapp) -> None:
    """Filter/Mode columns are independent; Mode stack must not move 分类."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QWidget

    from ui.library_view import LIBRARY_ACTION_BTN_H, ModLibraryView
    from ui.styles import APP_STYLE

    qapp.setStyleSheet(APP_STYLE)
    view = ModLibraryView()
    view.resize(1280, 800)
    view.show()
    QCoreApplication.processEvents()
    view._apply_filter_row_height()
    QCoreApplication.processEvents()

    chips_h = view._status_chips.height()
    chips_y = view._status_chips.y()
    meta_y = view._meta_bar.y()
    compact_mode = view._record_actions.height()
    # Compact column: 部署记录 + 合集模式 (Sorting Mode hidden).
    assert view.btn_wh3_sort_mode.isHidden()
    assert compact_mode <= LIBRARY_ACTION_BTN_H * 2 + 16
    gap = view._meta_bar.y() - (view._status_chips.y() + view._status_chips.height())
    assert 0 <= gap <= 12
    assert view._status_chips.maximumHeight() <= chips_h + 2

    view.btn_wh3_sort_mode.setVisible(True)
    view._record_actions.adjustSize()
    view._record_actions.updateGeometry()
    QCoreApplication.processEvents()
    stacked_mode = view._record_actions.height()
    assert stacked_mode > compact_mode
    assert stacked_mode <= LIBRARY_ACTION_BTN_H * 3 + 24
    assert view._status_chips.height() == chips_h
    assert view._status_chips.y() == chips_y
    assert view._meta_bar.y() == meta_y

    view.btn_wh3_sort_mode.setVisible(False)
    view._record_actions.adjustSize()
    view._record_actions.updateGeometry()
    QCoreApplication.processEvents()
    restored_mode = view._record_actions.height()
    assert restored_mode <= compact_mode + 2
    assert restored_mode < stacked_mode
    assert view._status_chips.height() == chips_h
    assert view._meta_bar.y() == meta_y
    gap = view._meta_bar.y() - (view._status_chips.y() + view._status_chips.height())
    assert 0 <= gap <= 12

    # FlowLayout can request ~200px at a narrow width; chips must not follow.
    chips_flow = view._status_chips.layout()
    tall_hfw = chips_flow.heightForWidth(74)
    assert tall_hfw > chips_h + 40
    assert view._status_chips.height() < tall_hfw
    assert view._status_chips.maximumHeight() < tall_hfw

    center = view.findChild(QWidget, "libraryCenter")
    lay = center.layout()
    names = []
    for i in range(lay.count()):
        w = lay.itemAt(i).widget()
        if w is not None:
            names.append(w.objectName())
    assert "libraryToolbar" in names
    assert names.count("libraryFilterBar") == 0
    assert "来源" not in names
    view.deleteLater()


