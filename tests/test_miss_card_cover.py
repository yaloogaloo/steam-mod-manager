"""MISS Library cover projection — Card uses Backup, same contract as Detail."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from core.db_manager import DatabaseManager
from core.game_info import GameInfo
from core.mod_platform import PLATFORM_STEAM
from services.cover_projection import projection_cover_ref
from services.identity_service import create_mod_identity, identity_create_scope
from services.metadata_backup import backup_root
from services.mod_library_cache import (
    list_item_to_card_data,
    mod_list_item_from_row,
)
from services.mod_metadata_resolver import resolve_cover_path
from tests.helpers.identity import bind_managed_path, write_info_sidecar

APP_ID = 2379780
GAME = "小丑牌"


@pytest.fixture()
def db(tmp_path: Path) -> DatabaseManager:
    DatabaseManager.reset_instance()
    manager = DatabaseManager.instance(tmp_path / "miss_cover.db")
    manager.upsert_game(GameInfo(app_id=APP_ID, name=GAME, folder_name=GAME))
    yield manager
    manager.close()
    DatabaseManager.reset_instance()


@pytest.fixture(autouse=True)
def _patch_get_db(db: DatabaseManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.db_manager.get_db", lambda: db)


def _seed(
    db: DatabaseManager,
    library: Path,
    *,
    title: str,
    workshop: str,
    with_folder: bool,
    with_backup_cover: bool,
) -> tuple[Path, str]:
    with identity_create_scope():
        created = create_mod_identity(
            db,
            platform=PLATFORM_STEAM,
            external_id=workshop,
            workshop_id=workshop,
            title=title,
            app_id=APP_ID,
            game_name=GAME,
            operation="import",
        )
    pk = str(created.mod_id)
    frozen = str(created.internal_id or "")
    folder = library / GAME / title
    if with_folder:
        folder.mkdir(parents=True)
        (folder / "payload.txt").write_text("x", encoding="utf-8")
        info = folder / ".info"
        info.mkdir(exist_ok=True)
        (info / "cover.png").write_bytes(b"\x89PNGlocalcover")
        write_info_sidecar(
            folder,
            internal_id=frozen,
            title=title,
            external_id=workshop,
            workspace_id=workshop,
            app_id=APP_ID,
            game_name=GAME,
        )
        bind_managed_path(db, pk, folder)
        db.update_mod_cover_path(pk, ".info/cover.png")
        db.set_mod_folder_present(pk, present=True)
    else:
        db.update_mod_identity_fields(
            pk,
            last_known_path=str(folder),
            folder_present=False,
        )
        db.update_mod_cover_path(pk, ".info/cover.png")
        db.set_mod_folder_present(pk, present=False)
    if with_backup_cover:
        dest = backup_root(pk)
        dest.mkdir(parents=True, exist_ok=True)
        cover = dest / "cover.png"
        cover.write_bytes(b"\x89PNGbackupcover")
        (dest / "metadata.json").write_text(
            '{"title":"%s","source_type":"steam","workspace_id":"%s",'
            '"source_url":"https://example.test/%s","internal_id":"%s"}'
            % (title, workshop, workshop, frozen),
            encoding="utf-8",
        )
        db.update_mod_backup_snapshot(
            pk,
            last_known_path=str(folder),
            folder_present=with_folder,
            backup_metadata_json="{}",
            backup_cover_path=str(cover),
        )
        # snapshot may restore folder_present — force MISS again when needed
        if not with_folder:
            db.set_mod_folder_present(pk, present=False)
    return folder, pk


def test_projection_cover_ref_live_prefers_local() -> None:
    assert (
        projection_cover_ref(
            folder_present=True,
            cover_path=".info/cover.png",
            backup_cover_path=r"D:\backup\cover.png",
        )
        == ".info/cover.png"
    )


def test_projection_cover_ref_miss_uses_backup_only() -> None:
    assert (
        projection_cover_ref(
            folder_present=False,
            cover_path=".info/cover.png",
            backup_cover_path=r"D:\backup\cover.png",
        )
        == r"D:\backup\cover.png"
    )
    assert (
        projection_cover_ref(
            folder_present=False,
            cover_path=".info/cover.png",
            backup_cover_path="",
        )
        == ""
    )


def test_live_card_uses_local_cover(db: DatabaseManager, tmp_path: Path) -> None:
    _seed(
        db,
        tmp_path / "mod",
        title="Live",
        workshop="1001",
        with_folder=True,
        with_backup_cover=True,
    )
    rows = db.list_mod_list_items(game_id=APP_ID)
    live = [r for r in rows if not r.get("folder_absent")]
    assert live
    card = list_item_to_card_data(mod_list_item_from_row(live[0]))
    assert card.folder_absent is False
    assert card.cover == ".info/cover.png"


def test_miss_card_uses_backup_cover(db: DatabaseManager, tmp_path: Path) -> None:
    _folder, pk = _seed(
        db,
        tmp_path / "mod",
        title="Miss",
        workshop="1002",
        with_folder=False,
        with_backup_cover=True,
    )
    rows = db.list_mod_list_items(mod_id=int(pk))
    card = list_item_to_card_data(mod_list_item_from_row(rows[0]))
    assert card.folder_absent is True
    assert "mod_backup" in card.cover.replace("\\", "/")
    assert not card.cover.startswith(".info/")
    assert Path(card.cover).is_file()


def test_miss_card_does_not_depend_on_local_folder(
    db: DatabaseManager, tmp_path: Path
) -> None:
    folder, pk = _seed(
        db,
        tmp_path / "mod",
        title="MissNoDir",
        workshop="1003",
        with_folder=False,
        with_backup_cover=True,
    )
    assert not folder.is_dir()
    rows = db.list_mod_list_items(mod_id=int(pk))
    card = list_item_to_card_data(mod_list_item_from_row(rows[0]))
    assert Path(card.cover).is_file()


def test_miss_card_and_detail_same_backup_source(
    db: DatabaseManager, tmp_path: Path
) -> None:
    _folder, pk = _seed(
        db,
        tmp_path / "mod",
        title="MissSame",
        workshop="1004",
        with_folder=False,
        with_backup_cover=True,
    )
    rows = db.list_mod_list_items(mod_id=int(pk))
    card = list_item_to_card_data(mod_list_item_from_row(rows[0]))
    detail = resolve_cover_path(pk, Path(rows[0]["managed_path"]))
    assert detail is not None
    assert Path(card.cover).resolve() == detail.resolve()


def test_miss_without_backup_cover_is_empty(db: DatabaseManager, tmp_path: Path) -> None:
    _folder, pk = _seed(
        db,
        tmp_path / "mod",
        title="NoCover",
        workshop="1005",
        with_folder=False,
        with_backup_cover=False,
    )
    rows = db.list_mod_list_items(mod_id=int(pk))
    card = list_item_to_card_data(mod_list_item_from_row(rows[0]))
    assert card.cover == ""


def test_card_rebind_no_fs_or_backup_metadata() -> None:
    from ui.mod_card import ModCardWidget

    src = inspect.getsource(ModCardWidget.rebind)
    assert "path.exists" not in src
    assert ".is_dir(" not in src
    assert "metadata.json" not in src
    assert "backup_root" not in src
    assert "reconcile_presence" not in src
    assert "observe_mod_fs" not in src


def test_cover_schedule_stays_viewport_lazy() -> None:
    from ui.library_view import ModLibraryView

    src = inspect.getsource(ModLibraryView._load_viewport_covers)
    assert "iter_viewport_cover_cards" in src
    assert "ensure_cover" in src
